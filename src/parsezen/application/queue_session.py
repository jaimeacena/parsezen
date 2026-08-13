"""Qt-free ownership of one sequential queue-processing session."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from time import monotonic

from parsezen.application.job_execution import JobExecutionController
from parsezen.application.job_queue import JobQueue
from parsezen.application.job_runtime import JobRuntime
from parsezen.application.run_preparation import PreparedQueueRun
from parsezen.application.scheduler import RunMode
from parsezen.domain.jobs import DocumentJob


class SessionTermination(StrEnum):
    """Why the active session stopped, independently of each job outcome."""

    COMPLETED = "completed"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class QueueSession:
    """Own transient batch state while domain state remains in ``JobQueue``."""

    def __init__(
        self,
        queue: JobQueue | None = None,
        execution: JobExecutionController | None = None,
    ) -> None:
        self._queue = queue if queue is not None else JobQueue()
        self._execution = (
            execution if execution is not None else JobExecutionController(self._queue)
        )
        self._runtime_by_job: dict[str, JobRuntime] = {}
        self._prepared_run: PreparedQueueRun | None = None
        self._current_job_id: str | None = None
        self._active_run_job_ids: tuple[str, ...] = ()
        self._running = False
        self._targeted_review = False
        self._pause_requested = False
        self._last_termination: SessionTermination | None = None

    @property
    def queue(self) -> JobQueue:
        return self._queue

    @property
    def execution(self) -> JobExecutionController:
        return self._execution

    @property
    def runtime(self) -> Mapping[str, JobRuntime]:
        return self._runtime_by_job

    @property
    def prepared_run(self) -> PreparedQueueRun | None:
        return self._prepared_run

    @property
    def current_job_id(self) -> str | None:
        return self._current_job_id

    @property
    def active_run_job_ids(self) -> tuple[str, ...]:
        return self._active_run_job_ids

    @property
    def running(self) -> bool:
        return self._running

    @property
    def targeted_review(self) -> bool:
        return self._targeted_review

    @property
    def pause_requested(self) -> bool:
        return self._pause_requested

    @property
    def last_termination(self) -> SessionTermination | None:
        return self._last_termination

    @property
    def current_job(self) -> DocumentJob | None:
        return self._queue.get(self._current_job_id) if self._current_job_id is not None else None

    @property
    def current_runtime(self) -> JobRuntime | None:
        return (
            self._runtime_by_job.get(self._current_job_id)
            if self._current_job_id is not None
            else None
        )

    @property
    def run_mode(self) -> RunMode:
        plan = self._prepared_run.plan if self._prepared_run is not None else None
        return plan.mode if plan is not None else self._execution.plan_run().mode

    def set_prepared_run(self, prepared: PreparedQueueRun | None) -> None:
        if self._running and prepared is not self._prepared_run:
            raise RuntimeError("Cannot replace a prepared run while its session is active.")
        self._prepared_run = prepared

    def ensure_runtime(self, job_id: str) -> JobRuntime:
        return self._runtime_by_job.setdefault(job_id, JobRuntime())

    def runtime_for(self, job_id: str) -> JobRuntime | None:
        return self._runtime_by_job.get(job_id)

    def remove_runtime(self, job_id: str) -> None:
        self._runtime_by_job.pop(job_id, None)

    def replace_runtime(self, runtime: Mapping[str, JobRuntime]) -> None:
        if self._running:
            raise RuntimeError("Cannot replace runtime state while a session is active.")
        self._runtime_by_job = dict(runtime)

    def retain_runtime(self, job_ids: Iterable[str]) -> None:
        retained = frozenset(job_ids)
        self._runtime_by_job = {
            job_id: runtime
            for job_id, runtime in self._runtime_by_job.items()
            if job_id in retained
        }
        for job_id in retained:
            self.ensure_runtime(job_id)

    def reset_idle_selection(self) -> None:
        if self._running:
            raise RuntimeError("Cannot reset the active job while processing.")
        self._current_job_id = None

    def begin_prepared_run(self) -> tuple[str, ...]:
        prepared = self._prepared_run
        if self._running:
            raise RuntimeError("A queue session is already active.")
        if prepared is None or not prepared.plan.job_ids or prepared.issues:
            raise ValueError("A valid prepared queue run is required.")
        self._execution.begin_run(
            prepared.plan,
            allow_subset=prepared.explicit_plan,
        )
        for job_id in prepared.plan.job_ids:
            runtime = self._runtime_by_job.get(job_id)
            if runtime is not None:
                runtime.reset_for_run()
        self._current_job_id = None
        self._active_run_job_ids = prepared.plan.job_ids
        self._running = True
        self._targeted_review = False
        self._pause_requested = False
        self._last_termination = None
        return self._active_run_job_ids

    def begin_targeted_review(self, job_id: str) -> JobRuntime:
        if self._running:
            raise RuntimeError("A queue session is already active.")
        self._execution.begin_targeted_review(job_id)
        runtime = self.ensure_runtime(job_id)
        runtime.reset_for_run()
        runtime.started_at = monotonic()
        self._current_job_id = job_id
        self._active_run_job_ids = (job_id,)
        self._running = True
        self._targeted_review = True
        self._pause_requested = False
        self._last_termination = None
        return runtime

    def claim_next(self) -> tuple[DocumentJob, JobRuntime] | None:
        if not self._running or self._pause_requested:
            return None
        job = self._execution.start_next_available()
        if job is None:
            return None
        self._current_job_id = job.id
        runtime = self.ensure_runtime(job.id)
        runtime.reset_for_run()
        runtime.started_at = monotonic()
        return job, runtime

    def release_current(self) -> None:
        self._current_job_id = None

    def request_pause(self) -> bool:
        if not self._running or self._pause_requested:
            return False
        self._pause_requested = True
        return True

    def reject_pause(self) -> None:
        self._pause_requested = False

    def has_next(self) -> bool:
        return (
            self._running and not self._pause_requested and bool(self._execution.plan_run().job_ids)
        )

    def finish(
        self,
        termination: SessionTermination | None = None,
    ) -> tuple[str, ...]:
        active_ids = self._active_run_job_ids
        resolved = termination
        if resolved is None:
            resolved = (
                SessionTermination.PAUSED if self._pause_requested else SessionTermination.COMPLETED
            )
        self._execution.finish_run()
        self._prepared_run = None
        self._current_job_id = None
        self._active_run_job_ids = ()
        self._running = False
        self._targeted_review = False
        self._pause_requested = False
        self._last_termination = resolved
        return active_ids


__all__ = ["QueueSession", "SessionTermination"]
