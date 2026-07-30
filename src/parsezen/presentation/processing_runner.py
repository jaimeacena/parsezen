"""Qt adapter that owns one physical document-processing task at a time."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from parsezen.application.recovery import ProcessingFailure
from parsezen.cancellation import CancellationToken
from parsezen.domain.outcomes import EarlyCheckReport
from parsezen.errors import EarlyCheckError, ParsezenError, ProcessingCancelledError
from parsezen.processing import ProcessRequest, ProcessResult, process_document
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
    ) -> None:
        super().__init__()
        self.signals = ProcessingWorkerSignals()
        self._request = request
        self._settings = settings
        self._cancellation = cancellation
        self._work_checkpoint_root = work_checkpoint_root
        self._processor = processor
        self._early_check = early_check

    def run(self) -> None:
        try:
            if self._early_check is not None:
                self.signals.early_check_started.emit()
                report = self._early_check(
                    self._request,
                    self._settings,
                    cancellation=self._cancellation,
                    work_checkpoint_root=self._work_checkpoint_root,
                    on_progress=self.signals.early_check_progress.emit,
                )
                self.signals.early_check_completed.emit(report)
                if report.blocking:
                    raise EarlyCheckError(
                        "La comprobación temprana detectó un riesgo material en las páginas "
                        "representativas. " + " ".join(report.blocking_reasons)
                    )
            arguments = {
                "on_stage": self.signals.stage_changed.emit,
                "on_progress": self.signals.improvement_progress.emit,
                "settings": self._settings,
                "cancellation": self._cancellation,
            }
            if "work_checkpoint_root" in inspect.signature(self._processor).parameters:
                arguments["work_checkpoint_root"] = self._work_checkpoint_root
            result = self._processor(self._request, **arguments)
        except ProcessingCancelledError:
            self.signals.cancelled.emit()
        except ParsezenError as exc:
            LOGGER.warning(
                "processing_task_failed error_type=%s",
                type(exc).__name__,
            )
            self.signals.failed.emit(ProcessingFailure.from_exception(exc))
        except Exception as exc:
            LOGGER.error(
                "unexpected_processing_task_failure error_type=%s",
                type(exc).__name__,
            )
            self.signals.failed.emit(ProcessingFailure.from_exception(exc))
        else:
            self.signals.succeeded.emit(result)
        finally:
            self.signals.finished.emit()


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

    @property
    def is_active(self) -> bool:
        return self._worker is not None

    @property
    def worker(self) -> ProcessingWorker | None:
        return self._worker

    @property
    def cancellation(self) -> CancellationToken | None:
        return self._cancellation

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
        worker = ProcessingWorker(
            request,
            settings,
            cancellation,
            work_checkpoint_root,
            processor=processor,
            early_check=early_check,
        )
        worker.signals.stage_changed.connect(self.stage_changed.emit)
        worker.signals.improvement_progress.connect(self.improvement_progress.emit)
        worker.signals.early_check_started.connect(self.early_check_started.emit)
        worker.signals.early_check_progress.connect(self.early_check_progress.emit)
        worker.signals.early_check_completed.connect(self.early_check_completed.emit)
        worker.signals.succeeded.connect(self.succeeded.emit)
        worker.signals.failed.connect(self.failed.emit)
        worker.signals.cancelled.connect(self.cancelled.emit)
        worker.signals.finished.connect(self._worker_finished)
        self._cancellation = cancellation
        self._worker = worker
        (self._thread_pool or QThreadPool.globalInstance()).start(worker)

    def cancel(self) -> bool:
        cancellation = self._cancellation
        if cancellation is None or cancellation.is_cancelled:
            return False
        cancellation.cancel()
        return True

    @Slot()
    def _worker_finished(self) -> None:
        self._worker = None
        self._cancellation = None
        self.finished.emit()
