"""Throttle durable queue snapshots without involving Qt."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic
from typing import Protocol

from parsezen.domain.jobs import DocumentJob


class QueueRepository(Protocol):
    def replace_jobs(self, jobs: tuple[DocumentJob, ...]) -> None: ...


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
    ) -> QueuePersistenceResult:
        if self._unavailable:
            return QueuePersistenceResult(QueuePersistenceStatus.UNAVAILABLE)

        now = self._clock()
        changed = jobs != self._last_jobs
        due = now - self._last_saved_at >= self._interval_seconds
        if not (force or self._retry_pending or changed and due):
            return QueuePersistenceResult(QueuePersistenceStatus.SKIPPED)

        try:
            self._repository.replace_jobs(jobs)
        except (OSError, RuntimeError):
            self._retry_pending = True
            return QueuePersistenceResult(QueuePersistenceStatus.FAILED)

        self._retry_pending = False
        self._last_jobs = jobs
        self._last_saved_at = now
        return QueuePersistenceResult(QueuePersistenceStatus.SAVED)
