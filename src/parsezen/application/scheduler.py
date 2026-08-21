"""Pure selection rules for the single-resource automatic scheduler."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from parsezen.application.planner import activate_next_stage
from parsezen.domain.jobs import DocumentJob, JobStatus
from parsezen.domain.stages import StageKind, StageStatus


class RunMode(StrEnum):
    NONE = "none"
    NEW = "new"
    RESUME = "resume"
    RETRY = "retry"


@dataclass(frozen=True, slots=True)
class SchedulerDecision:
    job_id: str
    stage: StageKind


@dataclass(frozen=True, slots=True)
class QueueRunPlan:
    mode: RunMode
    job_ids: tuple[str, ...]

    def consume(self, job_id: str) -> QueueRunPlan:
        if job_id not in self.job_ids:
            raise ValueError("Only a planned job can be consumed.")
        return QueueRunPlan(
            self.mode,
            tuple(candidate for candidate in self.job_ids if candidate != job_id),
        )


def plan_queue_run(jobs: tuple[DocumentJob, ...]) -> QueueRunPlan:
    """Choose one explicit run mode without mixing unrelated retries."""

    ordered = tuple(job for job in sorted(jobs, key=lambda item: item.order) if job.is_configured)
    if any(job.status is JobStatus.PAUSED for job in ordered):
        eligible = tuple(
            job.id for job in ordered if job.status in {JobStatus.QUEUED, JobStatus.PAUSED}
        )
        return QueueRunPlan(RunMode.RESUME, eligible)
    queued = tuple(job.id for job in ordered if job.status is JobStatus.QUEUED)
    if queued:
        return QueueRunPlan(RunMode.NEW, queued)
    retryable = tuple(
        job.id for job in ordered if job.status in {JobStatus.FAILED, JobStatus.CANCELLED}
    )
    if retryable:
        return QueueRunPlan(RunMode.RETRY, retryable)
    return QueueRunPlan(RunMode.NONE, ())


def validate_queue_run_plan(
    jobs: tuple[DocumentJob, ...],
    plan: QueueRunPlan,
) -> None:
    """Validate an explicit subset run without broadening its requested mode."""

    if plan.mode is RunMode.NONE or not plan.job_ids:
        raise ValueError("An explicit queue run must contain at least one document.")
    if len(plan.job_ids) != len(set(plan.job_ids)):
        raise ValueError("A queue run cannot contain the same document twice.")
    jobs_by_id = {job.id: job for job in jobs}
    if any(job_id not in jobs_by_id for job_id in plan.job_ids):
        raise ValueError("The queue run references a document that is no longer available.")
    if any(job.status is JobStatus.RUNNING for job in jobs):
        raise ValueError("A new queue run cannot start while another document is active.")
    eligible_statuses = {
        RunMode.NEW: frozenset({JobStatus.QUEUED}),
        RunMode.RESUME: frozenset({JobStatus.QUEUED, JobStatus.PAUSED}),
        RunMode.RETRY: frozenset({JobStatus.FAILED, JobStatus.CANCELLED}),
    }[plan.mode]
    if any(
        not jobs_by_id[job_id].is_configured or jobs_by_id[job_id].status not in eligible_statuses
        for job_id in plan.job_ids
    ):
        raise ValueError("Los documentos seleccionados ya no pueden ejecutarse de este modo.")


def prepare_runnable_jobs(jobs: tuple[DocumentJob, ...]) -> tuple[DocumentJob, ...]:
    """Activate at most one ready phase per job without starting any work."""

    return tuple(activate_next_stage(job) for job in sorted(jobs, key=lambda item: item.order))


def select_next_stage(
    jobs: tuple[DocumentJob, ...],
    eligible_job_ids: tuple[str, ...] | None = None,
) -> SchedulerDecision | None:
    """Select the first runnable phase unless automatic work is already active."""

    ordered = tuple(sorted(jobs, key=lambda item: item.order))
    if any(
        stage.status is StageStatus.RUNNING
        for job in ordered
        for stage in job.stages
        if stage.participates
    ):
        return None
    eligible = set(eligible_job_ids) if eligible_job_ids is not None else None
    for job in ordered:
        if eligible is not None and job.id not in eligible:
            continue
        for stage in job.stages:
            if not stage.participates or stage.status is StageStatus.COMPLETED:
                continue
            if stage.status is StageStatus.BLOCKED_FOR_REVIEW:
                break
            if stage.status in {
                StageStatus.PENDING,
                StageStatus.READY,
                StageStatus.FAILED,
                StageStatus.PAUSED,
                StageStatus.CANCELLED,
                StageStatus.INVALIDATED,
            }:
                return SchedulerDecision(job_id=job.id, stage=stage.kind)
            break
    return None


def start_selected_stage(
    jobs: tuple[DocumentJob, ...],
    decision: SchedulerDecision,
    eligible_job_ids: tuple[str, ...] | None = None,
) -> tuple[DocumentJob, ...]:
    """Atomically project the selected ready phase as running."""

    if select_next_stage(jobs, eligible_job_ids) != decision:
        raise ValueError("The scheduler decision is stale.")
    updated: list[DocumentJob] = []
    for job in jobs:
        if job.id != decision.job_id:
            updated.append(job)
            continue
        stage = job.stage(decision.stage)
        if stage.status in {
            StageStatus.FAILED,
            StageStatus.PAUSED,
            StageStatus.CANCELLED,
            StageStatus.INVALIDATED,
        }:
            job = job.replace_stage(stage.transition(StageStatus.READY))
            stage = job.stage(decision.stage)
        if stage.status is StageStatus.PENDING:
            job = job.replace_stage(stage.transition(StageStatus.READY))
            stage = job.stage(decision.stage)
        updated.append(job.replace_stage(stage.transition(StageStatus.RUNNING)))
    return tuple(updated)
