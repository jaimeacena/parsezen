"""Throttle durable queue snapshots without involving Qt."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic
from typing import Protocol

from parsezen.domain.job_events import JobEvent, JobEventKind
from parsezen.domain.jobs import DocumentJob


class QueueRepository(Protocol):
    def replace_jobs(
        self,
        jobs: tuple[DocumentJob, ...],
        *,
        events: tuple[JobEvent, ...] = (),
    ) -> None: ...


class QueuePersistenceStatus(StrEnum):
    SKIPPED = "skipped"
    SAVED = "saved"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class QueuePersistenceResult:
    status: QueuePersistenceStatus

    @property
    def successful(self) -> bool:
        return self.status in {
            QueuePersistenceStatus.SKIPPED,
            QueuePersistenceStatus.SAVED,
        }


class QueuePersistenceCoordinator:
    """Own retry and throttling state for one durable queue projection."""

    def __init__(
        self,
        repository: QueueRepository,
        *,
        interval_seconds: float = 1.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if interval_seconds < 0:
            raise ValueError("The persistence interval cannot be negative.")
        self._repository = repository
        self._interval_seconds = interval_seconds
        self._clock = clock
        self._last_jobs: tuple[DocumentJob, ...] = ()
        self._observed_jobs: tuple[DocumentJob, ...] | None = None
        self._pending_events: list[JobEvent] = []
        self._last_saved_at = 0.0
        self._retry_pending = False
        self._unavailable = False

    @property
    def unavailable(self) -> bool:
        return self._unavailable

    def mark_unavailable(self) -> None:
        self._unavailable = True

    def mark_available(self) -> None:
        self._unavailable = False

    def persist(
        self,
        jobs: tuple[DocumentJob, ...],
        *,
        force: bool = False,
        attempt_ids: Mapping[str, str] | None = None,
    ) -> QueuePersistenceResult:
        if self._unavailable:
            return QueuePersistenceResult(QueuePersistenceStatus.UNAVAILABLE)

        if self._observed_jobs is not None:
            self._pending_events.extend(
                _audit_changes(
                    self._observed_jobs,
                    jobs,
                    attempt_ids=attempt_ids or {},
                )
            )
        self._observed_jobs = jobs
        retained_ids = {job.id for job in jobs}
        self._pending_events = [
            event for event in self._pending_events if event.job_id in retained_ids
        ]

        now = self._clock()
        changed = jobs != self._last_jobs
        due = now - self._last_saved_at >= self._interval_seconds
        if not (force or self._retry_pending or changed and due):
            return QueuePersistenceResult(QueuePersistenceStatus.SKIPPED)

        try:
            self._repository.replace_jobs(jobs, events=tuple(self._pending_events))
        except (OSError, RuntimeError):
            self._retry_pending = True
            return QueuePersistenceResult(QueuePersistenceStatus.FAILED)

        self._retry_pending = False
        self._pending_events.clear()
        self._last_jobs = jobs
        self._last_saved_at = now
        return QueuePersistenceResult(QueuePersistenceStatus.SAVED)


def _audit_changes(
    previous_jobs: tuple[DocumentJob, ...],
    jobs: tuple[DocumentJob, ...],
    *,
    attempt_ids: Mapping[str, str],
) -> tuple[JobEvent, ...]:
    """Project observable queue mutations into content-free audit events."""

    previous_by_id = {job.id: job for job in previous_jobs}
    events: list[JobEvent] = []
    for job in jobs:
        previous = previous_by_id.get(job.id)
        if previous is None:
            continue
        attempt_id = attempt_ids.get(job.id)
        if previous.configuration_revision != job.configuration_revision:
            events.append(
                JobEvent(
                    job.id,
                    JobEventKind.CONFIGURATION_CHANGED,
                    job.configuration_revision,
                    attempt_id=attempt_id,
                )
            )
        for old_stage, new_stage in zip(previous.stages, job.stages, strict=True):
            if old_stage.status is new_stage.status:
                continue
            events.append(
                JobEvent(
                    job.id,
                    JobEventKind.STAGE_TRANSITION,
                    job.configuration_revision,
                    stage=new_stage.kind,
                    from_status=old_stage.status,
                    to_status=new_stage.status,
                    attempt_id=attempt_id,
                    error_code=new_stage.error_code,
                )
            )
    return tuple(events)
