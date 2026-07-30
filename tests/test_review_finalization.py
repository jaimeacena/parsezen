from pathlib import Path

import pytest

from parsezen.application.job_execution import JobExecutionController
from parsezen.application.job_queue import JobQueue
from parsezen.application.review_finalization import (
    ReviewFinalizationCoordinator,
    ReviewFinalizationWarning,
)
from parsezen.domain.jobs import (
    DocumentFormat,
    DocumentSource,
    JobConfiguration,
    JobStatus,
    RefinementConfiguration,
)
from parsezen.domain.reviews import (
    ReviewChoice,
    ReviewKind,
    ReviewSession,
    ReviewStatus,
    ReviewUnit,
)
from parsezen.domain.stages import StageKind


class ReviewRepository:
    def __init__(self) -> None:
        self.saved: list[ReviewSession] = []
        self.deleted: list[str] = []

    def save_review(self, review: ReviewSession) -> None:
        self.saved.append(review)

    def delete_review_material(self, job_id: str) -> None:
        self.deleted.append(job_id)


class ArtifactRepository:
    def __init__(self, *, fail_cleanup: bool = False) -> None:
        self.fail_cleanup = fail_cleanup
        self.removed: list[str] = []

    def remove_job(self, job_id: str) -> None:
        if self.fail_cleanup:
            raise OSError("locked")
        self.removed.append(job_id)


def blocked_review(
    queue: JobQueue,
    execution: JobExecutionController,
) -> tuple[str, ReviewSession]:
    job = queue.add(
        DocumentSource(Path("book.md"), DocumentFormat.MARKDOWN, 100, 1),
        JobConfiguration(refinement=RefinementConfiguration(enabled=True)),
        job_id="job",
    )
    review = ReviewSession.create(
        job_id=job.id,
        stage=StageKind.REFINE,
        kind=ReviewKind.REFINEMENT,
        input_artifact_id="input",
        input_version=job.configuration_revision,
        units=(ReviewUnit("change", "original", "proposed"),),
    ).decide("change", ReviewChoice.PROPOSED)
    execution.start_next(job.id)
    execution.block_for_review(job.id, StageKind.REFINE, review_id=review.id)
    return job.id, review


def test_finalization_persists_decisions_completes_job_and_cleans_material() -> None:
    queue = JobQueue()
    execution = JobExecutionController(queue)
    job_id, review = blocked_review(queue, execution)
    reviews = ReviewRepository()
    artifacts = ArtifactRepository()
    coordinator = ReviewFinalizationCoordinator(queue, execution, reviews, artifacts)

    finalized = coordinator.finalize(job_id, Path("book.epub"), (review,))

    assert finalized.job.status is JobStatus.COMPLETED
    assert finalized.job.result_path == Path("book.epub")
    assert finalized.reviews[0].status is ReviewStatus.APPLIED
    assert reviews.saved == list(finalized.reviews)
    assert reviews.deleted == [job_id]
    assert artifacts.removed == [job_id]
    assert finalized.warning is None


def test_incomplete_review_is_rejected_before_any_side_effect() -> None:
    queue = JobQueue()
    execution = JobExecutionController(queue)
    job_id, review = blocked_review(queue, execution)
    incomplete = ReviewSession.create(
        job_id=job_id,
        stage=StageKind.REFINE,
        kind=ReviewKind.REFINEMENT,
        input_artifact_id="input",
        input_version=review.input_version,
        units=(ReviewUnit("unresolved", "original", "proposed"),),
    )
    reviews = ReviewRepository()
    artifacts = ArtifactRepository()
    coordinator = ReviewFinalizationCoordinator(queue, execution, reviews, artifacts)

    with pytest.raises(ValueError, match="complete"):
        coordinator.finalize(job_id, Path("book.epub"), (incomplete,))

    assert reviews.saved == []
    assert reviews.deleted == []
    assert artifacts.removed == []
    job = queue.get(job_id)
    assert job is not None
    assert job.status is JobStatus.WAITING_REVIEW


def test_cleanup_failure_does_not_reclassify_a_published_result() -> None:
    queue = JobQueue()
    execution = JobExecutionController(queue)
    job_id, review = blocked_review(queue, execution)
    coordinator = ReviewFinalizationCoordinator(
        queue,
        execution,
        ReviewRepository(),
        ArtifactRepository(fail_cleanup=True),
    )

    finalized = coordinator.finalize(job_id, Path("book.epub"), (review,))

    assert finalized.job.status is JobStatus.COMPLETED
    assert finalized.warning is ReviewFinalizationWarning.STALE_REVIEW_MATERIAL_NOT_REMOVED


def test_already_applied_phase_reviews_can_be_published_without_reapplying() -> None:
    queue = JobQueue()
    execution = JobExecutionController(queue)
    job_id, review = blocked_review(queue, execution)
    applied = review.apply()
    reviews = ReviewRepository()
    coordinator = ReviewFinalizationCoordinator(
        queue,
        execution,
        reviews,
        ArtifactRepository(),
    )

    finalized = coordinator.finalize(job_id, Path("book.epub"), (applied,))

    assert finalized.job.status is JobStatus.COMPLETED
    assert finalized.reviews == (applied,)
    assert reviews.saved == [applied]


def test_review_from_an_old_configuration_is_rejected() -> None:
    queue = JobQueue()
    execution = JobExecutionController(queue)
    job_id, review = blocked_review(queue, execution)
    stale = ReviewSession(
        review.id,
        review.job_id,
        review.stage,
        review.kind,
        review.input_artifact_id,
        review.input_version + 1,
        review.units,
        review.status,
        review.created_at,
        review.updated_at,
    )
    coordinator = ReviewFinalizationCoordinator(
        queue,
        execution,
        ReviewRepository(),
        ArtifactRepository(),
    )

    with pytest.raises(ValueError, match="no longer matches"):
        coordinator.finalize(job_id, Path("book.epub"), (stale,))
