import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import parsezen.job_sessions as sessions_module
from parsezen.domain.outcomes import OutcomeSummary
from parsezen.glossary import GlossaryEntry
from parsezen.improvement import ImprovementMode
from parsezen.job_sessions import (
    MAX_RECENT_JOBS,
    RecentJob,
    RecentJobStatus,
    SessionDocument,
    SessionDocumentStatus,
    WorkSession,
    append_recent_jobs,
    clear_recent_jobs,
    load_recent_jobs,
    load_work_session,
    save_work_session,
)
from parsezen.pdf_conversion import PdfPageRange
from parsezen.processing import OutputFormat, ProcessRequest
from parsezen.settings import AppSettings


def test_active_session_round_trips_exact_requests_and_completed_results(tmp_path: Path) -> None:
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.md"
    result = tmp_path / "first.md"
    first.write_bytes(b"%PDF-local")
    second.write_text("Second", encoding="utf-8")
    result.write_text("Done", encoding="utf-8")
    attachments = tmp_path / "attachments"
    attachments.mkdir()
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"png")
    settings = AppSettings(
        model="qwen3:4b",
        context_window=8_192,
        output_directory=tmp_path,
        image_output_directory=attachments,
    )
    saved_at = datetime.now(UTC)
    session = WorkSession(
        settings=settings,
        documents=(
            SessionDocument(
                ProcessRequest(
                    first,
                    True,
                    output_directory=tmp_path,
                    pdf_page_range=PdfPageRange(10, 25),
                    output_format=OutputFormat.EPUB,
                    epub_title="Primer libro",
                    epub_author="Autora local",
                    epub_cover_path=cover,
                ),
                SessionDocumentStatus.COMPLETED,
                result,
            ),
            SessionDocument(
                ProcessRequest(
                    second,
                    False,
                    improvement_mode=ImprovementMode.CLEAN,
                    image_output_directory=attachments,
                ),
                SessionDocumentStatus.PAUSED,
            ),
        ),
        saved_at=saved_at,
    )
    path = tmp_path / "session.json"

    save_work_session(session, path=path)
    restored = load_work_session(path=path)

    assert restored is not None
    assert restored.settings == settings
    assert restored.documents == session.documents
    assert restored.saved_at == saved_at


def test_active_session_encrypts_and_restores_translation_glossary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(sessions_module, "_protect_for_current_user", lambda value: value)
    monkeypatch.setattr(sessions_module, "_unprotect_for_current_user", lambda value: value)
    source = tmp_path / "private.txt"
    source.write_text("content", encoding="utf-8")
    path = tmp_path / "session.json"
    request = ProcessRequest(
        source,
        convert_to_markdown=False,
        offline_translation_language="Español",
        output_format=OutputFormat.TEXT,
        glossary=(GlossaryEntry("Secret term", "Término privado"),),
    )

    save_work_session(
        WorkSession(
            AppSettings(),
            (SessionDocument(request, SessionDocumentStatus.PAUSED),),
            datetime.now(UTC),
        ),
        path=path,
    )
    restored = load_work_session(path=path)

    assert restored is not None
    assert restored.documents[0].request.glossary == request.glossary
    raw = path.read_text(encoding="utf-8")
    assert "Secret term" not in raw
    assert "Término privado" not in raw


def test_active_session_restores_image_and_first_page_cover_choices(tmp_path: Path) -> None:
    pdf = tmp_path / "book.pdf"
    markdown = tmp_path / "notes.md"
    pdf.write_bytes(b"%PDF-local")
    markdown.write_text("# Notes", encoding="utf-8")
    path = tmp_path / "session.json"
    requests = (
        ProcessRequest(
            pdf,
            True,
            output_format=OutputFormat.EPUB,
            epub_first_page_cover=True,
        ),
        ProcessRequest(markdown, True, include_images=False, preserve_styles=False),
    )

    save_work_session(
        WorkSession(
            AppSettings(),
            tuple(SessionDocument(request, SessionDocumentStatus.PAUSED) for request in requests),
            datetime.now(UTC),
        ),
        path=path,
    )

    restored = load_work_session(path=path)

    assert restored is not None
    assert tuple(document.request for document in restored.documents) == requests


def test_pending_review_resumes_safely_without_persisting_private_review_text(
    tmp_path: Path,
) -> None:
    source = tmp_path / "private-book.pdf"
    result = tmp_path / "private-book.epub"
    source.write_bytes(b"%PDF-local")
    result.write_bytes(b"epub draft")
    path = tmp_path / "session.json"
    request = ProcessRequest(source, True, output_format=OutputFormat.EPUB)

    save_work_session(
        WorkSession(
            AppSettings(),
            (
                SessionDocument(
                    request,
                    SessionDocumentStatus.REVIEW_PENDING,
                    result,
                ),
            ),
            datetime.now(UTC),
        ),
        path=path,
    )

    restored = load_work_session(path=path)

    assert restored is not None
    assert restored.documents[0].status is SessionDocumentStatus.PAUSED
    assert restored.documents[0].result_path == result


def test_active_session_is_rejected_if_a_source_changed(tmp_path: Path) -> None:
    source = tmp_path / "document.txt"
    source.write_text("First", encoding="utf-8")
    path = tmp_path / "session.json"
    save_work_session(
        WorkSession(
            AppSettings(),
            (
                SessionDocument(
                    ProcessRequest(source, True),
                    SessionDocumentStatus.PAUSED,
                ),
            ),
            datetime.now(UTC),
        ),
        path=path,
    )
    source.write_text("Changed and longer", encoding="utf-8")

    assert load_work_session(path=path) is None


def test_legacy_direct_epub_session_is_migrated_to_epub_output(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    source.write_bytes(b"legacy book")
    path = tmp_path / "session.json"
    save_work_session(
        WorkSession(
            AppSettings(),
            (
                SessionDocument(
                    ProcessRequest(
                        source,
                        convert_to_markdown=False,
                        offline_translation_language="Español",
                        output_format=OutputFormat.EPUB,
                    ),
                    SessionDocumentStatus.PAUSED,
                ),
            ),
            datetime.now(UTC),
        ),
        path=path,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = 1
    request = payload["documents"][0]["request"]
    request.pop("output_format")
    request.pop("image_output_directory")
    path.write_text(json.dumps(payload), encoding="utf-8")

    restored = load_work_session(path=path)

    assert restored is not None
    assert restored.documents[0].request.output_format is OutputFormat.EPUB


def test_legacy_epub_cleanup_session_is_migrated_to_markdown(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    source.write_bytes(b"legacy book")
    path = tmp_path / "session.json"
    save_work_session(
        WorkSession(
            AppSettings(model="local-model"),
            (
                SessionDocument(
                    ProcessRequest(
                        source,
                        convert_to_markdown=False,
                        improvement_mode=ImprovementMode.CLEAN_AND_TRANSLATE,
                        target_language="Español",
                        output_format=OutputFormat.EPUB,
                    ),
                    SessionDocumentStatus.PAUSED,
                ),
            ),
            datetime.now(UTC),
        ),
        path=path,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = 1
    request = payload["documents"][0]["request"]
    request.pop("output_format")
    request.pop("image_output_directory")
    path.write_text(json.dumps(payload), encoding="utf-8")

    restored = load_work_session(path=path)

    assert restored is not None
    migrated = restored.documents[0].request
    assert migrated.convert_to_markdown is True
    assert migrated.output_format is OutputFormat.MARKDOWN


def test_recent_jobs_are_bounded_deduplicated_and_clearable(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    now = datetime.now(UTC)
    jobs = tuple(
        RecentJob(
            source_path=tmp_path / f"document-{index}.pdf",
            status=RecentJobStatus.COMPLETED,
            finished_at=now - timedelta(minutes=index),
            result_path=tmp_path / f"document-{index}.md",
        )
        for index in range(MAX_RECENT_JOBS + 5)
    )

    append_recent_jobs(jobs, path=path)
    append_recent_jobs((jobs[0],), path=path)
    restored = load_recent_jobs(path=path)

    assert len(restored) == MAX_RECENT_JOBS
    assert restored[0] == jobs[0]
    assert restored.count(jobs[0]) == 1
    clear_recent_jobs(path=path)
    assert load_recent_jobs(path=path) == ()


def test_recent_job_round_trips_a_content_free_outcome_summary(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    job = RecentJob(
        source_path=tmp_path / "book.pdf",
        status=RecentJobStatus.COMPLETED,
        finished_at=datetime.now(UTC),
        result_path=tmp_path / "book.epub",
        summary=OutcomeSummary(
            output_format="EPUB",
            operations=("Conversión", "Edición EPUB"),
            processed_pages=100,
            preserved_images=12,
            integrity_verified=True,
            integrity_checks=3,
            duration_seconds=600,
        ),
    )

    append_recent_jobs((job,), path=path)

    assert load_recent_jobs(path=path) == (job,)
    raw = path.read_text(encoding="utf-8")
    assert "book.pdf" in raw
    assert "processed_pages" in raw
    assert "document content" not in raw
