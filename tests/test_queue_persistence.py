from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from parsezen.application.queue_persistence import (
    QueuePersistenceCoordinator,
    QueuePersistenceStatus,
)
from parsezen.domain.job_events import JobEvent, JobEventKind
from parsezen.domain.jobs import DocumentJob, DocumentSource, JobConfiguration
from parsezen.domain.stages import StageKind, StageStatus


@dataclass
class QueueRepositoryStub:
    saved: list[tuple[DocumentJob, ...]] = field(default_factory=list)
    events: list[tuple[JobEvent, ...]] = field(default_factory=list)
    failures_remaining: int = 0

    def replace_jobs(
        self,
        jobs: tuple[DocumentJob, ...],
        *,
        events: tuple[JobEvent, ...] = (),
    ) -> None:
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("unavailable")
        self.saved.append(jobs)
        self.events.append(events)


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


def test_queue_persistence_accumulates_typed_stage_events_with_attempt_id(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source.txt"
    source_path.write_text("private document text", encoding="utf-8")
    job = DocumentJob.create(
        DocumentSource.inspect(source_path),
        JobConfiguration(),
        order=0,
        job_id="job-one",
    )
    ready = job.replace_stage(job.stage(StageKind.PREPARE).transition(StageStatus.READY))
    running = ready.replace_stage(ready.stage(StageKind.PREPARE).transition(StageStatus.RUNNING))
    repository = QueueRepositoryStub()
    coordinator = QueuePersistenceCoordinator(repository, interval_seconds=0)

    coordinator.persist((ready,), force=True)
    coordinator.persist(
        (running,),
        force=True,
        attempt_ids={job.id: "a" * 32},
    )

    assert repository.events[0] == ()
    assert repository.events[1] == (
        JobEvent(
            job.id,
            JobEventKind.STAGE_TRANSITION,
            job.configuration_revision,
            stage=StageKind.PREPARE,
            from_status=StageStatus.READY,
            to_status=StageStatus.RUNNING,
            attempt_id="a" * 32,
        ),
    )


def test_queue_persistence_drops_pending_events_when_the_job_is_removed(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source.txt"
    source_path.write_text("document", encoding="utf-8")
    job = DocumentJob.create(
        DocumentSource.inspect(source_path),
        JobConfiguration(),
        order=0,
        job_id="removed-job",
    )
    ready = job.replace_stage(job.stage(StageKind.PREPARE).transition(StageStatus.READY))
    running = ready.replace_stage(ready.stage(StageKind.PREPARE).transition(StageStatus.RUNNING))
    repository = QueueRepositoryStub()
    coordinator = QueuePersistenceCoordinator(repository, interval_seconds=0)
    coordinator.persist((ready,), force=True)
    repository.failures_remaining = 1

    assert not coordinator.persist((running,), force=True).successful
    assert coordinator.persist((), force=True).successful

    assert repository.saved[-1] == ()
    assert repository.events[-1] == ()


def test_queue_persistence_audits_configuration_revisions(tmp_path: Path) -> None:
    source_path = tmp_path / "source.txt"
    source_path.write_text("document", encoding="utf-8")
    job = DocumentJob.create(
        DocumentSource.inspect(source_path),
        JobConfiguration(),
        order=0,
        job_id="configured-job",
    )
    configured = job.with_configuration(JobConfiguration())
    repository = QueueRepositoryStub()
    coordinator = QueuePersistenceCoordinator(repository, interval_seconds=0)

    coordinator.persist((job,), force=True)
    coordinator.persist((configured,), force=True)

    assert repository.events[-1] == (
        JobEvent(
            job.id,
            JobEventKind.CONFIGURATION_CHANGED,
            configured.configuration_revision,
        ),
    )
