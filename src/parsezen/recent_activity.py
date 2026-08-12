"""Bounded, content-free local history of completed attempts."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from tempfile import mkstemp
from typing import Any, cast

from platformdirs import user_data_path

from parsezen import APP_STORAGE_NAME
from parsezen.domain.attempt_activity import (
    MAX_ATTEMPT_EVENT_MESSAGE,
    MAX_ATTEMPT_EVENT_TIMESTAMP,
    MAX_ATTEMPT_EVENTS,
    MAX_ATTEMPT_ID,
    MAX_DIAGNOSTIC_REFERENCE,
    MAX_ERROR_CODE,
    MAX_FAILURE_MESSAGE,
    AttemptEvent,
    AttemptEventStatus,
    AttemptPhase,
    AttemptTimeline,
    FailureSnapshot,
    ReusableWork,
    durable_failure_message,
    is_safe_token,
)
from parsezen.domain.outcomes import OutcomeSummary

LOGGER = logging.getLogger(__name__)

_HISTORY_SCHEMA_VERSION = 3
_MAX_JOURNAL_BYTES = 1024 * 1024
_MAX_PATH_CHARACTERS = 32_767
MAX_RECENT_JOBS = 20


class RecentJobStatus(StrEnum):
    """Small user-facing outcome retained in recent activity."""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class RecentJob:
    source_path: Path
    status: RecentJobStatus
    finished_at: datetime
    result_path: Path | None = None
    summary: OutcomeSummary | None = None
    attempt_id: str | None = None
    timeline: AttemptTimeline = field(default_factory=AttemptTimeline)
    failure: FailureSnapshot | None = None

    @property
    def phase_timeline(self) -> AttemptTimeline:
        return self.timeline

    @property
    def failure_snapshot(self) -> FailureSnapshot | None:
        return self.failure


def get_history_path() -> Path:
    return user_data_path(APP_STORAGE_NAME, appauthor=False) / "recent-jobs.json"


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
        or raw.get("schema_version") not in {1, 2, _HISTORY_SCHEMA_VERSION}
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


def _recent_job_json(job: RecentJob) -> dict[str, Any]:
    return {
        "source_path": _path_text(job.source_path),
        "status": job.status.value,
        "finished_at": _datetime_text(job.finished_at),
        "result_path": _optional_path_text(job.result_path),
        "summary": _outcome_summary_json(job.summary),
        "attempt_id": _optional_attempt_id_text(job.attempt_id),
        "timeline": _attempt_timeline_json(job.timeline),
        "failure": _failure_snapshot_json(job.failure),
    }


def _recent_job_from_json(raw: object) -> RecentJob:
    required = {"source_path", "status", "finished_at", "result_path"}
    optional = {"summary", "attempt_id", "timeline", "failure"}
    if not isinstance(raw, dict) or not required.issubset(raw) or set(raw) - required - optional:
        raise ValueError
    base = RecentJob(
        source_path=_required_path(raw["source_path"]),
        status=RecentJobStatus(raw["status"]),
        finished_at=_parse_datetime(raw["finished_at"]),
        result_path=_optional_path(raw["result_path"]),
        summary=_outcome_summary_from_json(raw.get("summary")),
    )
    try:
        attempt_id = _optional_attempt_id(raw.get("attempt_id"))
    except (TypeError, ValueError):
        attempt_id = None
    try:
        timeline = _attempt_timeline_from_json(raw.get("timeline"))
    except (TypeError, ValueError):
        timeline = AttemptTimeline()
    try:
        failure = _failure_snapshot_from_json(raw.get("failure"))
    except (TypeError, ValueError):
        failure = None
    if failure is not None and base.status is not RecentJobStatus.FAILED:
        failure = None
    return replace(base, attempt_id=attempt_id, timeline=timeline, failure=failure)


def _attempt_timeline_json(timeline: AttemptTimeline) -> list[dict[str, str | None]]:
    if not isinstance(timeline, AttemptTimeline):
        raise ValueError
    if len(timeline.events) > MAX_ATTEMPT_EVENTS:
        raise ValueError
    return [
        {
            "phase": event.phase.value,
            "status": event.status.value,
            "timestamp": _attempt_timestamp_text(event.timestamp),
            "message": event.message,
        }
        for event in timeline.events
    ]


def _attempt_timeline_from_json(raw: object) -> AttemptTimeline:
    if raw is None:
        return AttemptTimeline()
    if not isinstance(raw, list) or len(raw) > MAX_ATTEMPT_EVENTS:
        raise ValueError
    events: list[AttemptEvent] = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {
            "phase",
            "status",
            "timestamp",
            "message",
        }:
            raise ValueError
        timestamp = _attempt_timestamp_from_json(item["timestamp"])
        message = item["message"]
        if message is not None and (
            not isinstance(message, str)
            or len(message) > MAX_ATTEMPT_EVENT_MESSAGE
            or any(character in message for character in "\r\n\0")
        ):
            raise ValueError
        events.append(
            AttemptEvent(
                phase=AttemptPhase(item["phase"]),
                status=AttemptEventStatus(item["status"]),
                message=message,
                timestamp=timestamp,
            )
        )
    return AttemptTimeline(tuple(events))


def _failure_snapshot_json(failure: FailureSnapshot | None) -> dict[str, str | None] | None:
    if failure is None:
        return None
    if not isinstance(failure, FailureSnapshot):
        raise ValueError
    return {
        "phase": failure.phase.value,
        "error_code": failure.error_code,
        "message": durable_failure_message(failure.error_code),
        "diagnostic_reference": failure.diagnostic_reference,
        "reusable_work": failure.reusable_work.value,
    }


def _failure_snapshot_from_json(raw: object) -> FailureSnapshot | None:
    if raw is None:
        return None
    fields = {"phase", "error_code", "message", "diagnostic_reference", "reusable_work"}
    if not isinstance(raw, dict) or set(raw) != fields:
        raise ValueError
    phase = raw["phase"]
    error_code = raw["error_code"]
    message = raw["message"]
    reference = raw["diagnostic_reference"]
    reusable_work = raw["reusable_work"]
    if (
        not isinstance(error_code, str)
        or len(error_code) > MAX_ERROR_CODE
        or not is_safe_token(error_code, maximum=MAX_ERROR_CODE)
        or not isinstance(message, str)
        or len(message) > MAX_FAILURE_MESSAGE
        or any(character in message for character in "\r\n\0")
        or not message.strip()
        or reference is not None
        and (
            not isinstance(reference, str)
            or len(reference) > MAX_DIAGNOSTIC_REFERENCE
            or not is_safe_token(reference, maximum=MAX_DIAGNOSTIC_REFERENCE)
        )
    ):
        raise ValueError
    return FailureSnapshot(
        phase=AttemptPhase(phase),
        error_code=error_code,
        message=durable_failure_message(error_code),
        diagnostic_reference=reference,
        reusable_work=ReusableWork(reusable_work),
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
        "ai_review_recommended": summary.ai_review_recommended,
        "ai_review_blocks": summary.ai_review_blocks,
        "ai_review_signals": summary.ai_review_signals,
    }


def _outcome_summary_from_json(raw: object) -> OutcomeSummary | None:
    if raw is None:
        return None
    required_fields = {
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
    optional_fields = {
        "ai_review_recommended",
        "ai_review_blocks",
        "ai_review_signals",
    }
    if (
        not isinstance(raw, dict)
        or not required_fields.issubset(raw)
        or not set(raw).issubset(required_fields | optional_fields)
    ):
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
    if "ai_review_recommended" in raw and not isinstance(raw["ai_review_recommended"], bool):
        raise ValueError
    numeric_fields = required_fields - {
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
        ai_review_recommended=bool(raw.get("ai_review_recommended", False)),
        ai_review_blocks=cast(
            int,
            _summary_nonnegative_int(raw.get("ai_review_blocks", 0), optional=False),
        ),
        ai_review_signals=cast(
            int,
            _summary_nonnegative_int(raw.get("ai_review_signals", 0), optional=False),
        ),
        **cast(Any, values),
    )


def _summary_nonnegative_int(value: object, *, optional: bool) -> int | None:
    if value is None:
        if optional:
            return None
        raise ValueError
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


def _optional_attempt_id_text(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) > MAX_ATTEMPT_ID
        or not is_safe_token(
            value,
            maximum=MAX_ATTEMPT_ID,
        )
    ):
        raise ValueError
    return value


def _optional_attempt_id(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError
    return _optional_attempt_id_text(value)


def _datetime_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError
    return value.astimezone(UTC).isoformat()


def _attempt_timestamp_text(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise ValueError
    try:
        if value.utcoffset() is None:
            raise ValueError
        timestamp = value.astimezone(UTC)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError from exc
    if timestamp.utcoffset() is None:
        raise ValueError
    text = timestamp.isoformat()
    if len(text) > MAX_ATTEMPT_EVENT_TIMESTAMP:
        raise ValueError
    return text


def _attempt_timestamp_from_json(value: object) -> datetime:
    if (
        not isinstance(value, str)
        or len(value) > MAX_ATTEMPT_EVENT_TIMESTAMP
        or any(character in value for character in "\r\n\0")
    ):
        raise ValueError
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError from exc
    try:
        if parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(UTC)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError from exc


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError
    return parsed.astimezone(UTC)
