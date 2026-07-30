"""Qt controller for local Ollama discovery, recommendations and setup.

The controller owns asynchronous local-AI operations without depending on any
window or presentation widget.  Presentation code consumes its signals and
decides how progress and recovery actions are shown.
"""

from __future__ import annotations

from enum import StrEnum
from threading import Event

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from parsezen.errors import LocalModelUnavailableError, ParsezenError
from parsezen.local_models import (
    LocalAISetupCancelled,
    configure_ollama_local_only,
    delete_ollama_model,
    discover_ollama,
    install_ollama,
    pull_ollama_model,
    restart_ollama_local_only,
    start_ollama,
)
from parsezen.model_recommendations import recommend_ollama_models


class LocalAIAction(StrEnum):
    """Guided operations supported by the local-AI setup flow."""

    INSTALL = "install"
    START = "start"
    PROTECT = "protect"
    PULL_MODEL = "pull_model"
    DELETE_MODEL = "delete_model"


class _DiscoverySignals(QObject):
    succeeded = Signal(object)
    failed = Signal(str)
    finished = Signal()


class _DiscoveryWorker(QRunnable):
    def __init__(self, model: str | None) -> None:
        super().__init__()
        self.signals = _DiscoverySignals()
        self._model = model

    def run(self) -> None:
        try:
            connection = discover_ollama(self._model)
        except ParsezenError as exc:
            self.signals.failed.emit(str(exc))
        except Exception:
            self.signals.failed.emit("Se produjo un error inesperado al comprobar la IA local.")
        else:
            self.signals.succeeded.emit(connection)
        finally:
            self.signals.finished.emit()


class _RecommendationSignals(QObject):
    succeeded = Signal(object)
    failed = Signal(str)
    finished = Signal()


class _RecommendationWorker(QRunnable):
    def __init__(self, *, force_refresh: bool) -> None:
        super().__init__()
        self.signals = _RecommendationSignals()
        self._force_refresh = force_refresh

    def run(self) -> None:
        try:
            recommendations = (
                recommend_ollama_models(force_refresh=True)
                if self._force_refresh
                else recommend_ollama_models()
            )
        except ParsezenError as exc:
            self.signals.failed.emit(str(exc))
        except Exception:
            self.signals.failed.emit(
                "No se pudieron calcular recomendaciones ahora. "
                "Puedes añadir un modelo por su nombre."
            )
        else:
            self.signals.succeeded.emit(recommendations)
        finally:
            self.signals.finished.emit()


class _SetupSignals(QObject):
    progress = Signal(object, str)
    succeeded = Signal(str)
    cancelled = Signal(str)
    failed = Signal(str)
    finished = Signal()


class _SetupWorker(QRunnable):
    def __init__(
        self,
        action: LocalAIAction,
        *,
        model_id: str | None,
        cancellation: Event,
        expected_download_size_bytes: int | None,
    ) -> None:
        super().__init__()
        self.signals = _SetupSignals()
        self._action = action
        self._model_id = model_id
        self._cancellation = cancellation
        self._expected_download_size_bytes = expected_download_size_bytes

    def run(self) -> None:
        def report(percent: int | None, message: str) -> None:
            self.signals.progress.emit(percent, message)

        try:
            if self._action is LocalAIAction.INSTALL:
                configure_ollama_local_only()
                install_ollama(report)
                start_ollama(report)
            elif self._action is LocalAIAction.START:
                configure_ollama_local_only()
                start_ollama(report)
            elif self._action is LocalAIAction.PROTECT:
                restart_ollama_local_only(report)
            elif self._action is LocalAIAction.PULL_MODEL and self._model_id is not None:
                pull_ollama_model(
                    self._model_id,
                    on_progress=report,
                    cancellation=self._cancellation,
                    expected_download_size_bytes=self._expected_download_size_bytes,
                )
            elif self._action is LocalAIAction.DELETE_MODEL and self._model_id is not None:
                report(None, "Eliminando el modelo…")
                delete_ollama_model(self._model_id)
                report(100, "Modelo eliminado.")
            else:
                raise LocalModelUnavailableError("La acción de IA local no es válida.")
        except LocalAISetupCancelled as exc:
            self.signals.cancelled.emit(str(exc))
        except ParsezenError as exc:
            self.signals.failed.emit(str(exc))
        except Exception:
            self.signals.failed.emit("Se produjo un error inesperado al preparar la IA local.")
        else:
            self.signals.succeeded.emit(self._model_id or "")
        finally:
            self.signals.finished.emit()


class LocalAIController(QObject):
    """Own all local-AI background work and expose presentation-neutral events."""

    discovery_succeeded = Signal(object)
    discovery_failed = Signal(str)
    discovery_finished = Signal()
    recommendations_succeeded = Signal(object)
    recommendations_failed = Signal(str)
    recommendations_finished = Signal()
    setup_progress = Signal(object, str)
    setup_succeeded = Signal(str)
    setup_cancelled = Signal(str)
    setup_failed = Signal(str)
    setup_finished = Signal()

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        thread_pool: QThreadPool | None = None,
    ) -> None:
        super().__init__(parent)
        self._thread_pool = thread_pool
        self._discovery_worker: _DiscoveryWorker | None = None
        self._recommendation_worker: _RecommendationWorker | None = None
        self._setup_worker: _SetupWorker | None = None
        self._setup_cancellation: Event | None = None
        self._setup_action: LocalAIAction | None = None

    @property
    def discovering(self) -> bool:
        return self._discovery_worker is not None

    @property
    def recommending(self) -> bool:
        return self._recommendation_worker is not None

    @property
    def setting_up(self) -> bool:
        return self._setup_worker is not None

    @property
    def setup_action(self) -> LocalAIAction | None:
        return self._setup_action

    def discover(self, model: str | None) -> bool:
        if self.discovering or self.recommending or self.setting_up:
            return False
        worker = _DiscoveryWorker(model)
        worker.signals.succeeded.connect(self.discovery_succeeded)
        worker.signals.failed.connect(self.discovery_failed)
        worker.signals.finished.connect(self._discovery_done)
        self._discovery_worker = worker
        (self._thread_pool or QThreadPool.globalInstance()).start(worker)
        return True

    def recommend(self, *, force_refresh: bool = False) -> bool:
        if self.discovering or self.recommending or self.setting_up:
            return False
        worker = _RecommendationWorker(force_refresh=force_refresh)
        worker.signals.succeeded.connect(self.recommendations_succeeded)
        worker.signals.failed.connect(self.recommendations_failed)
        worker.signals.finished.connect(self._recommendations_done)
        self._recommendation_worker = worker
        (self._thread_pool or QThreadPool.globalInstance()).start(worker)
        return True

    def setup(
        self,
        action: LocalAIAction,
        *,
        model_id: str | None = None,
        expected_download_size_bytes: int | None = None,
    ) -> bool:
        if self.discovering or self.recommending or self.setting_up:
            return False
        cancellation = Event()
        worker = _SetupWorker(
            action,
            model_id=model_id,
            cancellation=cancellation,
            expected_download_size_bytes=expected_download_size_bytes,
        )
        worker.signals.progress.connect(self.setup_progress)
        worker.signals.succeeded.connect(self.setup_succeeded)
        worker.signals.cancelled.connect(self.setup_cancelled)
        worker.signals.failed.connect(self.setup_failed)
        worker.signals.finished.connect(self._setup_done)
        self._setup_cancellation = cancellation
        self._setup_action = action
        self._setup_worker = worker
        (self._thread_pool or QThreadPool.globalInstance()).start(worker)
        return True

    def cancel_setup(self) -> bool:
        cancellation = self._setup_cancellation
        if (
            cancellation is None
            or cancellation.is_set()
            or self._setup_action is not LocalAIAction.PULL_MODEL
        ):
            return False
        cancellation.set()
        return True

    @Slot()
    def _discovery_done(self) -> None:
        self._discovery_worker = None
        self.discovery_finished.emit()

    @Slot()
    def _recommendations_done(self) -> None:
        self._recommendation_worker = None
        self.recommendations_finished.emit()

    @Slot()
    def _setup_done(self) -> None:
        self._setup_worker = None
        self._setup_cancellation = None
        self._setup_action = None
        self.setup_finished.emit()


__all__ = ["LocalAIAction", "LocalAIController"]
