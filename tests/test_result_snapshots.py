from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from parsezen.document_model import ConvertedResource
from parsezen.domain.jobs import DocumentJob, DocumentSource, JobConfiguration
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
    TranslationIssueKind,
    TranslationQualityIssue,
    TranslationQualityReport,
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
        ),
        revision_draft=draft,
        revision_resources=(ConvertedResource(PurePosixPath("image.jpg"), b"image", "image/jpeg"),),
        revision_epub_metadata=EpubBookMetadata(
            "Libro",
            "es",
            "Autora",
            PurePosixPath("image.jpg"),
        ),
        review_markdown="# Propuesta\n\nTexto corregido.\n",
        review_required=True,
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

    snapshots.save(job.id, result)
    recovered = snapshots.load(job.id)

    assert recovered == result
    manifest_id = state.load_result_snapshot(job.id)
    assert manifest_id is not None
    assert (
        (tmp_path / "artifacts" / job.id / f"{manifest_id}.pza")
        .read_bytes()
        .startswith(b"protected:")
    )


def test_missing_snapshot_is_not_an_error(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite3")
    artifacts = ArtifactStore(
        tmp_path / "artifacts",
        protect=lambda payload: payload,
        unprotect=lambda payload: payload,
    )

    assert ResultSnapshotStore(state, artifacts).load("missing") is None


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
    first_files = {path.name for path in (tmp_path / "artifacts" / job.id).glob("*.pza")} - {
        f"{unrelated.id}.pza"
    }
    second_manifest = snapshots.save(job.id, second)

    assert first_manifest != second_manifest
    assert not any((tmp_path / "artifacts" / job.id / name).exists() for name in first_files)
    assert snapshots.load(job.id) == second
    assert (tmp_path / "artifacts" / job.id / f"{unrelated.id}.pza").exists()

    snapshots.discard(job.id)

    assert state.load_result_snapshot(job.id) is None
    assert {path.stem for path in (tmp_path / "artifacts" / job.id).glob("*.pza")} == {unrelated.id}
