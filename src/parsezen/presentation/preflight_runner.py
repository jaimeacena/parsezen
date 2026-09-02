"""Background Qt adapter for local queue preparation and forecasts."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from parsezen.application.preflight import DocumentPreflight, analyze_preflight
from parsezen.application.run_preparation import prepare_queue_run
from parsezen.application.runtime_mapping import request_and_settings_from_job
from parsezen.application.scheduler import QueueRunPlan
from parsezen.cancellation import CancellationToken
from parsezen.domain.estimates import ProcessingMetric
from parsezen.domain.jobs import AIPhase, DocumentFormat, DocumentJob, resolve_ai_profile
from parsezen.errors import ParsezenError, ProcessingCancelledError

ForecastCacheKey = tuple[object, ...]


@dataclass(frozen=True, slots=True)
class ForecastResult:
    key: ForecastCacheKey
    job_id: str
    forecast: DocumentPreflight | None


@dataclass(frozen=True, slots=True)
class ForecastBatch:
    jobs: tuple[DocumentJob, ...]
    metrics_generation: int
    results: tuple[ForecastResult, ...]


class _WorkerSignals(QObject):
    succeeded = Signal(object)
    failed = Signal(str)
    cancelled = Signal()
    finished = Signal()


class _PreparationWorker(QRunnable):
    def __init__(
        self,
        jobs: tuple[DocumentJob, ...],
        *,
        timeout_seconds: float,
        checkpoint_retention_days: int,
        metrics: tuple[ProcessingMetric, ...],
        plan: QueueRunPlan,
        cancellation: CancellationToken,
    ) -> None:
        super().__init__()
        self.signals = _WorkerSignals()
        self._jobs = jobs
        self._timeout_seconds = timeout_seconds
        self._checkpoint_retention_days = checkpoint_retention_days
        self._metrics = metrics
        self._plan = plan
        self._cancellation = cancellation

    def run(self) -> None:
        try:
            prepared = prepare_queue_run(
                self._jobs,
                timeout_seconds=self._timeout_seconds,
                checkpoint_retention_days=self._checkpoint_retention_days,
                metrics=self._metrics,
                plan=self._plan,
                cancellation=self._cancellation,
            )
        except ProcessingCancelledError:
            self.signals.cancelled.emit()
        except (OSError, ParsezenError, ValueError) as exc:
            self.signals.failed.emit(str(exc))
        except Exception:
            self.signals.failed.emit("Se produjo un error inesperado al preparar el procesamiento.")
        else:
            self.signals.succeeded.emit(prepared)
        finally:
            self.signals.finished.emit()


class _ForecastWorker(QRunnable):
    def __init__(
        self,
        jobs: tuple[DocumentJob, ...],
        planned_ids: frozenset[str],
        *,
        timeout_seconds: float,
        checkpoint_retention_days: int,
        metrics: tuple[ProcessingMetric, ...],
        metrics_generation: int,
        excluded_keys: frozenset[ForecastCacheKey],
    ) -> None:
        super().__init__()
        self.signals = _WorkerSignals()
        self._jobs = jobs
        self._planned_ids = planned_ids
        self._timeout_seconds = timeout_seconds
        self._checkpoint_retention_days = checkpoint_retention_days
        self._metrics = metrics
        self._metrics_generation = metrics_generation
        self._excluded_keys = excluded_keys

    def run(self) -> None:
        results: list[ForecastResult] = []
        for job in self._jobs:
            if (
                job.id not in self._planned_ids
                or not job.is_configured
                or job.source.format is DocumentFormat.PDF
            ):
                continue
            key = forecast_cache_key(job, self._metrics_generation)
            try:
                request, settings = request_and_settings_from_job(
                    job,
                    timeout_seconds=self._timeout_seconds,
                    checkpoint_retention_days=self._checkpoint_retention_days,
                )
                key = forecast_cache_key(job, self._metrics_generation)
                if key in self._excluded_keys:
                    continue
                forecast, _profile = analyze_preflight(
                    job,
                    request,
                    settings,
                    self._metrics,
                )
            except (OSError, ParsezenError, ValueError):
                if key not in self._excluded_keys:
                    results.append(ForecastResult(key, job.id, None))
            except Exception:
                if key not in self._excluded_keys:
                    results.append(ForecastResult(key, job.id, None))
            else:
                results.append(ForecastResult(key, job.id, forecast))
        self.signals.succeeded.emit(
            ForecastBatch(self._jobs, self._metrics_generation, tuple(results))
        )
        self.signals.finished.emit()


def forecast_cache_key(
    job: DocumentJob,
    metrics_generation: int,
) -> ForecastCacheKey:
    ai_identity = tuple(
        resolve_ai_profile(job.configuration.ai, phase).identity for phase in AIPhase
    )
    return (
        job.id,
        job.configuration_revision,
        job.source.size_bytes,
        job.source.modified_ns,
        ai_identity,
        metrics_generation,
    )


class PreflightRunner(QObject):
    """Own cancellable read-only preflight work outside the GUI thread."""

    preparation_succeeded = Signal(object)
    preparation_failed = Signal(str)
    preparation_cancelled = Signal()
    preparation_finished = Signal()
    forecasts_succeeded = Signal(object)
    forecasts_finished = Signal()

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        thread_pool: QThreadPool | None = None,
    ) -> None:
        super().__init__(parent)
        self._thread_pool = thread_pool
        self._preparation_worker: _PreparationWorker | None = None
        self._preparation_cancellation: CancellationToken | None = None
        self._forecast_worker: _ForecastWorker | None = None

    @property
    def preparing(self) -> bool:
        return self._preparation_worker is not None

    @property
    def forecasting(self) -> bool:
        return self._forecast_worker is not None

    def prepare(
        self,
        jobs: tuple[DocumentJob, ...],
        *,
        timeout_seconds: float,
        checkpoint_retention_days: int,
        metrics: tuple[ProcessingMetric, ...],
        plan: QueueRunPlan,
    ) -> bool:
        if self.preparing:
            return False
        cancellation = CancellationToken()
        worker = _PreparationWorker(
            jobs,
            timeout_seconds=timeout_seconds,
            checkpoint_retention_days=checkpoint_retention_days,
            metrics=metrics,
            plan=plan,
            cancellation=cancellation,
        )
        worker.signals.succeeded.connect(self.preparation_succeeded)
        worker.signals.failed.connect(self.preparation_failed)
        worker.signals.cancelled.connect(self.preparation_cancelled)
        worker.signals.finished.connect(self._preparation_done)
        self._preparation_worker = worker
        self._preparation_cancellation = cancellation
        (self._thread_pool or QThreadPool.globalInstance()).start(worker)
        return True

    def cancel_preparation(self) -> bool:
        if self._preparation_cancellation is None:
            return False
        self._preparation_cancellation.cancel()
        return True

    def forecast(
        self,
        jobs: tuple[DocumentJob, ...],
        planned_ids: frozenset[str],
        *,
        timeout_seconds: float,
        checkpoint_retention_days: int,
        metrics: tuple[ProcessingMetric, ...],
        metrics_generation: int,
        excluded_keys: frozenset[ForecastCacheKey],
    ) -> bool:
        if self.forecasting:
            return False
        worker = _ForecastWorker(
            jobs,
            planned_ids,
            timeout_seconds=timeout_seconds,
            checkpoint_retention_days=checkpoint_retention_days,
            metrics=metrics,
            metrics_generation=metrics_generation,
            excluded_keys=excluded_keys,
        )
        worker.signals.succeeded.connect(self.forecasts_succeeded)
        worker.signals.finished.connect(self._forecast_done)
        self._forecast_worker = worker
        (self._thread_pool or QThreadPool.globalInstance()).start(worker)
        return True

    @Slot()
    def _preparation_done(self) -> None:
        self._preparation_worker = None
        self._preparation_cancellation = None
        self.preparation_finished.emit()

    @Slot()
    def _forecast_done(self) -> None:
        self._forecast_worker = None
        self.forecasts_finished.emit()
