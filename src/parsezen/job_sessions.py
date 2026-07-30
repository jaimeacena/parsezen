"""Small local journal for recoverable queues and recent completed jobs."""

from __future__ import annotations

import json
import logging
import os
from base64 import b64decode, b64encode
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from tempfile import mkstemp
from typing import Any, cast

from platformdirs import user_data_path

from parsezen import APP_STORAGE_NAME
from parsezen.domain.outcomes import OutcomeSummary
from parsezen.epub_checkpoints import (
    _protect_for_current_user,
    _unprotect_for_current_user,
)
from parsezen.glossary import GlossaryEntry, validate_glossary
from parsezen.improvement import ImprovementMode
from parsezen.pdf_conversion import PdfPageRange
from parsezen.processing import OutputFormat, ProcessRequest
from parsezen.settings import AppSettings, validate_settings

LOGGER = logging.getLogger(__name__)

_SESSION_SCHEMA_VERSION = 7
_HISTORY_SCHEMA_VERSION = 2
_MAX_JOURNAL_BYTES = 1024 * 1024
_MAX_PATH_CHARACTERS = 32_767
MAX_RECENT_JOBS = 20


class SessionDocumentStatus(StrEnum):
    """Durable status of one document in an interrupted queue."""

    PENDING = "pending"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REVIEW_PENDING = "review_pending"


class RecentJobStatus(StrEnum):
    """Small user-facing outcome retained in recent activity."""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class SessionDocument:
    request: ProcessRequest
    status: SessionDocumentStatus
    result_path: Path | None = None


@dataclass(frozen=True, slots=True)
class WorkSession:
    settings: AppSettings
    documents: tuple[SessionDocument, ...]
    saved_at: datetime


@dataclass(frozen=True, slots=True)
class RecentJob:
    source_path: Path
    status: RecentJobStatus
    finished_at: datetime
    result_path: Path | None = None
    summary: OutcomeSummary | None = None


def get_session_path() -> Path:
    return user_data_path(APP_STORAGE_NAME, appauthor=False) / "active-session.json"


def get_history_path() -> Path:
    return user_data_path(APP_STORAGE_NAME, appauthor=False) / "recent-jobs.json"


def save_work_session(session: WorkSession, *, path: Path | None = None) -> None:
    """Atomically persist an active queue without storing document content."""
    destination = path if path is not None else get_session_path()
    if not session.documents:
        clear_work_session(path=destination)
        return
    settings = validate_settings(session.settings)
    payload = {
        "schema_version": _SESSION_SCHEMA_VERSION,
        "saved_at": _datetime_text(session.saved_at),
        "settings": _settings_json(settings),
        "documents": [_session_document_json(document) for document in session.documents],
    }
    _atomic_json_write(destination, payload)


def load_work_session(*, path: Path | None = None) -> WorkSession | None:
    """Load a strict, still-recoverable queue; invalid journals are ignored."""
    source = path if path is not None else get_session_path()
    raw = _read_json(source)
    if raw is None:
        return None
    try:
        if not isinstance(raw, dict) or set(raw) != {
            "schema_version",
            "saved_at",
            "settings",
            "documents",
        }:
            raise ValueError
        if raw["schema_version"] not in {1, 2, 3, 4, 5, 6, _SESSION_SCHEMA_VERSION}:
            raise ValueError
        settings = _settings_from_json(raw["settings"])
        raw_documents = raw["documents"]
        if not isinstance(raw_documents, list) or not 1 <= len(raw_documents) <= 100:
            raise ValueError
        documents_list: list[SessionDocument] = []
        for item in raw_documents:
            try:
                documents_list.append(_session_document_from_json(item))
            except (OSError, TypeError, ValueError):
                continue
        if not documents_list:
            raise ValueError
        documents = tuple(documents_list)
        return WorkSession(
            settings=settings,
            documents=documents,
            saved_at=_parse_datetime(raw["saved_at"]),
        )
    except (KeyError, TypeError, ValueError):
        LOGGER.warning("active_session_invalid")
        return None


def clear_work_session(*, path: Path | None = None) -> None:
    destination = path if path is not None else get_session_path()
    try:
        destination.unlink(missing_ok=True)
    except OSError:
        LOGGER.warning("active_session_cleanup_failed")


def append_recent_jobs(
    jobs: tuple[RecentJob, ...],
    *,
    path: Path | None = None,
) -> None:
    """Prepend outcomes, deduplicate exact attempts and retain a small bounded history."""
    if not jobs:
        return
    destination = path if path is not None else get_history_path()
    existing = load_recent_jobs(path=destination)
    combined: list[RecentJob] = []
    seen: set[tuple[str, str, str | None]] = set()
    for job in (*jobs, *existing):
        key = (
            str(job.source_path.resolve(strict=False)).casefold(),
            job.status.value,
            str(job.result_path.resolve(strict=False)).casefold()
            if job.result_path is not None
            else None,
        )
        if key in seen:
            continue
        seen.add(key)
        combined.append(job)
        if len(combined) >= MAX_RECENT_JOBS:
            break
    payload = {
        "schema_version": _HISTORY_SCHEMA_VERSION,
        "jobs": [_recent_job_json(job) for job in combined],
    }
    _atomic_json_write(destination, payload)


def load_recent_jobs(*, path: Path | None = None) -> tuple[RecentJob, ...]:
    source = path if path is not None else get_history_path()
    raw = _read_json(source)
    if raw is None:
        return ()
    if (
        not isinstance(raw, dict)
        or set(raw) != {"schema_version", "jobs"}
        or raw.get("schema_version") not in {1, _HISTORY_SCHEMA_VERSION}
        or not isinstance(raw.get("jobs"), list)
    ):
        return ()
    jobs: list[RecentJob] = []
    for item in raw["jobs"][:MAX_RECENT_JOBS]:
        try:
            jobs.append(_recent_job_from_json(item))
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(jobs)


def clear_recent_jobs(*, path: Path | None = None) -> None:
    destination = path if path is not None else get_history_path()
    try:
        destination.unlink(missing_ok=True)
    except OSError:
        LOGGER.warning("recent_jobs_cleanup_failed")


def _session_document_json(document: SessionDocument) -> dict[str, Any]:
    request = document.request
    source_stat = request.source_path.stat()
    return {
        "status": document.status.value,
        "result_path": _optional_path_text(document.result_path),
        "source_size": source_stat.st_size,
        "source_mtime_ns": source_stat.st_mtime_ns,
        "request": {
            "source_path": _path_text(request.source_path),
            "convert_to_markdown": request.convert_to_markdown,
            "output_directory": _optional_path_text(request.output_directory),
            "improvement_mode": (
                request.improvement_mode.value if request.improvement_mode is not None else None
            ),
            "target_language": request.target_language,
            "offline_translation_language": request.offline_translation_language,
            "pdf_page_range": (
                [request.pdf_page_range.first_page, request.pdf_page_range.last_page]
                if request.pdf_page_range is not None
                else None
            ),
            "force_pdf_ocr": request.force_pdf_ocr,
            "output_format": request.output_format.value,
            "include_images": request.include_images,
            "preserve_styles": request.preserve_styles,
            "image_output_directory": _optional_path_text(request.image_output_directory),
            "epub_title": request.epub_title,
            "epub_author": request.epub_author,
            "epub_cover_path": _optional_path_text(request.epub_cover_path),
            "epub_first_page_cover": request.epub_first_page_cover,
            "epub_remove_cover": request.epub_remove_cover,
            "review_content": request.review_content,
            "review_structure": request.review_structure,
            "protected_glossary": _protected_glossary_text(request.glossary),
        },
    }


def _session_document_from_json(raw: object) -> SessionDocument:
    if not isinstance(raw, dict) or set(raw) != {
        "status",
        "result_path",
        "source_size",
        "source_mtime_ns",
        "request",
    }:
        raise ValueError
    request = _request_from_json(raw["request"])
    source_stat = request.source_path.stat()
    if (
        raw["source_size"] != source_stat.st_size
        or raw["source_mtime_ns"] != source_stat.st_mtime_ns
    ):
        raise ValueError
    status = SessionDocumentStatus(raw["status"])
    if status is SessionDocumentStatus.COMPLETED:
        result_path = _optional_path(raw["result_path"])
        if result_path is None or not result_path.is_file():
            raise ValueError
    else:
        result_path = _optional_path(raw["result_path"])
        if status not in {SessionDocumentStatus.PENDING, SessionDocumentStatus.CANCELLED}:
            # Review drafts contain private document text and intentionally do
            # not live in this small journal. Reopening therefore resumes from
            # the encrypted processing checkpoints and reconstructs the exact
            # review instead of exposing it in plain JSON.
            status = SessionDocumentStatus.PAUSED
    return SessionDocument(request=request, status=status, result_path=result_path)


def _request_from_json(raw: object) -> ProcessRequest:
    required_fields = {
        "source_path",
        "convert_to_markdown",
        "output_directory",
        "improvement_mode",
        "target_language",
        "offline_translation_language",
        "pdf_page_range",
        "force_pdf_ocr",
    }
    optional_fields = {
        "output_format",
        "include_images",
        "preserve_styles",
        "image_output_directory",
        "epub_title",
        "epub_author",
        "epub_cover_path",
        "epub_first_page_cover",
        "epub_remove_cover",
        "review_content",
        "review_structure",
        "protected_glossary",
    }
    if (
        not isinstance(raw, dict)
        or not required_fields.issubset(raw)
        or not set(raw).issubset(required_fields | optional_fields)
    ):
        raise ValueError
    source_path = _required_path(raw["source_path"])
    if not source_path.is_file():
        raise ValueError
    convert_to_markdown = raw["convert_to_markdown"]
    force_pdf_ocr = raw["force_pdf_ocr"]
    include_images = raw.get("include_images", True)
    preserve_styles = raw.get("preserve_styles", True)
    first_page_cover = raw.get("epub_first_page_cover", False)
    remove_cover = raw.get("epub_remove_cover", False)
    review_content = raw.get("review_content", False)
    review_structure = raw.get("review_structure", False)
    if (
        not isinstance(convert_to_markdown, bool)
        or not isinstance(force_pdf_ocr, bool)
        or not isinstance(include_images, bool)
        or not isinstance(preserve_styles, bool)
        or not isinstance(first_page_cover, bool)
        or not isinstance(remove_cover, bool)
        or not isinstance(review_content, bool)
        or not isinstance(review_structure, bool)
    ):
        raise ValueError
    mode_value = raw["improvement_mode"]
    mode = ImprovementMode(mode_value) if mode_value is not None else None
    page_value = raw["pdf_page_range"]
    page_range = None
    if page_value is not None:
        if (
            not isinstance(page_value, list)
            or len(page_value) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) for value in page_value)
        ):
            raise ValueError
        page_range = PdfPageRange(page_value[0], page_value[1])
    output_format_value = raw.get("output_format", OutputFormat.MARKDOWN.value)
    legacy_direct_epub = (
        "output_format" not in raw
        and source_path.suffix.lower() == ".epub"
        and not convert_to_markdown
        and (
            _optional_text(raw["offline_translation_language"]) is not None
            or mode in {ImprovementMode.TRANSLATE, ImprovementMode.CLEAN_AND_TRANSLATE}
        )
    )
    if legacy_direct_epub:
        if mode in {ImprovementMode.CLEAN, ImprovementMode.CLEAN_AND_TRANSLATE}:
            convert_to_markdown = True
        else:
            output_format_value = OutputFormat.EPUB.value
    epub_cover_path = _optional_path(raw.get("epub_cover_path"))
    if epub_cover_path is not None and not epub_cover_path.is_file():
        raise ValueError
    return ProcessRequest(
        source_path=source_path,
        convert_to_markdown=convert_to_markdown,
        output_directory=_optional_path(raw["output_directory"]),
        improvement_mode=mode,
        target_language=_optional_text(raw["target_language"]),
        offline_translation_language=_optional_text(raw["offline_translation_language"]),
        pdf_page_range=page_range,
        force_pdf_ocr=force_pdf_ocr,
        output_format=OutputFormat(output_format_value),
        include_images=include_images,
        preserve_styles=preserve_styles,
        image_output_directory=_optional_path(raw.get("image_output_directory")),
        epub_title=_optional_text(raw.get("epub_title")),
        epub_author=_optional_text(raw.get("epub_author")),
        epub_cover_path=epub_cover_path,
        epub_first_page_cover=first_page_cover,
        epub_remove_cover=remove_cover,
        review_content=review_content,
        review_structure=review_structure,
        glossary=_glossary_from_protected_text(raw.get("protected_glossary")),
    )


def _protected_glossary_text(entries: tuple[GlossaryEntry, ...]) -> str | None:
    normalized = validate_glossary(entries)
    if not normalized:
        return None
    encoded = json.dumps(
        [[entry.source, entry.target] for entry in normalized],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return b64encode(_protect_for_current_user(encoded)).decode("ascii")


def _glossary_from_protected_text(value: object) -> tuple[GlossaryEntry, ...]:
    if value is None:
        return ()
    if not isinstance(value, str) or not value:
        raise ValueError
    try:
        decoded = _unprotect_for_current_user(b64decode(value, validate=True))
        raw = json.loads(decoded.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError from exc
    if not isinstance(raw, list):
        raise ValueError
    entries: list[GlossaryEntry] = []
    for item in raw:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
        ):
            raise ValueError
        entries.append(GlossaryEntry(item[0], item[1]))
    try:
        return validate_glossary(tuple(entries))
    except Exception as exc:
        raise ValueError from exc


def _settings_json(settings: AppSettings) -> dict[str, Any]:
    return {
        "model": settings.model,
        "context_window": settings.context_window,
        "output_directory": _optional_path_text(settings.output_directory),
        "image_output_directory": _optional_path_text(settings.image_output_directory),
        "timeout_seconds": settings.timeout_seconds,
        "checkpoint_retention_days": settings.checkpoint_retention_days,
    }


def _settings_from_json(raw: object) -> AppSettings:
    required_fields = {
        "model",
        "context_window",
        "output_directory",
        "timeout_seconds",
    }
    optional_fields = {"image_output_directory", "checkpoint_retention_days"}
    if (
        not isinstance(raw, dict)
        or not required_fields.issubset(raw)
        or set(raw) - required_fields - optional_fields
    ):
        raise ValueError
    return validate_settings(
        AppSettings(
            model=_optional_text(raw["model"]),
            context_window=raw["context_window"],
            output_directory=_optional_path(raw["output_directory"]),
            image_output_directory=_optional_path(raw.get("image_output_directory")),
            timeout_seconds=raw["timeout_seconds"],
            checkpoint_retention_days=raw.get("checkpoint_retention_days", 30),
        )
    )


def _recent_job_json(job: RecentJob) -> dict[str, Any]:
    return {
        "source_path": _path_text(job.source_path),
        "status": job.status.value,
        "finished_at": _datetime_text(job.finished_at),
        "result_path": _optional_path_text(job.result_path),
        "summary": _outcome_summary_json(job.summary),
    }


def _recent_job_from_json(raw: object) -> RecentJob:
    required = {"source_path", "status", "finished_at", "result_path"}
    if not isinstance(raw, dict) or not required.issubset(raw) or set(raw) - required - {"summary"}:
        raise ValueError
    return RecentJob(
        source_path=_required_path(raw["source_path"]),
        status=RecentJobStatus(raw["status"]),
        finished_at=_parse_datetime(raw["finished_at"]),
        result_path=_optional_path(raw["result_path"]),
        summary=_outcome_summary_from_json(raw.get("summary")),
    )


def _outcome_summary_json(summary: OutcomeSummary | None) -> dict[str, Any] | None:
    if summary is None:
        return None
    return {
        "output_format": summary.output_format,
        "operations": list(summary.operations),
        "processed_pages": summary.processed_pages,
        "ocr_pages": summary.ocr_pages,
        "preserved_images": summary.preserved_images,
        "chapters": summary.chapters,
        "conversion_issues": summary.conversion_issues,
        "translation_issues": summary.translation_issues,
        "preserved_segments": summary.preserved_segments,
        "review_units": summary.review_units,
        "review_changes": summary.review_changes,
        "review_edits": summary.review_edits,
        "review_originals": summary.review_originals,
        "editor_completed": summary.editor_completed,
        "manual_review_expected": summary.manual_review_expected,
        "integrity_verified": summary.integrity_verified,
        "integrity_checks": summary.integrity_checks,
        "integrity_warnings": summary.integrity_warnings,
        "duration_seconds": summary.duration_seconds,
        "estimate_lower_seconds": summary.estimate_lower_seconds,
        "estimate_upper_seconds": summary.estimate_upper_seconds,
        "early_check_pages": summary.early_check_pages,
        "early_check_warnings": summary.early_check_warnings,
    }


def _outcome_summary_from_json(raw: object) -> OutcomeSummary | None:
    if raw is None:
        return None
    fields = {
        "output_format",
        "operations",
        "processed_pages",
        "ocr_pages",
        "preserved_images",
        "chapters",
        "conversion_issues",
        "translation_issues",
        "preserved_segments",
        "review_units",
        "review_changes",
        "review_edits",
        "review_originals",
        "editor_completed",
        "manual_review_expected",
        "integrity_verified",
        "integrity_checks",
        "integrity_warnings",
        "duration_seconds",
        "estimate_lower_seconds",
        "estimate_upper_seconds",
        "early_check_pages",
        "early_check_warnings",
    }
    if not isinstance(raw, dict) or set(raw) != fields:
        raise ValueError
    output_format = raw["output_format"]
    operations = raw["operations"]
    if (
        not isinstance(output_format, str)
        or not output_format
        or len(output_format) > 16
        or not isinstance(operations, list)
        or len(operations) > 10
        or any(
            not isinstance(operation, str)
            or not operation
            or len(operation) > 64
            or any(character in operation for character in "\r\n\0")
            for operation in operations
        )
    ):
        raise ValueError
    boolean_fields = (
        "editor_completed",
        "manual_review_expected",
        "integrity_verified",
    )
    if any(not isinstance(raw[field], bool) for field in boolean_fields):
        raise ValueError
    numeric_fields = fields - {
        "output_format",
        "operations",
        *boolean_fields,
    }
    values = {
        field: _summary_nonnegative_int(
            raw[field],
            optional=field
            in {
                "duration_seconds",
                "estimate_lower_seconds",
                "estimate_upper_seconds",
            },
        )
        for field in numeric_fields
    }
    return OutcomeSummary(
        output_format=output_format,
        operations=tuple(operations),
        editor_completed=raw["editor_completed"],
        manual_review_expected=raw["manual_review_expected"],
        integrity_verified=raw["integrity_verified"],
        **cast(Any, values),
    )


def _summary_nonnegative_int(value: object, *, optional: bool) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2_147_483_647:
        raise ValueError
    return value


def _atomic_json_write(path: Path, payload: object) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    if len(encoded) > _MAX_JOURNAL_BYTES:
        raise OSError("El registro local supera el tamaño permitido.")
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = mkstemp(
            dir=path.parent,
            prefix=f".{path.stem}-",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _read_json(path: Path) -> object | None:
    try:
        if path.is_symlink() or path.stat().st_size > _MAX_JOURNAL_BYTES:
            return None
        return cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None


def _path_text(path: Path) -> str:
    value = str(path)
    if not value or len(value) > _MAX_PATH_CHARACTERS or "\0" in value:
        raise ValueError
    return value


def _optional_path_text(path: Path | None) -> str | None:
    return _path_text(path) if path is not None else None


def _required_path(value: object) -> Path:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_PATH_CHARACTERS
        or "\0" in value
    ):
        raise ValueError
    return Path(value)


def _optional_path(value: object) -> Path | None:
    return None if value is None else _required_path(value)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or any(char in value for char in "\r\n\0"):
        raise ValueError
    return value.strip()


def _datetime_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError
    return value.astimezone(UTC).isoformat()


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError
    return parsed.astimezone(UTC)
