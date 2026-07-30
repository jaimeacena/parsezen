from pathlib import Path

import pytest

from parsezen.application.job_execution import JobExecutionController
from parsezen.application.job_outcomes import JobOutcomeCoordinator, OutcomeWarning
from parsezen.application.job_queue import JobQueue
from parsezen.domain.jobs import (
    DocumentFormat,
    DocumentSource,
    JobConfiguration,
    JobStatus,
    RefinementConfiguration,
)
from parsezen.domain.stages import StageKind, StageStatus
from parsezen.processing import ProcessResult


class SnapshotRepository:
    def __init__(self, queue: JobQueue, *, fail_save: bool = False) -> None:
        self.queue = queue
        self.fail_save = fail_save
        self.saved: list[str] = []
        self.discarded: list[str] = []

    def save(self, job_id: str, _result: ProcessResult) -> str:
        job = self.queue.get(job_id)
        assert job is not None
        assert job.status is JobStatus.RUNNING
        if self.fail_save:
            raise OSError("disk unavailable")
        self.saved.append(job_id)
        return "snapshot"

    def discard(self, job_id: str) -> None:
        self.discarded.append(job_id)


def add_job(queue: JobQueue, identifier: str, *, review: bool = False) -> str:
    job = queue.add(
        DocumentSource(
            Path(f"{identifier}.txt"),
            DocumentFormat.TEXT,
            10,
            1,
        ),
        JobConfiguration(
            refinement=RefinementConfiguration(enabled=review),
        ),
        job_id=identifier,
    )
    return job.id


def test_success_completes_the_job_and_discards_obsolete_review_state() -> None:
    queue = JobQueue()
    job_id = add_job(queue, "complete")
    execution = JobExecutionController(queue)
    execution.start_next(job_id)
    snapshots = SnapshotRepository(queue)
    coordinator = JobOutcomeCoordinator(queue, execution, snapshots)

    outcome = coordinator.resolve_success(job_id, ProcessResult(Path("complete.md")))

    assert outcome.job.status is JobStatus.COMPLETED
    assert not outcome.review_required
    assert outcome.warning is None
    assert snapshots.saved == []
    assert snapshots.discarded == [job_id]


def test_review_snapshot_is_saved_before_the_job_is_blocked() -> None:
    queue = JobQueue()
    job_id = add_job(queue, "review", review=True)
    execution = JobExecutionController(queue)
    execution.start_next(job_id)
    snapshots = SnapshotRepository(queue)
    coordinator = JobOutcomeCoordinator(queue, execution, snapshots)

    outcome = coordinator.resolve_success(
        job_id,
        ProcessResult(
            Path("review.md"),
            review_markdown="Proposal",
            review_required=True,
        ),
    )

    assert outcome.review_stage is StageKind.REFINE
    assert outcome.job.status is JobStatus.WAITING_REVIEW
    assert outcome.job.stage(StageKind.REFINE).status is StageStatus.BLOCKED_FOR_REVIEW
    assert snapshots.saved == [job_id]
    assert snapshots.discarded == []


def test_review_can_continue_in_memory_when_its_recovery_snapshot_fails() -> None:
    queue = JobQueue()
    job_id = add_job(queue, "review", review=True)
    execution = JobExecutionController(queue)
    execution.start_next(job_id)
    coordinator = JobOutcomeCoordinator(
        queue,
        execution,
        SnapshotRepository(queue, fail_save=True),
    )

    outcome = coordinator.resolve_success(
        job_id,
        ProcessResult(
            Path("review.md"),
            review_markdown="Proposal",
            review_required=True,
        ),
    )

    assert outcome.job.status is JobStatus.WAITING_REVIEW
    assert outcome.warning is OutcomeWarning.REVIEW_RECOVERY_UNAVAILABLE


def test_failure_pause_and_cancellation_use_the_same_terminal_boundary() -> None:
    queue = JobQueue()
    failed_id = add_job(queue, "failed")
    paused_id = add_job(queue, "paused")
    cancelled_id = add_job(queue, "cancelled")
    execution = JobExecutionController(queue)
    coordinator = JobOutcomeCoordinator(queue, execution, SnapshotRepository(queue))

    execution.start_next(failed_id)
    failed = coordinator.resolve_failure(failed_id, StageKind.PREPARE, "Failure")
    execution.start_next(paused_id)
    paused = coordinator.resolve_cancellation(paused_id, paused=True)
    execution.start_next(cancelled_id)
    cancelled = coordinator.resolve_cancellation(cancelled_id, paused=False)

    assert failed.status is JobStatus.FAILED
    assert failed.stage(StageKind.PREPARE).error_message == "Failure"
    assert paused.status is JobStatus.PAUSED
    assert cancelled.status is JobStatus.CANCELLED


def test_unknown_job_outcome_is_rejected() -> None:
    queue = JobQueue()
    coordinator = JobOutcomeCoordinator(
        queue,
        JobExecutionController(queue),
        SnapshotRepository(queue),
    )

    with pytest.raises(KeyError, match="missing"):
        coordinator.resolve_success("missing", ProcessResult(Path("missing.md")))
