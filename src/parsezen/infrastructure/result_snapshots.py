"""Encrypted recovery snapshots for results waiting on human review."""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any

from parsezen.document_model import ConvertedResource
from parsezen.domain.jobs import MarkdownOrganization
from parsezen.domain.process_lifecycle import ProcessStage
from parsezen.domain.source_identity import SourceIdentity
from parsezen.epub_builder import EpubBookMetadata
from parsezen.final_integrity import (
    FinalIntegrityReport,
    IntegrityFinding,
    IntegrityLedger,
    IntegritySeverity,
)
from parsezen.infrastructure.artifact_store import ArtifactStore
from parsezen.infrastructure.state_store import StateStore
from parsezen.pdf_conversion import PdfQualityReport, PdfReviewIssue
from parsezen.pipeline.contracts import (
    ProcessResult,
    ProcessTelemetry,
    StageTelemetry,
)
from parsezen.revision import RevisionDraft, RevisionKind, build_revision_draft
from parsezen.translation_quality import (
    LinguisticReviewCoverage,
    LinguisticReviewMode,
    TranslationIssueKind,
    TranslationQualityIssue,
    TranslationQualityReport,
)

_SNAPSHOT_MEDIA_TYPE = "application/vnd.parsezen.result+json"
_MARKDOWN_MEDIA_TYPE = "text/markdown; charset=utf-8"
_V2_MANIFEST_ID = "manifest"
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReviewResourceReference:
    """Encrypted resource referenced by one immutable snapshot generation."""

    path: PurePosixPath
    media_type: str
    artifact_id: str


@dataclass(frozen=True, slots=True)
class ReviewSnapshot:
    """Minimal durable contract needed to resume one pending human review."""

    generation: str
    destination: Path
    source_identity: SourceIdentity | None
    base_text_artifact_id: str
    proposed_text_artifact_id: str | None
    review_text_artifact_id: str
    resources: tuple[ReviewResourceReference, ...]
    revision_kinds: frozenset[RevisionKind]
    state: dict[str, Any]


class ResultSnapshotStore:
    """Persist sensitive review inputs encrypted, keeping SQLite metadata-only."""

    def __init__(self, state: StateStore, artifacts: ArtifactStore) -> None:
        self._state = state
        self._artifacts = artifacts
        self._lock = RLock()

    def save(
        self,
        job_id: str,
        result: ProcessResult,
        *,
        source_identity: SourceIdentity | None = None,
    ) -> str:
        with self._lock:
            previous_pointer = self._state.load_result_snapshot(job_id)
            generation = f"gen-{secrets.token_hex(16)}"
            try:
                snapshot = self._write_generation(
                    job_id,
                    generation,
                    result,
                    source_identity,
                )
                self._artifacts.put_text(
                    job_id=job_id,
                    text=json.dumps(
                        _snapshot_to_json(snapshot),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    media_type=_SNAPSHOT_MEDIA_TYPE,
                    artifact_id=_V2_MANIFEST_ID,
                    generation=generation,
                )
                self._state.save_result_snapshot(job_id, generation)
            except Exception:
                if not self._snapshot_pointer_matches(job_id, generation):
                    self._remove_generation_after_failed_save(job_id, generation)
                raise

            if previous_pointer is not None and previous_pointer != generation:
                try:
                    self._remove_snapshot(job_id, previous_pointer)
                except OSError:
                    LOGGER.warning("stale_result_snapshot_artifact_cleanup_failed")
            return generation

    def load(
        self,
        job_id: str,
        *,
        source_identity: SourceIdentity | None = None,
    ) -> ProcessResult | None:
        with self._lock:
            pointer = self._state.load_result_snapshot(job_id)
            if pointer is None:
                return None
            if not self._artifacts.generation_exists(job_id, pointer):
                return self._load_v1(job_id, pointer)
            raw = json.loads(
                self._artifacts.read_text(
                    job_id,
                    _V2_MANIFEST_ID,
                    generation=pointer,
                )
            )
            if not isinstance(raw, dict):
                raise ValueError("La instantánea de revisión no es válida.")
            snapshot = _snapshot_from_json(pointer, raw)
            if source_identity is not None and snapshot.source_identity != source_identity:
                raise ValueError("La instantánea pertenece a otra versión del original.")
            result = self._result_from_snapshot(job_id, snapshot)
            self._artifacts.prune_orphaned_generations(job_id, (pointer,))
            return result

    def discard(self, job_id: str) -> None:
        with self._lock:
            pointer = self._state.load_result_snapshot(job_id)
            self._state.delete_result_snapshot(job_id)
            if pointer is not None:
                self._remove_snapshot(job_id, pointer)

    def _write_generation(
        self,
        job_id: str,
        generation: str,
        result: ProcessResult,
        source_identity: SourceIdentity | None,
    ) -> ReviewSnapshot:
        draft = result.revision_draft
        base_text = draft.original_markdown if draft is not None else result.review_markdown
        if base_text is None or result.review_markdown is None:
            raise ValueError("La revisión no contiene el texto necesario para recuperarse.")

        text_artifacts: dict[str, str] = {}

        def store_text(text: str) -> str:
            existing = text_artifacts.get(text)
            if existing is not None:
                return existing
            record = self._artifacts.put_text(
                job_id=job_id,
                generation=generation,
                text=text,
                media_type=_MARKDOWN_MEDIA_TYPE,
            )
            text_artifacts[text] = record.id
            return record.id

        base_id = store_text(base_text)
        proposed_id = store_text(draft.proposed_markdown) if draft is not None else None
        review_id = store_text(result.review_markdown)
        resources = tuple(
            ReviewResourceReference(
                resource.relative_path,
                resource.media_type,
                self._artifacts.put(
                    job_id=job_id,
                    generation=generation,
                    payload=resource.content,
                    media_type=resource.media_type,
                ).id,
            )
            for resource in result.revision_resources
        )
        return ReviewSnapshot(
            generation=generation,
            destination=result.final_path,
            source_identity=source_identity,
            base_text_artifact_id=base_id,
            proposed_text_artifact_id=proposed_id,
            review_text_artifact_id=review_id,
            resources=resources,
            revision_kinds=draft.kinds if draft is not None else frozenset(),
            state=_result_recovery_state(result),
        )

    def _result_from_snapshot(self, job_id: str, snapshot: ReviewSnapshot) -> ProcessResult:
        def read_text(artifact_id: str) -> str:
            return self._artifacts.read_text(
                job_id,
                artifact_id,
                generation=snapshot.generation,
            )

        base_text = read_text(snapshot.base_text_artifact_id)
        proposed_text = (
            read_text(snapshot.proposed_text_artifact_id)
            if snapshot.proposed_text_artifact_id is not None
            else None
        )
        draft = (
            build_revision_draft(
                base_text,
                proposed_text,
                kinds=snapshot.revision_kinds,
            )
            if proposed_text is not None
            else None
        )
        resources = tuple(
            ConvertedResource(
                reference.path,
                self._artifacts.read(
                    job_id,
                    reference.artifact_id,
                    generation=snapshot.generation,
                ),
                reference.media_type,
            )
            for reference in snapshot.resources
        )
        return _result_from_recovery_state(
            snapshot.destination,
            snapshot.state,
            draft=draft,
            resources=resources,
            review_markdown=read_text(snapshot.review_text_artifact_id),
        )

    def _load_v1(self, job_id: str, manifest_id: str) -> ProcessResult:
        raw = json.loads(self._artifacts.read_text(job_id, manifest_id))
        if not isinstance(raw, dict):
            raise ValueError("La instantánea de revisión no es válida.")
        resources = tuple(
            ConvertedResource(
                relative_path=PurePosixPath(str(item["path"])),
                content=self._artifacts.read(job_id, str(item["artifact_id"])),
                media_type=str(item["media_type"]),
            )
            for item in raw.get("revision_resources", ())
        )
        return _result_from_json(raw, resources)

    def _snapshot_artifact_ids(
        self,
        job_id: str,
        manifest_id: str | None,
    ) -> tuple[str, ...]:
        if manifest_id is None:
            return ()
        identifiers = [manifest_id]
        try:
            raw = json.loads(self._artifacts.read_text(job_id, manifest_id))
            resources = raw.get("revision_resources", ()) if isinstance(raw, dict) else ()
            for item in resources:
                if isinstance(item, dict) and isinstance(item.get("artifact_id"), str):
                    identifiers.append(item["artifact_id"])
        except (json.JSONDecodeError, OSError, TypeError, UnicodeError, ValueError):
            LOGGER.warning("result_snapshot_manifest_cleanup_degraded")
        return tuple(dict.fromkeys(identifiers))

    def _snapshot_pointer_matches(self, job_id: str, pointer: str) -> bool:
        try:
            return self._state.load_result_snapshot(job_id) == pointer
        except (OSError, RuntimeError, ValueError):
            # If persistence is unreadable, retaining encrypted files is safer
            # than deleting a snapshot that may already have been committed.
            return True

    def _remove_generation_after_failed_save(self, job_id: str, generation: str) -> None:
        try:
            self._artifacts.remove_generation(job_id, generation)
        except OSError:
            LOGGER.warning("failed_result_snapshot_artifact_cleanup_failed")

    def _remove_snapshot(self, job_id: str, pointer: str) -> None:
        if self._artifacts.generation_exists(job_id, pointer):
            self._artifacts.remove_generation(job_id, pointer)
            return
        self._remove_artifacts(job_id, self._snapshot_artifact_ids(job_id, pointer))

    def _remove_artifacts(self, job_id: str, artifact_ids: tuple[str, ...]) -> None:
        failure: OSError | None = None
        for artifact_id in reversed(artifact_ids):
            try:
                self._artifacts.remove_artifact(job_id, artifact_id)
            except OSError as exc:
                failure = failure or exc
        if failure is not None:
            raise failure


def _snapshot_to_json(snapshot: ReviewSnapshot) -> dict[str, Any]:
    identity = snapshot.source_identity
    return {
        "version": 2,
        "destination": str(snapshot.destination),
        "source_identity": (
            {
                "size_bytes": identity.size_bytes,
                "modified_ns": identity.modified_ns,
                "sha256": identity.sha256,
            }
            if identity is not None
            else None
        ),
        "texts": {
            "base_artifact_id": snapshot.base_text_artifact_id,
            "proposed_artifact_id": snapshot.proposed_text_artifact_id,
            "review_artifact_id": snapshot.review_text_artifact_id,
        },
        "resources": [
            {
                "path": resource.path.as_posix(),
                "media_type": resource.media_type,
                "artifact_id": resource.artifact_id,
            }
            for resource in snapshot.resources
        ],
        "revision_kinds": sorted(kind.value for kind in snapshot.revision_kinds),
        "review_state": snapshot.state,
    }


def _snapshot_from_json(generation: str, raw: dict[str, Any]) -> ReviewSnapshot:
    if int(raw.get("version", 0)) != 2:
        raise ValueError("La versión de la instantánea de revisión no es compatible.")
    texts = raw.get("texts")
    state = raw.get("review_state")
    if not isinstance(texts, dict) or not isinstance(state, dict):
        raise ValueError("La instantánea de revisión no es válida.")
    identity_raw = raw.get("source_identity")
    identity = (
        SourceIdentity(
            size_bytes=int(identity_raw["size_bytes"]),
            modified_ns=int(identity_raw["modified_ns"]),
            sha256=str(identity_raw["sha256"]),
        )
        if isinstance(identity_raw, dict)
        else None
    )
    try:
        base_id = str(texts["base_artifact_id"])
        review_id = str(texts["review_artifact_id"])
        proposed_value = texts.get("proposed_artifact_id")
        return ReviewSnapshot(
            generation=generation,
            destination=Path(str(raw["destination"])),
            source_identity=identity,
            base_text_artifact_id=base_id,
            proposed_text_artifact_id=(str(proposed_value) if proposed_value is not None else None),
            review_text_artifact_id=review_id,
            resources=tuple(
                ReviewResourceReference(
                    PurePosixPath(str(item["path"])),
                    str(item["media_type"]),
                    str(item["artifact_id"]),
                )
                for item in raw.get("resources", ())
                if isinstance(item, dict)
            ),
            revision_kinds=frozenset(
                RevisionKind(value) for value in raw.get("revision_kinds", ())
            ),
            state=state,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("La instantánea de revisión no es válida.") from exc


def _result_recovery_state(result: ProcessResult) -> dict[str, Any]:
    """Persist only state needed to resume review and publish a decision."""

    serialized = _result_to_json(result)
    retained = (
        "review_original_path",
        "problematic_pdf_pages",
        "pdf_quality_report",
        "translation_quality_report",
        "linguistic_review_coverage",
        "preserved_translation_chunks",
        "preserved_images",
        "epub_chapters",
        "revision_epub_metadata",
        "review_required",
        "revision_approved",
        "final_integrity_report",
        "markdown_organization",
        "markdown_include_metadata",
        "markdown_include_page_references",
        "markdown_source_name",
    )
    return {key: serialized[key] for key in retained}


def _result_from_recovery_state(
    destination: Path,
    state: dict[str, Any],
    *,
    draft: RevisionDraft | None,
    resources: tuple[ConvertedResource, ...],
    review_markdown: str,
) -> ProcessResult:
    return ProcessResult(
        final_path=destination,
        review_original_path=_optional_path(state.get("review_original_path")),
        problematic_pdf_pages=tuple(int(value) for value in state.get("problematic_pdf_pages", ())),
        pdf_quality_report=_pdf_report_from_json(state.get("pdf_quality_report")),
        translation_quality_report=_translation_report_from_json(
            state.get("translation_quality_report")
        ),
        linguistic_review_coverage=_linguistic_review_coverage_from_json(
            state.get("linguistic_review_coverage")
        ),
        preserved_translation_chunks=tuple(
            int(value) for value in state.get("preserved_translation_chunks", ())
        ),
        preserved_images=max(0, int(state.get("preserved_images", 0))),
        epub_chapters=max(0, int(state.get("epub_chapters", 0))),
        revision_draft=draft,
        revision_resources=resources,
        revision_epub_metadata=_epub_metadata_from_json(state.get("revision_epub_metadata")),
        review_markdown=review_markdown,
        review_required=bool(state.get("review_required", True)),
        revision_approved=bool(state.get("revision_approved", False)),
        final_integrity_report=_integrity_report_from_json(state.get("final_integrity_report")),
        markdown_organization=MarkdownOrganization(
            state.get("markdown_organization", MarkdownOrganization.SINGLE_FILE.value)
        ),
        markdown_include_metadata=bool(state.get("markdown_include_metadata", False)),
        markdown_include_page_references=bool(state.get("markdown_include_page_references", False)),
        markdown_source_name=state.get("markdown_source_name"),
    )


def _result_to_json(result: ProcessResult) -> dict[str, Any]:
    draft = result.revision_draft
    metadata = result.revision_epub_metadata
    return {
        "version": 1,
        "final_path": str(result.final_path),
        "raw_markdown_path": _path(result.raw_markdown_path),
        "review_original_path": _path(result.review_original_path),
        "problematic_pdf_pages": list(result.problematic_pdf_pages),
        "pdf_quality_report": _pdf_report_to_json(result.pdf_quality_report),
        "exhaustive_pdf_ocr_used": result.exhaustive_pdf_ocr_used,
        "epub_translation_parts": result.epub_translation_parts,
        "epub_resumed_parts": result.epub_resumed_parts,
        "epub_checkpoint_degraded": result.epub_checkpoint_degraded,
        "translation_quality_report": _translation_report_to_json(
            result.translation_quality_report
        ),
        "linguistic_review_coverage": _linguistic_review_coverage_to_json(
            result.linguistic_review_coverage
        ),
        "preserved_translation_chunks": list(result.preserved_translation_chunks),
        "preserved_images": result.preserved_images,
        "epub_chapters": result.epub_chapters,
        "revision_draft": (
            {
                "original": draft.original_markdown,
                "proposed": draft.proposed_markdown,
                "kinds": sorted(kind.value for kind in draft.kinds),
            }
            if draft is not None
            else None
        ),
        "revision_resources": [],
        "revision_epub_metadata": (
            {
                "title": metadata.title,
                "language": metadata.language,
                "author": metadata.author,
                "cover_resource": (
                    metadata.cover_resource.as_posix()
                    if metadata.cover_resource is not None
                    else None
                ),
                "identifiers": list(metadata.identifiers),
                "publisher": metadata.publisher,
                "publication_date": metadata.publication_date,
            }
            if metadata is not None
            else None
        ),
        "review_markdown": result.review_markdown,
        "review_required": result.review_required,
        "revision_approved": result.revision_approved,
        "final_integrity_report": _integrity_report_to_json(result.final_integrity_report),
        "front_matter_blocks": result.front_matter_blocks,
        "toc_blocks": result.toc_blocks,
        "terminology_terms": result.terminology_terms,
        "markdown_organization": result.markdown_organization.value,
        "markdown_include_metadata": result.markdown_include_metadata,
        "markdown_include_page_references": result.markdown_include_page_references,
        "markdown_source_name": result.markdown_source_name,
        "telemetry": (
            {
                "total_duration_ms": result.telemetry.total_duration_ms,
                "stages": [
                    {
                        "stage": stage.stage.value,
                        "duration_ms": stage.duration_ms,
                        "visits": stage.visits,
                    }
                    for stage in result.telemetry.stages
                ],
            }
            if result.telemetry is not None
            else None
        ),
    }


def _result_from_json(
    raw: dict[str, Any],
    resources: tuple[ConvertedResource, ...],
) -> ProcessResult:
    if int(raw.get("version", 0)) != 1:
        raise ValueError("La versión de la instantánea de revisión no es compatible.")
    draft_raw = raw.get("revision_draft")
    draft = (
        build_revision_draft(
            str(draft_raw["original"]),
            str(draft_raw["proposed"]),
            kinds=frozenset(RevisionKind(value) for value in draft_raw["kinds"]),
        )
        if isinstance(draft_raw, dict)
        else None
    )
    metadata = _epub_metadata_from_json(raw.get("revision_epub_metadata"))
    return ProcessResult(
        final_path=Path(str(raw["final_path"])),
        raw_markdown_path=_optional_path(raw.get("raw_markdown_path")),
        review_original_path=_optional_path(raw.get("review_original_path")),
        problematic_pdf_pages=tuple(int(value) for value in raw.get("problematic_pdf_pages", ())),
        pdf_quality_report=_pdf_report_from_json(raw.get("pdf_quality_report")),
        exhaustive_pdf_ocr_used=bool(raw.get("exhaustive_pdf_ocr_used", False)),
        epub_translation_parts=int(raw.get("epub_translation_parts", 0)),
        epub_resumed_parts=int(raw.get("epub_resumed_parts", 0)),
        epub_checkpoint_degraded=bool(raw.get("epub_checkpoint_degraded", False)),
        translation_quality_report=_translation_report_from_json(
            raw.get("translation_quality_report")
        ),
        linguistic_review_coverage=_linguistic_review_coverage_from_json(
            raw.get("linguistic_review_coverage")
        ),
        preserved_translation_chunks=tuple(
            int(value) for value in raw.get("preserved_translation_chunks", ())
        ),
        preserved_images=int(raw.get("preserved_images", 0)),
        epub_chapters=int(raw.get("epub_chapters", 0)),
        revision_draft=draft,
        revision_resources=resources,
        revision_epub_metadata=metadata,
        review_markdown=raw.get("review_markdown"),
        review_required=bool(raw.get("review_required", False)),
        revision_approved=bool(raw.get("revision_approved", False)),
        final_integrity_report=_integrity_report_from_json(raw.get("final_integrity_report")),
        telemetry=_telemetry_from_json(raw.get("telemetry")),
        front_matter_blocks=max(0, int(raw.get("front_matter_blocks", 0))),
        toc_blocks=max(0, int(raw.get("toc_blocks", 0))),
        terminology_terms=max(0, int(raw.get("terminology_terms", 0))),
        markdown_organization=MarkdownOrganization(
            raw.get("markdown_organization", MarkdownOrganization.SINGLE_FILE.value)
        ),
        markdown_include_metadata=bool(raw.get("markdown_include_metadata", False)),
        markdown_include_page_references=bool(raw.get("markdown_include_page_references", False)),
        markdown_source_name=raw.get("markdown_source_name"),
    )


def _epub_metadata_from_json(raw: object) -> EpubBookMetadata | None:
    if not isinstance(raw, dict):
        return None
    return EpubBookMetadata(
        title=str(raw["title"]),
        language=str(raw["language"]),
        author=raw.get("author"),
        cover_resource=(
            PurePosixPath(str(raw["cover_resource"])) if raw.get("cover_resource") else None
        ),
        identifiers=tuple(str(value) for value in raw.get("identifiers", ())),
        publisher=raw.get("publisher"),
        publication_date=raw.get("publication_date"),
    )


def _path(value: Path | None) -> str | None:
    return str(value) if value is not None else None


def _optional_path(value: object) -> Path | None:
    return Path(value) if isinstance(value, str) and value else None


def _telemetry_from_json(raw: object) -> ProcessTelemetry | None:
    if not isinstance(raw, dict):
        return None
    stages = raw.get("stages")
    if not isinstance(stages, list):
        return None
    try:
        return ProcessTelemetry(
            total_duration_ms=max(0, int(raw["total_duration_ms"])),
            stages=tuple(
                StageTelemetry(
                    stage=ProcessStage(item["stage"]),
                    duration_ms=max(0, int(item["duration_ms"])),
                    visits=max(1, int(item["visits"])),
                )
                for item in stages
                if isinstance(item, dict)
            ),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _integrity_report_to_json(
    report: FinalIntegrityReport | None,
) -> dict[str, Any] | None:
    if report is None:
        return None
    return {
        "format_label": report.format_label,
        "checks": list(report.checks),
        "ledger": {
            "blocks": report.ledger.blocks,
            "paragraphs": report.ledger.paragraphs,
            "headings": report.ledger.headings,
            "images": report.ledger.images,
            "links": report.ledger.links,
            "resources": report.ledger.resources,
        },
        "findings": [
            {
                "code": finding.code,
                "message": finding.message,
                "severity": finding.severity.value,
                "expected": finding.expected,
                "actual": finding.actual,
            }
            for finding in report.findings
        ],
    }


def _integrity_report_from_json(raw: object) -> FinalIntegrityReport | None:
    if not isinstance(raw, dict):
        return None
    ledger_raw = raw.get("ledger")
    if not isinstance(ledger_raw, dict):
        return None
    try:
        return FinalIntegrityReport(
            format_label=str(raw["format_label"]),
            checks=tuple(str(value) for value in raw.get("checks", ())),
            ledger=IntegrityLedger(
                blocks=max(0, int(ledger_raw.get("blocks", 0))),
                paragraphs=max(0, int(ledger_raw.get("paragraphs", 0))),
                headings=max(0, int(ledger_raw.get("headings", 0))),
                images=max(0, int(ledger_raw.get("images", 0))),
                links=max(0, int(ledger_raw.get("links", 0))),
                resources=max(0, int(ledger_raw.get("resources", 0))),
            ),
            findings=tuple(
                IntegrityFinding(
                    code=str(item["code"]),
                    message=str(item["message"]),
                    severity=IntegritySeverity(item["severity"]),
                    expected=(int(item["expected"]) if item.get("expected") is not None else None),
                    actual=(int(item["actual"]) if item.get("actual") is not None else None),
                )
                for item in raw.get("findings", ())
                if isinstance(item, dict)
            ),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _pdf_report_to_json(report: PdfQualityReport | None) -> dict[str, Any] | None:
    if report is None:
        return None
    return {
        "processed_pages": list(report.processed_pages),
        "ocr_pages": list(report.ocr_pages),
        "ocr_replaced_pages": list(report.ocr_replaced_pages),
        "low_confidence_pages": list(report.low_confidence_pages),
        "issues": [
            {
                "page_number": issue.page_number,
                "message": issue.message,
                "markdown": issue.markdown,
                "identifier": issue.identifier,
                "blocking": issue.blocking,
                "target_marker": issue.target_marker,
            }
            for issue in report.issues
        ],
    }


def _pdf_report_from_json(raw: object) -> PdfQualityReport | None:
    if not isinstance(raw, dict):
        return None
    return PdfQualityReport(
        processed_pages=tuple(int(value) for value in raw.get("processed_pages", ())),
        ocr_pages=tuple(int(value) for value in raw.get("ocr_pages", ())),
        ocr_replaced_pages=tuple(int(value) for value in raw.get("ocr_replaced_pages", ())),
        low_confidence_pages=tuple(int(value) for value in raw.get("low_confidence_pages", ())),
        issues=tuple(
            PdfReviewIssue(
                page_number=int(issue["page_number"]),
                message=str(issue["message"]),
                markdown=str(issue["markdown"]),
                identifier=str(issue.get("identifier", "")),
                blocking=bool(issue.get("blocking", False)),
                target_marker=str(issue.get("target_marker", "")),
            )
            for issue in raw.get("issues", ())
        ),
    )


def _translation_report_to_json(
    report: TranslationQualityReport | None,
) -> dict[str, Any] | None:
    if report is None:
        return None
    return {
        "source_language": report.source_language,
        "target_language": report.target_language,
        "detected_language": report.detected_language,
        "checked_segments": report.checked_segments,
        "source_characters": report.source_characters,
        "translated_characters": report.translated_characters,
        "source_blocks": report.source_blocks,
        "translated_blocks": report.translated_blocks,
        "total_issues": report.total_issues,
        "issue_totals": {kind.value: count for kind, count in report.issue_totals},
        "issues": [
            {
                "segment_number": issue.segment_number,
                "kind": issue.kind.value,
                "message": issue.message,
                "original_excerpt": issue.original_excerpt,
                "translated_excerpt": issue.translated_excerpt,
                "identifier": issue.identifier,
            }
            for issue in report.issues
        ],
    }


def _linguistic_review_coverage_to_json(
    coverage: LinguisticReviewCoverage | None,
) -> dict[str, Any] | None:
    if coverage is None:
        return None
    return {
        "mode": coverage.mode.value,
        "translated_blocks": coverage.translated_blocks,
        "automatically_checked_blocks": coverage.automatically_checked_blocks,
        "semantically_reviewed_blocks": coverage.semantically_reviewed_blocks,
        "independently_verified_blocks": coverage.independently_verified_blocks,
        "remaining_issues": coverage.remaining_issues,
    }


def _linguistic_review_coverage_from_json(
    raw: object,
) -> LinguisticReviewCoverage | None:
    if not isinstance(raw, dict):
        return None
    return LinguisticReviewCoverage(
        mode=LinguisticReviewMode(str(raw["mode"])),
        translated_blocks=max(0, int(raw["translated_blocks"])),
        automatically_checked_blocks=max(0, int(raw["automatically_checked_blocks"])),
        semantically_reviewed_blocks=max(0, int(raw["semantically_reviewed_blocks"])),
        independently_verified_blocks=max(0, int(raw["independently_verified_blocks"])),
        remaining_issues=max(0, int(raw["remaining_issues"])),
    )


def _translation_report_from_json(raw: object) -> TranslationQualityReport | None:
    if not isinstance(raw, dict):
        return None
    source_blocks = raw.get("source_blocks", raw["checked_segments"])
    translated_blocks = raw.get("translated_blocks", raw["checked_segments"])
    if source_blocks is None or translated_blocks is None:
        raise ValueError("La cobertura de traducción de la instantánea no es válida.")
    return TranslationQualityReport(
        source_language=raw.get("source_language"),
        target_language=str(raw["target_language"]),
        detected_language=raw.get("detected_language"),
        checked_segments=int(raw["checked_segments"]),
        source_characters=int(raw["source_characters"]),
        translated_characters=int(raw["translated_characters"]),
        total_issues=int(raw["total_issues"]),
        issues=tuple(
            TranslationQualityIssue(
                segment_number=int(issue["segment_number"]),
                kind=TranslationIssueKind(issue["kind"]),
                message=str(issue["message"]),
                original_excerpt=str(issue["original_excerpt"]),
                translated_excerpt=str(issue["translated_excerpt"]),
                identifier=str(issue.get("identifier", "")),
            )
            for issue in raw.get("issues", ())
        ),
        issue_totals=tuple(
            (TranslationIssueKind(kind), int(count))
            for kind, count in raw.get("issue_totals", {}).items()
        ),
        source_blocks=max(0, int(source_blocks)),
        translated_blocks=max(0, int(translated_blocks)),
    )
