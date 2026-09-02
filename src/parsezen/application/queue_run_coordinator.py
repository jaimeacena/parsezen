"""Qt-free coordination of one prepared sequential queue run."""

from __future__ import annotations

from dataclasses import dataclass

from parsezen.application.configuration_rules import requires_ai
from parsezen.application.job_runtime import JobRuntime
from parsezen.application.queue_session import QueueSession, SessionTermination
from parsezen.application.run_preparation import PreparedRunItem
from parsezen.application.run_validation import BatchValidationIssue
from parsezen.application.runtime_mapping import request_and_settings_from_job
from parsezen.application.scheduler import RunMode
from parsezen.domain.jobs import DocumentJob, JobStatus
from parsezen.pipeline.contracts import ProcessRequest
from parsezen.settings import AppSettings


@dataclass(frozen=True, slots=True)
class ClaimedQueueItem:
    job: DocumentJob
    runtime: JobRuntime
    request: ProcessRequest
    settings: AppSettings
    prepared: PreparedRunItem | None


class QueueRunCoordinator:
    """Own run selection and physical request lookup outside the main window."""

    def __init__(self, session: QueueSession) -> None:
        self._session = session

    @property
    def session(self) -> QueueSession:
        return self._session

    def begin(self) -> tuple[str, ...]:
        return self._session.begin_prepared_run()

    def finish(self, termination: SessionTermination | None = None) -> tuple[str, ...]:
        return self._session.finish(termination)

    def validation_issues(self) -> tuple[BatchValidationIssue, ...]:
        prepared = self._session.prepared_run
        return prepared.issues if prepared is not None else ()

    def pending_items(self) -> tuple[tuple[ProcessRequest, AppSettings], ...]:
        prepared = self._session.prepared_run
        return (
            tuple((item.request, item.settings) for item in prepared.items)
            if prepared is not None
            else ()
        )

    def runtime_for(
        self,
        job_id: str,
        settings: AppSettings,
    ) -> tuple[ProcessRequest, AppSettings] | None:
        prepared = self._session.prepared_run
        job = self._session.queue.get(job_id)
        if prepared is not None and job is not None:
            item = prepared.item(job_id)
            if item is not None:
                return item.request, item.settings
        if job is None:
            return None
        return request_and_settings_from_job(
            job,
            timeout_seconds=settings.timeout_seconds,
            checkpoint_retention_days=settings.checkpoint_retention_days,
        )

    def claim_next(self, settings: AppSettings) -> ClaimedQueueItem | None:
        claimed = self._session.claim_next()
        if claimed is None:
            return None
        job, runtime = claimed
        physical = self.runtime_for(job.id, settings)
        if physical is None:
            return None
        request, runtime_settings = physical
        prepared_run = self._session.prepared_run
        prepared = prepared_run.item(job.id) if prepared_run is not None else None
        return ClaimedQueueItem(job, runtime, request, runtime_settings, prepared)

    def run_flags(self) -> tuple[bool, bool]:
        mode = self._session.run_mode
        return mode is RunMode.RESUME, mode is RunMode.RETRY

    def local_ai_required(self) -> bool:
        return any(
            job.status is JobStatus.QUEUED and requires_ai(job.configuration)
            for job in self._session.queue.jobs
        )


__all__ = ["ClaimedQueueItem", "QueueRunCoordinator"]
