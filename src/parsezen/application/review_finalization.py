"""Finalize approved reviews without mixing persistence decisions into Qt."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from parsezen.application.job_execution import JobExecutionController
from parsezen.application.job_queue import JobQueue
from parsezen.domain.jobs import DocumentJob
from parsezen.domain.reviews import ReviewSession, ReviewStatus


class ReviewRepository(Protocol):
    def save_review(self, review: ReviewSession) -> None: ...

    def delete_review_material(self, job_id: str) -> None: ...


class ReviewArtifactRepository(Protocol):
    def remove_job(self, job_id: str) -> None: ...


class ReviewFinalizationWarning(StrEnum):
    STALE_REVIEW_MATERIAL_NOT_REMOVED = "stale_review_material_not_removed"


@dataclass(frozen=True, slots=True)
class FinalizedReview:
    job: DocumentJob
    reviews: tuple[ReviewSession, ...]
    warning: ReviewFinalizationWarning | None = None


class ReviewFinalizationCoordinator:
    """Publish review decisions and then remove their private temporary material."""

    def __init__(
        self,
        queue: JobQueue,
        execution: JobExecutionController,
        reviews: ReviewRepository,
        artifacts: ReviewArtifactRepository,
    ) -> None:
        self._queue = queue
        self._execution = execution
        self._reviews = reviews
        self._artifacts = artifacts

    def finalize(
        self,
        job_id: str,
        result_path: Path,
        reviews: tuple[ReviewSession, ...],
    ) -> FinalizedReview:
        job = self._require(job_id)
        if any(
            review.job_id != job_id or review.input_version != job.configuration_revision
            for review in reviews
        ):
            raise ValueError("The review no longer matches this document configuration.")
        applied = tuple(
            review if review.status is ReviewStatus.APPLIED else review.apply()
            for review in reviews
        )
        for review in applied:
            self._reviews.save_review(review)
        completed = self._execution.complete(job_id, result_path)
        warning = None
        try:
            self._reviews.delete_review_material(job_id)
            self._artifacts.remove_job(job_id)
        except (OSError, RuntimeError, ValueError):
            warning = ReviewFinalizationWarning.STALE_REVIEW_MATERIAL_NOT_REMOVED
        return FinalizedReview(completed, applied, warning)

    def _require(self, job_id: str) -> DocumentJob:
        job = self._queue.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job
