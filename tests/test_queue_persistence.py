from __future__ import annotations

from dataclasses import dataclass, field

from parsezen.application.queue_persistence import (
    QueuePersistenceCoordinator,
    QueuePersistenceStatus,
)
from parsezen.domain.jobs import DocumentJob


@dataclass
class QueueRepositoryStub:
    saved: list[tuple[DocumentJob, ...]] = field(default_factory=list)
    failures_remaining: int = 0

    def replace_jobs(self, jobs: tuple[DocumentJob, ...]) -> None:
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("unavailable")
        self.saved.append(jobs)


def test_queue_persistence_throttles_unchanged_snapshots() -> None:
    repository = QueueRepositoryStub()
    now = [10.0]
    coordinator = QueuePersistenceCoordinator(
        repository,
        interval_seconds=1.0,
        clock=lambda: now[0],
    )

    assert coordinator.persist((), force=True).status is QueuePersistenceStatus.SAVED
    assert coordinator.persist(()).status is QueuePersistenceStatus.SKIPPED
    assert repository.saved == [()]


def test_queue_persistence_retries_a_failed_snapshot_without_waiting_for_a_change() -> None:
    repository = QueueRepositoryStub(failures_remaining=1)
    coordinator = QueuePersistenceCoordinator(repository, clock=lambda: 10.0)

    failed = coordinator.persist((), force=True)
    recovered = coordinator.persist(())

    assert failed.status is QueuePersistenceStatus.FAILED
    assert recovered.status is QueuePersistenceStatus.SAVED
    assert repository.saved == [()]


def test_queue_persistence_can_block_writes_after_an_unrecoverable_restore() -> None:
    repository = QueueRepositoryStub()
    coordinator = QueuePersistenceCoordinator(repository)
    coordinator.mark_unavailable()

    result = coordinator.persist((), force=True)

    assert result.status is QueuePersistenceStatus.UNAVAILABLE
    assert not result.successful
    assert repository.saved == []

    coordinator.mark_available()
    assert coordinator.persist((), force=True).successful
