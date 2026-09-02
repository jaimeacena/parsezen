"""Typed, content-free audit events for durable document jobs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from parsezen.domain.attempt_activity import is_safe_token
from parsezen.domain.stages import StageKind, StageStatus

JOB_EVENT_PAYLOAD_VERSION = 1


class JobEventKind(StrEnum):
    """Stable event names stored in the SQLite audit history."""

    CONFIGURATION_CHANGED = "configuration_changed"
    STAGE_TRANSITION = "stage_transition"
    SNAPSHOT_SAVED = "snapshot_saved"


@dataclass(frozen=True, slots=True)
class JobEvent:
    """One versioned audit fact; it never carries document text or paths."""

    job_id: str
    kind: JobEventKind
    configuration_revision: int
    stage: StageKind | None = None
    from_status: StageStatus | None = None
    to_status: StageStatus | None = None
    attempt_id: str | None = None
    error_code: str | None = None
    snapshot_generation: str | None = None
    payload_version: int = JOB_EVENT_PAYLOAD_VERSION

    def __post_init__(self) -> None:
        if not is_safe_token(self.job_id):
            raise ValueError("A job event requires a compact job id.")
        if self.payload_version != JOB_EVENT_PAYLOAD_VERSION:
            raise ValueError("The job event payload version is unsupported.")
        if self.configuration_revision < 1:
            raise ValueError("A job event requires a positive configuration revision.")
        statuses = (self.from_status, self.to_status)
        if self.kind is JobEventKind.STAGE_TRANSITION:
            if self.stage is None or any(status is None for status in statuses):
                raise ValueError("A stage transition requires its stage and both statuses.")
            if self.from_status is self.to_status:
                raise ValueError("A stage transition must change status.")
        elif self.stage is not None or any(status is not None for status in statuses):
            raise ValueError("Only stage transitions can carry stage statuses.")
        if self.attempt_id is not None and not is_safe_token(self.attempt_id):
            raise ValueError("The job event attempt id is invalid.")
        if self.error_code is not None and not is_safe_token(self.error_code, maximum=64):
            raise ValueError("The job event error code is invalid.")
        if self.snapshot_generation is not None and not is_safe_token(
            self.snapshot_generation,
            maximum=128,
        ):
            raise ValueError("The snapshot generation is invalid.")
        if self.kind is JobEventKind.SNAPSHOT_SAVED:
            if self.snapshot_generation is None:
                raise ValueError("A snapshot event requires its generation.")
        elif self.snapshot_generation is not None:
            raise ValueError("Only snapshot events can carry a generation.")


__all__ = [
    "JOB_EVENT_PAYLOAD_VERSION",
    "JobEvent",
    "JobEventKind",
]
