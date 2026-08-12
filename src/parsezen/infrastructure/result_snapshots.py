"""Encrypted recovery snapshots for results waiting on human review."""

from __future__ import annotations

import json
import logging
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any

from parsezen.document_model import ConvertedResource
from parsezen.domain.jobs import MarkdownOrganization
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
from parsezen.processing import (
    ProcessResult,
    ProcessStage,
    ProcessTelemetry,
    StageTelemetry,
)
from parsezen.revision import RevisionKind, build_revision_draft
from parsezen.translation_quality import (
    TranslationIssueKind,
    TranslationQualityIssue,
    TranslationQualityReport,
)

_SNAPSHOT_MEDIA_TYPE = "application/vnd.parsezen.result+json"
LOGGER = logging.getLogger(__name__)


class ResultSnapshotStore:
    """Persist sensitive review inputs encrypted, keeping SQLite metadata-only."""

    def __init__(self, state: StateStore, artifacts: ArtifactStore) -> None:
        self._state = state
        self._artifacts = artifacts
        self._lock = RLock()

    def save(self, job_id: str, result: ProcessResult) -> str:
        with self._lock:
            previous_manifest_id = self._state.load_result_snapshot(job_id)
            previous_artifact_ids = self._snapshot_artifact_ids(
                job_id,
                previous_manifest_id,
            )
            created_artifact_ids: list[str] = []
            manifest_id: str | None = None
            try:
                resources: list[dict[str, str]] = []
                for resource in result.revision_resources:
                    record = self._artifacts.put(
                        job_id=job_id,
                        payload=resource.content,
                        media_type=resource.media_type,
                    )
                    created_artifact_ids.append(record.id)
                    resources.append(
                        {
                            "path": resource.relative_path.as_posix(),
                            "media_type": resource.media_type,
                            "artifact_id": record.id,
                        }
                    )
                payload = _result_to_json(result)
                payload["revision_resources"] = resources
                manifest = self._artifacts.put_text(
                    job_id=job_id,
                    text=json.dumps(
                        payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    media_type=_SNAPSHOT_MEDIA_TYPE,
                )
                manifest_id = manifest.id
                created_artifact_ids.append(manifest.id)
                self._state.save_result_snapshot(job_id, manifest.id)
            except Exception:
                if not self._snapshot_pointer_matches(job_id, manifest_id):
                    self._remove_after_failed_save(job_id, tuple(created_artifact_ids))
                raise

            if previous_manifest_id is not None and previous_manifest_id != manifest_id:
                try:
                    self._remove_artifacts(job_id, previous_artifact_ids)
                except OSError:
                    LOGGER.warning("stale_result_snapshot_artifact_cleanup_failed")
            if manifest_id is None:
                raise AssertionError("A saved snapshot always has a manifest.")
            return manifest_id

    def load(self, job_id: str) -> ProcessResult | None:
        with self._lock:
            artifact_id = self._state.load_result_snapshot(job_id)
            if artifact_id is None:
                return None
            raw = json.loads(self._artifacts.read_text(job_id, artifact_id))
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

    def discard(self, job_id: str) -> None:
        with self._lock:
            manifest_id = self._state.load_result_snapshot(job_id)
            artifact_ids = self._snapshot_artifact_ids(job_id, manifest_id)
            self._state.delete_result_snapshot(job_id)
            self._remove_artifacts(job_id, artifact_ids)

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

    def _snapshot_pointer_matches(self, job_id: str, manifest_id: str | None) -> bool:
        if manifest_id is None:
            return False
        try:
            return self._state.load_result_snapshot(job_id) == manifest_id
        except (OSError, RuntimeError, ValueError):
            # If persistence is unreadable, retaining encrypted files is safer
            # than deleting a snapshot that may already have been committed.
            return True

    def _remove_after_failed_save(
        self,
        job_id: str,
        artifact_ids: tuple[str, ...],
    ) -> None:
        try:
            self._remove_artifacts(job_id, artifact_ids)
        except OSError:
            LOGGER.warning("failed_result_snapshot_artifact_cleanup_failed")

    def _remove_artifacts(self, job_id: str, artifact_ids: tuple[str, ...]) -> None:
        failure: OSError | None = None
        for artifact_id in reversed(artifact_ids):
            try:
                self._artifacts.remove_artifact(job_id, artifact_id)
            except OSError as exc:
                failure = failure or exc
        if failure is not None:
            raise failure


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
    metadata_raw = raw.get("revision_epub_metadata")
    metadata = (
        EpubBookMetadata(
            title=str(metadata_raw["title"]),
            language=str(metadata_raw["language"]),
            author=metadata_raw.get("author"),
            cover_resource=(
                PurePosixPath(str(metadata_raw["cover_resource"]))
                if metadata_raw.get("cover_resource")
                else None
            ),
            identifiers=tuple(str(value) for value in metadata_raw.get("identifiers", ())),
            publisher=metadata_raw.get("publisher"),
            publication_date=metadata_raw.get("publication_date"),
        )
        if isinstance(metadata_raw, dict)
        else None
    )
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


def _translation_report_from_json(raw: object) -> TranslationQualityReport | None:
    if not isinstance(raw, dict):
        return None
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
    )
