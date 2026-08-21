from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

import parsezen.infrastructure.result_snapshots as result_snapshots_module
from parsezen.document_model import ConvertedResource
from parsezen.domain.jobs import DocumentJob, DocumentSource, JobConfiguration
from parsezen.domain.source_identity import SourceIdentity
from parsezen.epub_builder import EpubBookMetadata
from parsezen.final_integrity import FinalIntegrityReport, IntegrityLedger
from parsezen.infrastructure.artifact_store import ArtifactStore
from parsezen.infrastructure.result_snapshots import ResultSnapshotStore
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
    LinguisticReviewCoverage,
    LinguisticReviewMode,
    TranslationIssueKind,
    TranslationQualityIssue,
    TranslationQualityReport,
)


def test_rejects_translation_report_with_null_block_coverage() -> None:
    with pytest.raises(ValueError, match="cobertura de traducción"):
        result_snapshots_module._translation_report_from_json(  # noqa: SLF001
            {"checked_segments": 1, "source_blocks": None}
        )


def test_round_trips_a_sensitive_pending_result_encrypted(tmp_path: Path) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF")
    job = DocumentJob.create(
        DocumentSource.inspect(source),
        JobConfiguration(),
        order=0,
        job_id="book",
    )
    state = StateStore(tmp_path / "state.sqlite3")
    state.upsert_job(job)
    artifacts = ArtifactStore(
        tmp_path / "artifacts",
        protect=lambda payload: b"protected:" + payload,
        unprotect=lambda payload: payload.removeprefix(b"protected:"),
    )
    snapshots = ResultSnapshotStore(state, artifacts)
    draft = build_revision_draft(
        "# Original\n\nTexto.\n",
        "# Propuesta\n\nTexto corregido.\n",
        kinds=frozenset({RevisionKind.CONTENT, RevisionKind.STRUCTURE}),
    )
    result = ProcessResult(
        final_path=tmp_path / "book.epub",
        review_original_path=source,
        problematic_pdf_pages=(2,),
        pdf_quality_report=PdfQualityReport(
            (1, 2),
            (2,),
            (PdfReviewIssue(2, "Revisar", "Texto OCR", "page-2"),),
        ),
        translation_quality_report=TranslationQualityReport(
            "en",
            "es",
            "es",
            1,
            5,
            5,
            1,
            (
                TranslationQualityIssue(
                    1,
                    TranslationIssueKind.SOURCE_TEXT,
                    "Revisar",
                    "Hello",
                    "Hola",
                    "translation-1",
                ),
            ),
            ((TranslationIssueKind.SOURCE_TEXT, 1),),
            source_blocks=5,
            translated_blocks=5,
        ),
        review_translation_quality_report=TranslationQualityReport(
            "en",
            "es",
            "es",
            1,
            5,
            5,
            1,
            (),
            source_blocks=5,
            translated_blocks=5,
        ),
        linguistic_review_coverage=LinguisticReviewCoverage(
            LinguisticReviewMode.INDEPENDENT_BILINGUAL,
            translated_blocks=5,
            automatically_checked_blocks=5,
            semantically_reviewed_blocks=5,
            independently_verified_blocks=5,
            remaining_issues=1,
        ),
        revision_draft=draft,
        revision_resources=(ConvertedResource(PurePosixPath("image.jpg"), b"image", "image/jpeg"),),
        revision_epub_metadata=EpubBookMetadata(
            "Libro",
            "es",
            "Autora",
            PurePosixPath("image.jpg"),
            identifiers=("primary-id", "secondary-id"),
            publisher="Editorial local",
            publication_date="2024-03-14",
        ),
        review_markdown="# Propuesta\n\nTexto corregido.\n",
        review_required=True,
        preserve_epub_package_on_unchanged_review=True,
        final_integrity_report=FinalIntegrityReport(
            "EPUB",
            ("Contenedor EPUB válido", "Contenido aprobado conservado"),
            IntegrityLedger(blocks=2, headings=2, images=1, resources=1),
        ),
        telemetry=ProcessTelemetry(
            1250,
            (
                StageTelemetry(ProcessStage.READING, 400, 1),
                StageTelemetry(ProcessStage.TRANSLATING, 850, 1),
            ),
        ),
        front_matter_blocks=3,
        toc_blocks=2,
        terminology_terms=4,
    )

    identity = SourceIdentity.inspect(source)
    snapshots.save(job.id, result, source_identity=identity)
    recovered = snapshots.load(job.id)

    assert recovered == replace(
        result,
        telemetry=None,
        front_matter_blocks=0,
        toc_blocks=0,
        terminology_terms=0,
    )
    generation = state.load_result_snapshot(job.id)
    assert generation is not None
    generation_directory = tmp_path / "artifacts" / job.id / generation
    assert all(
        path.read_bytes().startswith(b"protected:") for path in generation_directory.iterdir()
    )
    manifest = json.loads(artifacts.read_text(job.id, "manifest", generation=generation))
    assert manifest["version"] == 2
    assert manifest["source_identity"] == {
        "size_bytes": identity.size_bytes,
        "modified_ns": identity.modified_ns,
        "sha256": identity.sha256,
    }
    assert "telemetry" not in manifest["review_state"]
    assert manifest["texts"]["proposed_artifact_id"] == manifest["texts"]["review_artifact_id"]
    assert len(tuple(generation_directory.glob("*.pza"))) == 4


def test_missing_snapshot_is_not_an_error(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite3")
    artifacts = ArtifactStore(
        tmp_path / "artifacts",
        protect=lambda payload: payload,
        unprotect=lambda payload: payload,
    )

    assert ResultSnapshotStore(state, artifacts).load("missing") is None


def test_v2_snapshot_rejects_a_different_source_identity(tmp_path: Path) -> None:
    source = tmp_path / "book.md"
    source.write_text("first", encoding="utf-8")
    job = DocumentJob.create(
        DocumentSource.inspect(source),
        JobConfiguration(),
        order=0,
        job_id="identity",
    )
    state = StateStore(tmp_path / "state.sqlite3")
    state.upsert_job(job)
    artifacts = ArtifactStore(
        tmp_path / "artifacts",
        protect=lambda payload: payload,
        unprotect=lambda payload: payload,
    )
    snapshots = ResultSnapshotStore(state, artifacts)
    identity = SourceIdentity.inspect(source)
    snapshots.save(
        job.id,
        ProcessResult(tmp_path / "result.md", review_markdown="Proposal", review_required=True),
        source_identity=identity,
    )
    stale = SourceIdentity(identity.size_bytes, identity.modified_ns, "0" * 64)

    with pytest.raises(ValueError, match="otra versión"):
        snapshots.load(job.id, source_identity=stale)


def test_failed_snapshot_save_removes_only_artifacts_created_by_that_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF")
    job = DocumentJob.create(
        DocumentSource.inspect(source),
        JobConfiguration(),
        order=0,
        job_id="book",
    )
    state = StateStore(tmp_path / "state.sqlite3")
    state.upsert_job(job)
    artifacts = ArtifactStore(
        tmp_path / "artifacts",
        protect=lambda payload: payload,
        unprotect=lambda payload: payload,
    )
    unrelated = artifacts.put_text(job_id=job.id, text="keep")
    snapshots = ResultSnapshotStore(state, artifacts)
    result = ProcessResult(
        final_path=tmp_path / "book.epub",
        revision_resources=(ConvertedResource(PurePosixPath("image.jpg"), b"image", "image/jpeg"),),
        review_markdown="Texto",
        review_required=True,
    )

    monkeypatch.setattr(
        state,
        "save_result_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    with pytest.raises(OSError, match="disk unavailable"):
        snapshots.save(job.id, result)

    remaining = {path.stem for path in (tmp_path / "artifacts" / job.id).glob("*.pza")}
    assert remaining == {unrelated.id}
    assert state.load_result_snapshot(job.id) is None


def test_replacing_and_discarding_a_snapshot_removes_only_its_private_artifacts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF")
    job = DocumentJob.create(
        DocumentSource.inspect(source),
        JobConfiguration(),
        order=0,
        job_id="book",
    )
    state = StateStore(tmp_path / "state.sqlite3")
    state.upsert_job(job)
    artifacts = ArtifactStore(
        tmp_path / "artifacts",
        protect=lambda payload: payload,
        unprotect=lambda payload: payload,
    )
    unrelated = artifacts.put_text(job_id=job.id, text="keep")
    snapshots = ResultSnapshotStore(state, artifacts)
    first = ProcessResult(
        final_path=tmp_path / "first.epub",
        revision_resources=(ConvertedResource(PurePosixPath("first.jpg"), b"first", "image/jpeg"),),
        review_markdown="Primero",
        review_required=True,
    )
    second = ProcessResult(
        final_path=tmp_path / "second.epub",
        revision_resources=(
            ConvertedResource(PurePosixPath("second.jpg"), b"second", "image/jpeg"),
        ),
        review_markdown="Segundo",
        review_required=True,
    )

    first_manifest = snapshots.save(job.id, first)
    first_directory = tmp_path / "artifacts" / job.id / first_manifest
    second_manifest = snapshots.save(job.id, second)

    assert first_manifest != second_manifest
    assert not first_directory.exists()
    assert snapshots.load(job.id) == second
    assert (tmp_path / "artifacts" / job.id / f"{unrelated.id}.pza").exists()

    snapshots.discard(job.id)

    assert state.load_result_snapshot(job.id) is None
    assert {path.stem for path in (tmp_path / "artifacts" / job.id).glob("*.pza")} == {unrelated.id}


def test_loads_a_legacy_v1_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "legacy.md"
    source.write_text("Original", encoding="utf-8")
    job = DocumentJob.create(
        DocumentSource.inspect(source),
        JobConfiguration(),
        order=0,
        job_id="legacy",
    )
    state = StateStore(tmp_path / "state.sqlite3")
    state.upsert_job(job)
    artifacts = ArtifactStore(
        tmp_path / "artifacts",
        protect=lambda payload: payload,
        unprotect=lambda payload: payload,
    )
    result = ProcessResult(
        tmp_path / "legacy.out.md",
        review_original_path=source,
        review_markdown="Propuesta",
        review_required=True,
    )
    raw = result_snapshots_module._result_to_json(result)  # noqa: SLF001
    manifest = artifacts.put_text(
        job_id=job.id,
        text=json.dumps(raw),
        media_type="application/vnd.parsezen.result+json",
    )
    state.save_result_snapshot(job.id, manifest.id)

    assert ResultSnapshotStore(state, artifacts).load(job.id) == result


def test_crash_before_pointer_update_keeps_previous_generation(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.md"
    source.write_text("Original", encoding="utf-8")
    job = DocumentJob.create(
        DocumentSource.inspect(source),
        JobConfiguration(),
        order=0,
        job_id="before-pointer",
    )
    state = StateStore(tmp_path / "state.sqlite3")
    state.upsert_job(job)
    artifacts = ArtifactStore(
        tmp_path / "artifacts",
        protect=lambda payload: payload,
        unprotect=lambda payload: payload,
    )
    snapshots = ResultSnapshotStore(state, artifacts)
    first = ProcessResult(tmp_path / "first.md", review_markdown="Primero", review_required=True)
    second = ProcessResult(tmp_path / "second.md", review_markdown="Segundo", review_required=True)
    first_generation = snapshots.save(job.id, first)
    monkeypatch.setattr(
        state,
        "save_result_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("crash before pointer")),
    )

    with pytest.raises(OSError, match="before pointer"):
        snapshots.save(job.id, second)

    assert state.load_result_snapshot(job.id) == first_generation
    assert snapshots.load(job.id) == first
    generation_directories = tuple(
        path.name for path in (tmp_path / "artifacts" / job.id).iterdir() if path.is_dir()
    )
    assert generation_directories == (first_generation,)


def test_crash_after_pointer_update_recovers_new_generation_and_collects_old(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("Original", encoding="utf-8")
    job = DocumentJob.create(
        DocumentSource.inspect(source),
        JobConfiguration(),
        order=0,
        job_id="after-pointer",
    )
    state = StateStore(tmp_path / "state.sqlite3")
    state.upsert_job(job)
    artifacts = ArtifactStore(
        tmp_path / "artifacts",
        protect=lambda payload: payload,
        unprotect=lambda payload: payload,
    )
    snapshots = ResultSnapshotStore(state, artifacts)
    first = ProcessResult(tmp_path / "first.md", review_markdown="Primero", review_required=True)
    second = ProcessResult(tmp_path / "second.md", review_markdown="Segundo", review_required=True)
    first_generation = snapshots.save(job.id, first)
    save_pointer = state.save_result_snapshot

    def commit_then_crash(job_id: str, generation: str) -> None:
        save_pointer(job_id, generation)
        raise OSError("crash after pointer")

    monkeypatch.setattr(state, "save_result_snapshot", commit_then_crash)
    with pytest.raises(OSError, match="after pointer"):
        snapshots.save(job.id, second)
    active_generation = state.load_result_snapshot(job.id)
    assert active_generation is not None and active_generation != first_generation

    assert snapshots.load(job.id) == second
    assert not (tmp_path / "artifacts" / job.id / first_generation).exists()
