"""Persist and advance independent human reviews over one combined worker result."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from parsezen.application.job_execution import JobExecutionController
from parsezen.application.job_queue import JobQueue
from parsezen.application.review_coordinator import selected_artifact_ids
from parsezen.application.review_plan import ReviewStep, review_steps_for_result
from parsezen.domain.jobs import DocumentJob
from parsezen.domain.reviews import ReviewKind, ReviewSession, ReviewStatus
from parsezen.domain.stages import StageStatus
from parsezen.pipeline.contracts import ProcessResult


class PhaseReviewRepository(Protocol):
    def save_review(self, review: ReviewSession) -> None: ...

    def load_reviews(self, *, job_id: str | None = None) -> tuple[ReviewSession, ...]: ...


@dataclass(frozen=True, slots=True)
class PhaseReviewProgress:
    job: DocumentJob
    applied_review: ReviewSession | None
    next_step: ReviewStep | None


class PhaseReviewSequenceCoordinator:
    """Make each review durable before exposing the next required phase."""

    def __init__(
        self,
        queue: JobQueue,
        execution: JobExecutionController,
        reviews: PhaseReviewRepository,
    ) -> None:
        self._queue = queue
        self._execution = execution
        self._reviews = reviews

    def steps(self, job_id: str, result: ProcessResult) -> tuple[ReviewStep, ...]:
        job = self._require(job_id)
        return tuple(
            step
            for step in review_steps_for_result(result, job.configuration)
            if job.stage(step.stage).participates
        )

    def reconcile(self, job_id: str, result: ProcessResult) -> PhaseReviewProgress:
        """Recover an interrupted boundary from already-applied review records."""

        job = self._require(job_id)
        steps = self.steps(job_id, result)
        saved = self._saved_by_kind(job)
        while True:
            blocked = next(
                (stage for stage in job.stages if stage.status is StageStatus.BLOCKED_FOR_REVIEW),
                None,
            )
            if blocked is None:
                break
            step = next((item for item in steps if item.stage is blocked.kind), None)
            if step is None:
                return PhaseReviewProgress(job, None, step)
            review = saved.get(step.kind)
            if review is None or review.status is not ReviewStatus.APPLIED:
                next_step = self._next_step(steps, saved)
                if next_step is not None and next_step.kind is not step.kind:
                    job = self._align_to_pending_step(job, steps, saved, next_step)
                    return PhaseReviewProgress(job, None, next_step)
                return PhaseReviewProgress(job, None, step)
            if blocked.review_id != review.id:
                job = self._execution.bind_review(job_id, step.stage, review.id)
            job = self._execution.complete_reviewed_stage(
                job_id,
                step.stage,
                review_id=review.id,
                artifact_ids=selected_artifact_ids(review),
            )

        next_step = self._next_step(steps, saved)
        if next_step is not None:
            job = self._align_to_pending_step(job, steps, saved, next_step)
        return PhaseReviewProgress(job, None, next_step)

    def prepare(
        self,
        job_id: str,
        result: ProcessResult,
        review: ReviewSession,
    ) -> DocumentJob:
        """Persist a resumable review and bind it to the exact blocked phase."""

        job = self._require(job_id)
        step = self._require_current_step(job, result, review.kind)
        if (
            review.job_id != job_id
            or review.input_version != job.configuration_revision
            or review.stage is not step.stage
        ):
            raise ValueError("The review no longer matches this document phase.")
        self._reviews.save_review(review)
        return self._execution.bind_review(job_id, step.stage, review.id)

    def apply(
        self,
        job_id: str,
        result: ProcessResult,
        review: ReviewSession,
    ) -> PhaseReviewProgress:
        """Persist one decision set and advance only to the next review."""

        self.prepare(job_id, result, review)
        applied = review.apply()
        self._reviews.save_review(applied)
        job = self._execution.complete_reviewed_stage(
            job_id,
            review.stage,
            review_id=review.id,
            artifact_ids=selected_artifact_ids(applied),
        )
        saved = self._saved_by_kind(job)
        steps = self.steps(job_id, result)
        next_step = self._next_step(steps, saved)
        if next_step is not None:
            job = self._execution.block_completed_result_for_review(
                job_id,
                next_step.stage,
                review_id=self._gate_id(job, next_step),
            )
        return PhaseReviewProgress(job, applied, next_step)

    def reopen_last_review(self, job_id: str, result: ProcessResult) -> DocumentJob:
        """Keep a failed finalization visibly reviewable without reprocessing."""

        job = self._require(job_id)
        steps = self.steps(job_id, result)
        saved = self._saved_by_kind(job)
        completed = tuple(
            (step, saved.get(step.kind))
            for step in steps
            if saved.get(step.kind) is not None and saved[step.kind].status is ReviewStatus.APPLIED
        )
        if not completed:
            return job
        step, _review = completed[-1]
        return self.reopen_review(job_id, result, kind=step.kind)

    def reopen_review(
        self,
        job_id: str,
        result: ProcessResult,
        *,
        kind: ReviewKind,
    ) -> DocumentJob:
        """Reopen one phase and invalidate all dependent review material."""

        job = self._require(job_id)
        steps = self.steps(job_id, result)
        target_index = next(
            (index for index, step in enumerate(steps) if step.kind is kind),
            None,
        )
        if target_index is None:
            raise ValueError("Esta fase de revisión no forma parte del resultado actual.")
        saved = self._saved_by_kind(job)
        target = saved.get(kind)
        if target is None or target.status is not ReviewStatus.APPLIED:
            raise ValueError("Solo se puede volver a abrir una revisión ya aplicada.")

        for later_step in steps[target_index + 1 :]:
            later = saved.get(later_step.kind)
            if later is not None:
                self._reviews.save_review(later.dismiss())
        reopened = target.reopen()
        self._reviews.save_review(reopened)
        return self._execution.block_completed_result_for_review(
            job_id,
            reopened.stage,
            review_id=reopened.id,
        )

    def _require_current_step(
        self,
        job: DocumentJob,
        result: ProcessResult,
        kind: ReviewKind,
    ) -> ReviewStep:
        progress = self.reconcile(job.id, result)
        step = progress.next_step
        if step is None or step.kind is not kind:
            raise ValueError("La revisión seleccionada ya no es la siguiente fase pendiente.")
        return step

    def _saved_by_kind(self, job: DocumentJob) -> dict[ReviewKind, ReviewSession]:
        return {
            review.kind: review
            for review in self._reviews.load_reviews(job_id=job.id)
            if review.input_version == job.configuration_revision
            and review.status in {ReviewStatus.PENDING, ReviewStatus.APPLIED}
        }

    def _align_to_pending_step(
        self,
        job: DocumentJob,
        steps: tuple[ReviewStep, ...],
        saved: dict[ReviewKind, ReviewSession],
        next_step: ReviewStep,
    ) -> DocumentJob:
        """Rewind a stale later gate to the earliest durable pending review."""

        target_index = steps.index(next_step)
        for later_step in steps[target_index + 1 :]:
            later = saved.get(later_step.kind)
            if later is not None:
                self._reviews.save_review(later.dismiss())
        pending = saved.get(next_step.kind)
        review_id = (
            pending.id
            if pending is not None and pending.status is ReviewStatus.PENDING
            else self._gate_id(job, next_step)
        )
        return self._execution.block_completed_result_for_review(
            job.id,
            next_step.stage,
            review_id=review_id,
        )

    @staticmethod
    def _next_step(
        steps: tuple[ReviewStep, ...],
        saved: dict[ReviewKind, ReviewSession],
    ) -> ReviewStep | None:
        return next(
            (
                step
                for step in steps
                if saved.get(step.kind) is None
                or saved[step.kind].status is not ReviewStatus.APPLIED
            ),
            None,
        )

    @staticmethod
    def _gate_id(job: DocumentJob, step: ReviewStep) -> str:
        return f"{job.id}:{step.stage.value}:{step.kind.value}:{job.configuration_revision}"

    def _require(self, job_id: str) -> DocumentJob:
        job = self._queue.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job
