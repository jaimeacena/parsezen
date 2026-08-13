"""Qt adapter that owns one physical document-processing task at a time."""

from __future__ import annotations

import inspect
import logging
import secrets
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from parsezen.cancellation import CancellationToken
from parsezen.domain.attempt_activity import (
    AttemptEvent,
    AttemptEventStatus,
    AttemptPhase,
    AttemptTimeline,
    FailureSnapshot,
    extract_diagnostic_reference,
    is_safe_token,
    make_failure_snapshot,
)
from parsezen.domain.outcomes import EarlyCheckReport
from parsezen.domain.process_lifecycle import phase_for_process_stage
from parsezen.errors import EarlyCheckError, ParsezenError, ProcessingCancelledError
from parsezen.failure_recovery import ProcessingFailure
from parsezen.processing import ProcessRequest, ProcessResult, ProcessStage, process_document
from parsezen.settings import AppSettings

ProcessCallable = Callable[..., ProcessResult]
EarlyCheckCallable = Callable[..., EarlyCheckReport]
LOGGER = logging.getLogger(__name__)


class ProcessingWorkerSignals(QObject):
    """Queued events emitted by one physical processing task."""

    stage_changed = Signal(object)
    improvement_progress = Signal(int, int)
    early_check_started = Signal()
    early_check_progress = Signal(int, int)
    early_check_completed = Signal(object)
    succeeded = Signal(object)
    failed = Signal(object)
    cancelled = Signal()
    finished = Signal()


class ProcessingWorker(QRunnable):
    """Run the Qt-free processor once in a Qt thread pool."""

    def __init__(
        self,
        request: ProcessRequest,
        settings: AppSettings,
        cancellation: CancellationToken,
        work_checkpoint_root: Path | None = None,
        *,
        processor: ProcessCallable = process_document,
        early_check: EarlyCheckCallable | None = None,
        attempt_id: str | None = None,
    ) -> None:
        super().__init__()
        self.signals = ProcessingWorkerSignals()
        self._request = request
        self._settings = settings
        self._cancellation = cancellation
        self._work_checkpoint_root = work_checkpoint_root
        self._processor = processor
        self._early_check = early_check
        self._attempt_id = (
            attempt_id
            if attempt_id is not None and is_safe_token(attempt_id)
            else secrets.token_hex(16)
        )
        self._current_stage: ProcessStage | None = None
        self._early_check_active = early_check is not None
        self._pause_requested = False
        self._diagnostic_reference: str | None = None
        self._early_check_completed = False

    @property
    def attempt_id(self) -> str:
        return self._attempt_id

    @property
    def failure_phase(self) -> AttemptPhase:
        if self._early_check_active:
            return AttemptPhase.EARLY_CHECK
        phase = phase_for_process_stage(self._current_stage)
        return AttemptPhase.PUBLICATION if phase is AttemptPhase.COMPLETION else phase

    @property
    def diagnostic_reference(self) -> str | None:
        return self._diagnostic_reference

    def request_pause(self) -> None:
        self._pause_requested = True

    def run(self) -> None:
        LOGGER.info(
            "processing_attempt_started attempt_id=%s phase=%s",
            self._attempt_id,
            AttemptPhase.PREPARATION.value,
        )
        try:
            if self._early_check is not None:
                LOGGER.info(
                    "processing_attempt_early_check_started attempt_id=%s phase=%s",
                    self._attempt_id,
                    AttemptPhase.EARLY_CHECK.value,
                )
                self.signals.early_check_started.emit()
                early_arguments: dict[str, object] = {
                    "cancellation": self._cancellation,
                    "work_checkpoint_root": self._work_checkpoint_root,
                    "on_progress": self.signals.early_check_progress.emit,
                }
                if _accepts_keyword(self._early_check, "attempt_id"):
                    early_arguments["attempt_id"] = self._attempt_id
                if _accepts_keyword(self._early_check, "processor"):
                    early_arguments["processor"] = self._process_with_attempt_id
                report = self._early_check(
                    self._request,
                    self._settings,
                    **early_arguments,
                )
                self.signals.early_check_completed.emit(report)
                LOGGER.info(
                    "processing_attempt_early_check_completed attempt_id=%s phase=%s "
                    "blocking=%s sampled_pages=%d warning_pages=%d",
                    self._attempt_id,
                    AttemptPhase.EARLY_CHECK.value,
                    report.blocking,
                    len(report.sampled_pages),
                    report.warning_pages,
                )
                self._early_check_completed = True
                if report.blocking:
                    raise EarlyCheckError(
                        "La comprobaci\u00f3n temprana detect\u00f3 un riesgo material en las "
                        "p\u00e1ginas representativas. " + " ".join(report.blocking_reasons)
                    )
                self._early_check_active = False

            arguments: dict[str, object] = {
                "on_stage": self._report_stage,
                "on_progress": self.signals.improvement_progress.emit,
                "settings": self._settings,
                "cancellation": self._cancellation,
            }
            if _accepts_keyword(self._processor, "work_checkpoint_root"):
                arguments["work_checkpoint_root"] = self._work_checkpoint_root
            if _accepts_keyword(self._processor, "attempt_id"):
                arguments["attempt_id"] = self._attempt_id
            result = self._processor(self._request, **arguments)
        except ProcessingCancelledError:
            self._log_incomplete_early_check("cancelled")
            if self._pause_requested:
                LOGGER.info(
                    "processing_attempt_paused attempt_id=%s phase=%s error_code=pause "
                    "error_type=ProcessingCancelledError",
                    self._attempt_id,
                    self.failure_phase.value,
                )
            else:
                LOGGER.info(
                    "processing_attempt_cancelled attempt_id=%s phase=%s "
                    "error_code=cancellation error_type=ProcessingCancelledError",
                    self._attempt_id,
                    self.failure_phase.value,
                )
            self.signals.cancelled.emit()
        except ParsezenError as exc:
            self._log_incomplete_early_check("failed")
            failure = ProcessingFailure.from_exception(exc)
            self._log_failure(failure)
            LOGGER.warning(
                "processing_attempt_task_failed error_type=%s attempt_id=%s",
                type(exc).__name__,
                self._attempt_id,
            )
            self.signals.failed.emit(failure)
        except Exception as exc:
            self._log_incomplete_early_check("failed")
            failure = ProcessingFailure.from_exception(exc)
            self._log_failure(failure)
            LOGGER.error(
                "unexpected_processing_attempt_failure error_type=%s attempt_id=%s",
                type(exc).__name__,
                self._attempt_id,
            )
            self.signals.failed.emit(failure)
        else:
            LOGGER.info(
                "processing_attempt_completed attempt_id=%s phase=%s",
                self._attempt_id,
                AttemptPhase.COMPLETION.value,
            )
            self.signals.succeeded.emit(result)
        finally:
            self.signals.finished.emit()

    def _report_stage(self, stage: ProcessStage) -> None:
        self._current_stage = stage
        self.signals.stage_changed.emit(stage)

    def _process_with_attempt_id(
        self,
        request: ProcessRequest,
        *args: object,
        **kwargs: object,
    ) -> ProcessResult:
        arguments = dict(kwargs)
        if _accepts_keyword(self._processor, "attempt_id"):
            arguments["attempt_id"] = self._attempt_id
        return self._processor(request, *args, **arguments)

    def _log_failure(self, failure: ProcessingFailure) -> None:
        reference = extract_diagnostic_reference(failure.message)
        if failure.kind.value == "unexpected" and reference is None:
            reference = secrets.token_hex(4)
        self._diagnostic_reference = reference
        LOGGER.warning(
            "processing_attempt_failed attempt_id=%s phase=%s error_code=%s error_type=%s "
            "incident=%s diagnostic_reference=%s",
            self._attempt_id,
            self.failure_phase.value,
            failure.kind.value,
            failure.error_type,
            reference or "none",
            reference or "none",
        )

    def _log_incomplete_early_check(self, outcome: str) -> None:
        if not self._early_check_active or self._early_check_completed:
            return
        LOGGER.info(
            "processing_attempt_early_check_completed attempt_id=%s phase=%s outcome=%s "
            "blocking=false sampled_pages=0 warning_pages=0",
            self._attempt_id,
            AttemptPhase.EARLY_CHECK.value,
            outcome,
        )
        self._early_check_completed = True


class ProcessingRunner(QObject):
    """Own worker construction, cancellation and release outside the window."""

    stage_changed = Signal(object)
    improvement_progress = Signal(int, int)
    early_check_started = Signal()
    early_check_progress = Signal(int, int)
    early_check_completed = Signal(object)
    succeeded = Signal(object)
    failed = Signal(object)
    cancelled = Signal()
    finished = Signal()

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        thread_pool: QThreadPool | None = None,
    ) -> None:
        super().__init__(parent)
        self._thread_pool = thread_pool
        self._worker: ProcessingWorker | None = None
        self._cancellation: CancellationToken | None = None
        self._attempt_id: str | None = None
        self._timeline = AttemptTimeline()
        self._failure_snapshot: FailureSnapshot | None = None
        self._pause_requested = False
        self._active_phase: AttemptPhase | None = None

    @property
    def is_active(self) -> bool:
        return self._worker is not None

    @property
    def worker(self) -> ProcessingWorker | None:
        return self._worker

    @property
    def cancellation(self) -> CancellationToken | None:
        return self._cancellation

    @property
    def attempt_id(self) -> str | None:
        """Opaque id for the most recent attempt; never derived from a document."""

        return self._attempt_id

    @property
    def timeline(self) -> AttemptTimeline:
        return self._timeline

    @property
    def attempt_timeline(self) -> AttemptTimeline:
        return self._timeline

    @property
    def failure_snapshot(self) -> FailureSnapshot | None:
        return self._failure_snapshot

    @property
    def last_failure_snapshot(self) -> FailureSnapshot | None:
        return self._failure_snapshot

    def start(
        self,
        request: ProcessRequest,
        settings: AppSettings,
        *,
        work_checkpoint_root: Path | None = None,
        processor: ProcessCallable = process_document,
        early_check: EarlyCheckCallable | None = None,
    ) -> None:
        if self.is_active:
            raise RuntimeError("A physical processing task is already active.")
        cancellation = CancellationToken()
        attempt_id = secrets.token_hex(16)
        worker = ProcessingWorker(
            request,
            settings,
            cancellation,
            work_checkpoint_root,
            processor=processor,
            early_check=early_check,
            attempt_id=attempt_id,
        )
        worker.signals.stage_changed.connect(self._worker_stage_changed)
        worker.signals.improvement_progress.connect(self.improvement_progress.emit)
        worker.signals.early_check_started.connect(self._worker_early_check_started)
        worker.signals.early_check_progress.connect(self.early_check_progress.emit)
        worker.signals.early_check_completed.connect(self._worker_early_check_completed)
        worker.signals.succeeded.connect(self._worker_succeeded)
        worker.signals.failed.connect(self._worker_failed)
        worker.signals.cancelled.connect(self._worker_cancelled)
        worker.signals.finished.connect(self._worker_finished)
        self._attempt_id = attempt_id
        self._timeline = AttemptTimeline(
            (AttemptEvent(AttemptPhase.PREPARATION, AttemptEventStatus.STARTED),)
        )
        self._failure_snapshot = None
        self._pause_requested = False
        self._active_phase = AttemptPhase.PREPARATION
        self._cancellation = cancellation
        self._worker = worker
        (self._thread_pool or QThreadPool.globalInstance()).start(worker)

    def cancel(self, paused: bool = False) -> bool:
        cancellation = self._cancellation
        if cancellation is None or cancellation.is_cancelled:
            return False
        self._pause_requested = paused
        if paused and self._worker is not None:
            self._worker.request_pause()
        cancellation.cancel()
        return True

    def pause(self) -> bool:
        return self.cancel(paused=True)

    @Slot(object)
    def _worker_stage_changed(self, stage: object) -> None:
        if not isinstance(stage, ProcessStage):
            return
        phase = phase_for_process_stage(stage)
        self._begin_phase(phase)
        self.stage_changed.emit(stage)

    @Slot()
    def _worker_early_check_started(self) -> None:
        self._begin_phase(AttemptPhase.EARLY_CHECK)
        self.early_check_started.emit()

    @Slot(object)
    def _worker_early_check_completed(self, report: object) -> None:
        if isinstance(report, EarlyCheckReport) and report.blocking:
            self._terminate_phase(AttemptPhase.EARLY_CHECK, AttemptEventStatus.FAILED)
        else:
            self._complete_active_phase()
        self.early_check_completed.emit(report)

    @Slot(object)
    def _worker_succeeded(self, result: object) -> None:
        self._begin_phase(AttemptPhase.COMPLETION)
        self._complete_active_phase()
        self.succeeded.emit(result)

    @Slot(object)
    def _worker_failed(self, value: object) -> None:
        worker = self._worker
        failure = value if isinstance(value, ProcessingFailure) else None
        phase = worker.failure_phase if worker is not None else AttemptPhase.PREPARATION
        if failure is not None:
            reference = (
                worker.diagnostic_reference
                if worker is not None
                else extract_diagnostic_reference(failure.message)
            )
            self._failure_snapshot = make_failure_snapshot(
                phase,
                failure.kind.value,
                diagnostic_reference=reference,
            )
        self._terminate_phase(phase, AttemptEventStatus.FAILED)
        self.failed.emit(value)

    @Slot()
    def _worker_cancelled(self) -> None:
        worker = self._worker
        phase = worker.failure_phase if worker is not None else self._active_phase
        self._terminate_phase(
            phase or AttemptPhase.PREPARATION,
            AttemptEventStatus.PAUSED if self._pause_requested else AttemptEventStatus.CANCELLED,
        )
        self.cancelled.emit()

    @Slot()
    def _worker_finished(self) -> None:
        self._worker = None
        self._cancellation = None
        self.finished.emit()

    def _append_event(self, phase: AttemptPhase, status: AttemptEventStatus) -> None:
        if self._timeline.events:
            last = self._timeline.events[-1]
            if last.phase is phase and last.status is status:
                return
        self._timeline = self._timeline.append(AttemptEvent(phase, status))

    def _begin_phase(self, phase: AttemptPhase) -> None:
        if self._active_phase is phase:
            return
        self._complete_active_phase()
        self._append_event(phase, AttemptEventStatus.STARTED)
        self._active_phase = phase

    def _complete_active_phase(self) -> None:
        phase = self._active_phase
        if phase is None:
            return
        self._append_event(phase, AttemptEventStatus.COMPLETED)
        self._active_phase = None

    def _terminate_phase(self, phase: AttemptPhase, status: AttemptEventStatus) -> None:
        if self._active_phase is not phase:
            self._complete_active_phase()
            last = self._timeline.events[-1] if self._timeline.events else None
            if (
                last is None
                or last.phase is not phase
                or last.status
                not in {
                    AttemptEventStatus.COMPLETED,
                    AttemptEventStatus.FAILED,
                    AttemptEventStatus.CANCELLED,
                    AttemptEventStatus.PAUSED,
                }
            ):
                self._append_event(phase, AttemptEventStatus.STARTED)
        self._append_event(phase, status)
        self._active_phase = None


def _accepts_keyword(callable_object: Callable[..., object], name: str) -> bool:
    try:
        parameters = inspect.signature(callable_object).parameters
    except (TypeError, ValueError):
        return False
    return name in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )
