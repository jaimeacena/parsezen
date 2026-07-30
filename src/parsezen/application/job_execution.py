"""Validated runtime transitions for the sequential document queue."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from parsezen.application.job_queue import JobQueue
from parsezen.application.planner import activate_next_stage
from parsezen.application.scheduler import (
    QueueRunPlan,
    plan_queue_run,
    select_next_stage,
    start_selected_stage,
    validate_queue_run_plan,
)
from parsezen.domain.jobs import DocumentJob
from parsezen.domain.stages import STAGE_ORDER, StageKind, StageStatus


class JobExecutionController:
    """Apply worker events to domain jobs without depending on Qt."""

    def __init__(self, queue: JobQueue) -> None:
        self._queue = queue
        self._active_run_plan: QueueRunPlan | None = None

    def plan_run(self) -> QueueRunPlan:
        return plan_queue_run(self._queue.jobs)

    def begin_run(
        self,
        plan: QueueRunPlan | None = None,
        *,
        allow_subset: bool = False,
    ) -> QueueRunPlan:
        current = self.plan_run()
        if plan is not None and plan != current:
            if not allow_subset:
                raise ValueError("The prepared queue run is stale.")
            validate_queue_run_plan(self._queue.jobs, plan)
        self._active_run_plan = plan or current
        return self._active_run_plan

    def finish_run(self) -> None:
        self._active_run_plan = None

    def start_next_available(self) -> DocumentJob | None:
        plan = self._active_run_plan or self.begin_run()
        decision = select_next_stage(self._queue.jobs, plan.job_ids)
        if decision is None:
            return None
        self._active_run_plan = plan.consume(decision.job_id)
        self._queue.restore(
            start_selected_stage(
                self._queue.jobs,
                decision,
                plan.job_ids,
            )
        )
        return self._require(decision.job_id)

    def start_next(self, job_id: str) -> DocumentJob:
        job = self._require(job_id)
        self._ensure_resource_available(job_id)
        stage = next(
            (
                candidate
                for candidate in job.stages
                if candidate.participates and candidate.status is not StageStatus.COMPLETED
            ),
            None,
        )
        if stage is None:
            return job
        if stage.status is StageStatus.BLOCKED_FOR_REVIEW:
            raise ValueError("A review-blocked job cannot resume automatically.")
        job = self._make_running(job, stage.kind)
        return self._queue.replace(job)

    def advance(self, job_id: str, stage_kind: StageKind) -> DocumentJob:
        job = self._require(job_id)
        self._ensure_resource_available(job_id)
        target = job.stage(stage_kind)
        if not target.participates or target.status is StageStatus.COMPLETED:
            return job
        target_index = STAGE_ORDER.index(stage_kind)
        if any(
            stage.status in {StageStatus.RUNNING, StageStatus.BLOCKED_FOR_REVIEW}
            for stage in job.stages[target_index + 1 :]
            if stage.participates
        ):
            raise ValueError("A worker event cannot move backwards from an active later phase.")
        for stage in job.stages[:target_index]:
            if stage.participates and stage.status is not StageStatus.COMPLETED:
                job = self._complete_stage(job, stage.kind)
        job = self._make_running(job, stage_kind)
        return self._queue.replace(job)

    def report_progress(
        self,
        job_id: str,
        stage_kind: StageKind,
        current: int,
        total: int,
        message: str | None = None,
    ) -> DocumentJob:
        job = self.advance(job_id, stage_kind)
        stage = job.stage(stage_kind)
        if stage.status is not StageStatus.RUNNING:
            return job
        return self._queue.replace(job.replace_stage(stage.with_progress(current, total, message)))

    def block_for_review(
        self,
        job_id: str,
        stage_kind: StageKind,
        *,
        review_id: str,
    ) -> DocumentJob:
        job = self.advance(job_id, stage_kind)
        stage = job.stage(stage_kind)
        if stage.status is StageStatus.BLOCKED_FOR_REVIEW:
            return job
        if stage.status is not StageStatus.RUNNING:
            raise ValueError("Only a running phase can request review.")
        return self._queue.replace(
            job.replace_stage(
                stage.transition(
                    StageStatus.BLOCKED_FOR_REVIEW,
                    review_id=review_id,
                )
            )
        )

    def block_completed_result_for_review(
        self,
        job_id: str,
        stage_kind: StageKind,
        *,
        review_id: str,
    ) -> DocumentJob:
        """Reopen the exact review phase after the combined processor returns."""

        job = self._require(job_id)
        target_index = STAGE_ORDER.index(stage_kind)
        for earlier in job.stages[:target_index]:
            if earlier.participates and earlier.status is not StageStatus.COMPLETED:
                job = self._complete_stage(job, earlier.kind)
        for later in job.stages[target_index + 1 :]:
            if not later.participates:
                continue
            if later.status is StageStatus.RUNNING:
                job = job.replace_stage(later.transition(StageStatus.COMPLETED))
                later = job.stage(later.kind)
            if later.status in {StageStatus.READY, StageStatus.COMPLETED}:
                job = job.replace_stage(later.invalidate_cached_result())
        target = job.stage(stage_kind)
        if target.status is StageStatus.BLOCKED_FOR_REVIEW:
            return self.bind_review(job_id, stage_kind, review_id)
        return self._queue.replace(
            job.replace_stage(target.require_cached_result_review(review_id))
        )

    def bind_review(
        self,
        job_id: str,
        stage_kind: StageKind,
        review_id: str,
    ) -> DocumentJob:
        """Replace a deterministic review gate with its persisted review id."""

        job = self._require(job_id)
        stage = job.stage(stage_kind)
        if stage.status is not StageStatus.BLOCKED_FOR_REVIEW:
            raise ValueError("Only a review-blocked phase can bind a review.")
        return self._queue.replace(job.replace_stage(replace(stage, review_id=review_id)))

    def complete_reviewed_stage(
        self,
        job_id: str,
        stage_kind: StageKind,
        *,
        review_id: str,
        artifact_ids: tuple[str, ...],
    ) -> DocumentJob:
        """Complete one approved review and expose the next chronological phase."""

        job = self._require(job_id)
        stage = job.stage(stage_kind)
        if stage.status is not StageStatus.BLOCKED_FOR_REVIEW or stage.review_id != review_id:
            raise ValueError("The phase is not blocked by this review.")
        completed = stage.transition(
            StageStatus.COMPLETED,
            artifact_ids=artifact_ids,
        )
        return self._queue.replace(activate_next_stage(job.replace_stage(completed)))

    def fail(
        self,
        job_id: str,
        stage_kind: StageKind,
        *,
        error_code: str,
        error_message: str,
    ) -> DocumentJob:
        job = self.advance(job_id, stage_kind)
        stage = job.stage(stage_kind)
        if stage.status is not StageStatus.RUNNING:
            return job
        return self._queue.replace(
            job.replace_stage(
                stage.transition(
                    StageStatus.FAILED,
                    error_code=error_code,
                    error_message=error_message,
                )
            )
        )

    def pause(self, job_id: str) -> DocumentJob:
        return self._stop_active(job_id, StageStatus.PAUSED)

    def cancel(self, job_id: str) -> DocumentJob:
        return self._stop_active(job_id, StageStatus.CANCELLED)

    def complete(self, job_id: str, result_path: Path) -> DocumentJob:
        job = self._require(job_id)
        for stage in job.stages:
            if stage.participates and stage.status is not StageStatus.COMPLETED:
                job = self._complete_stage(job, stage.kind, allow_review=True)
        return self._queue.replace(replace(job, result_path=result_path))

    def reset_paused(self, job_id: str) -> DocumentJob:
        job = self._require(job_id)
        reset = DocumentJob.create(
            job.source,
            job.configuration,
            order=job.order,
            job_id=job.id,
        )
        reset = replace(
            reset,
            configuration_revision=job.configuration_revision,
            warnings=job.warnings,
        )
        first = next(stage for stage in reset.stages if stage.participates)
        reset = reset.replace_stage(
            first.transition(StageStatus.READY).transition(StageStatus.PAUSED)
        )
        return self._queue.replace(reset)

    def recover_interrupted(self, job_id: str) -> DocumentJob:
        job = self._require(job_id)
        active = next(
            (
                stage
                for stage in job.stages
                if stage.participates and stage.status in {StageStatus.RUNNING, StageStatus.READY}
            ),
            None,
        )
        if active is None:
            return job
        return self._queue.replace(job.replace_stage(active.transition(StageStatus.PAUSED)))

    def _stop_active(self, job_id: str, status: StageStatus) -> DocumentJob:
        job = self._require(job_id)
        active = next(
            (
                stage
                for stage in job.stages
                if stage.participates and stage.status in {StageStatus.RUNNING, StageStatus.READY}
            ),
            None,
        )
        if active is None:
            return job
        return self._queue.replace(job.replace_stage(active.transition(status)))

    def _make_running(self, job: DocumentJob, stage_kind: StageKind) -> DocumentJob:
        stage = job.stage(stage_kind)
        if stage.status is StageStatus.RUNNING:
            return job
        if stage.status in {
            StageStatus.FAILED,
            StageStatus.PAUSED,
            StageStatus.CANCELLED,
            StageStatus.INVALIDATED,
        }:
            job = job.replace_stage(stage.transition(StageStatus.READY))
            stage = job.stage(stage_kind)
        if stage.status is StageStatus.PENDING:
            job = job.replace_stage(stage.transition(StageStatus.READY))
            stage = job.stage(stage_kind)
        if stage.status is not StageStatus.READY:
            raise ValueError(f"{stage_kind.value} cannot start from {stage.status.value}.")
        return job.replace_stage(stage.transition(StageStatus.RUNNING))

    def _complete_stage(
        self,
        job: DocumentJob,
        stage_kind: StageKind,
        *,
        allow_review: bool = False,
    ) -> DocumentJob:
        stage = job.stage(stage_kind)
        if stage.status is StageStatus.COMPLETED:
            return job
        if stage.status is StageStatus.BLOCKED_FOR_REVIEW:
            if not allow_review:
                raise ValueError("A mandatory review must finish before later phases can run.")
            return job.replace_stage(stage.transition(StageStatus.COMPLETED))
        job = self._make_running(job, stage_kind)
        stage = job.stage(stage_kind)
        return job.replace_stage(stage.transition(StageStatus.COMPLETED))

    def _ensure_resource_available(self, job_id: str) -> None:
        if any(
            job.id != job_id
            and any(
                stage.participates and stage.status is StageStatus.RUNNING for stage in job.stages
            )
            for job in self._queue.jobs
        ):
            raise ValueError("Only one automatic phase can run at a time.")

    def _require(self, job_id: str) -> DocumentJob:
        job = self._queue.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job
