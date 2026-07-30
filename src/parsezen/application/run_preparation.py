"""Prepare and validate one immutable queue run before any worker starts."""

from __future__ import annotations

from dataclasses import dataclass

from parsezen.application.preflight import (
    DocumentPreflight,
    QueuePreflight,
    analyze_preflight,
    combine_preflights,
)
from parsezen.application.runtime_mapping import request_and_settings_from_job
from parsezen.application.scheduler import (
    QueueRunPlan,
    plan_queue_run,
    validate_queue_run_plan,
)
from parsezen.batch import BatchValidationIssue, validate_independent_batch_requests
from parsezen.domain.estimates import ProcessingMetric, WorkloadProfile
from parsezen.domain.jobs import DocumentJob
from parsezen.processing import ProcessRequest
from parsezen.settings import AppSettings


@dataclass(frozen=True, slots=True)
class PreparedRunItem:
    job_id: str
    request: ProcessRequest
    settings: AppSettings
    preflight: DocumentPreflight | None = None
    workload_profile: WorkloadProfile | None = None


@dataclass(frozen=True, slots=True)
class PreparedQueueRun:
    plan: QueueRunPlan
    items: tuple[PreparedRunItem, ...]
    issues: tuple[BatchValidationIssue, ...]
    preflight: QueuePreflight | None = None
    explicit_plan: bool = False

    def item(self, job_id: str) -> PreparedRunItem | None:
        return next((item for item in self.items if item.job_id == job_id), None)


def prepare_queue_run(
    jobs: tuple[DocumentJob, ...],
    *,
    timeout_seconds: float,
    checkpoint_retention_days: int = 30,
    metrics: tuple[ProcessingMetric, ...] = (),
    plan: QueueRunPlan | None = None,
) -> PreparedQueueRun:
    """Map only eligible jobs to physical requests and validate them together."""

    selected_plan = plan or plan_queue_run(jobs)
    if plan is not None:
        validate_queue_run_plan(jobs, plan)
    jobs_by_id = {job.id: job for job in jobs}
    mapped_items = tuple(
        PreparedRunItem(
            job_id,
            *request_and_settings_from_job(
                jobs_by_id[job_id],
                timeout_seconds=timeout_seconds,
                checkpoint_retention_days=checkpoint_retention_days,
            ),
        )
        for job_id in selected_plan.job_ids
    )
    issues = validate_independent_batch_requests(
        tuple((item.request, item.settings) for item in mapped_items)
    )
    if issues:
        return PreparedQueueRun(
            selected_plan,
            mapped_items,
            issues,
            explicit_plan=plan is not None,
        )

    items: list[PreparedRunItem] = []
    analyses: list[DocumentPreflight] = []
    for item in mapped_items:
        analysis, profile = analyze_preflight(
            jobs_by_id[item.job_id],
            item.request,
            item.settings,
            metrics,
        )
        analyses.append(analysis)
        items.append(
            PreparedRunItem(
                item.job_id,
                item.request,
                item.settings,
                analysis,
                profile,
            )
        )
    return PreparedQueueRun(
        selected_plan,
        tuple(items),
        issues,
        combine_preflights(tuple(analyses)),
        plan is not None,
    )
