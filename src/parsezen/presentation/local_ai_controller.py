"""Qt controller for local Ollama discovery and setup.

The controller owns asynchronous local-AI operations without depending on any
window or presentation widget.  Presentation code consumes its signals and
decides how progress and recovery actions are shown.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from threading import Event
from typing import cast

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from parsezen.component_catalog import PRODUCT_COMPONENT_CATALOG
from parsezen.component_installer import install_product_component
from parsezen.component_readiness import ComponentCatalogEntry, inspect_component_catalog
from parsezen.errors import LocalModelUnavailableError, ParsezenError
from parsezen.local_ai_policy import ComponentCapability
from parsezen.local_models import (
    LocalAISetupCancelled,
    configure_ollama_local_only,
    detect_local_hardware,
    discover_ollama,
    install_ollama,
    restart_ollama_local_only,
    start_ollama,
)


class LocalAIAction(StrEnum):
    """Guided operations supported by the local-AI setup flow."""

    INSTALL = "install"
    START = "start"
    PROTECT = "protect"


class _DiscoverySignals(QObject):
    succeeded = Signal(object)
    component_readiness = Signal(object)
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
            try:
                readiness = inspect_component_catalog(
                    cast(
                        Mapping[ComponentCapability | str, ComponentCatalogEntry],
                        PRODUCT_COMPONENT_CATALOG,
                    ),
                    detect_local_hardware(),
                )
            except Exception:
                # Keep ordinary Ollama discovery useful when a platform probe
                # is unavailable. The UI will show conservative states.
                readiness = {}
            try:
                self.signals.component_readiness.emit(readiness)
            except RuntimeError:
                return
            # Emit ordinary discovery last so a consumer cannot destroy the
            # worker's signal object while the bounded inspection is running.
            self.signals.succeeded.emit(connection)
        finally:
            try:
                self.signals.finished.emit()
            except RuntimeError:
                # A window may close while this read-only worker is finishing;
                # Qt can delete its signal carrier during teardown.
                pass


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
    ) -> None:
        super().__init__()
        self.signals = _SetupSignals()
        self._action = action

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
            else:
                raise LocalModelUnavailableError("La acción de IA local no es válida.")
        except LocalAISetupCancelled as exc:
            self.signals.cancelled.emit(str(exc))
        except ParsezenError as exc:
            self.signals.failed.emit(str(exc))
        except Exception:
            self.signals.failed.emit("Se produjo un error inesperado al preparar la IA local.")
        else:
            self.signals.succeeded.emit("")
        finally:
            self.signals.finished.emit()


class _ComponentSignals(QObject):
    progress = Signal(object, object, str)
    succeeded = Signal(object)
    cancelled = Signal(object, str)
    failed = Signal(object, str)
    finished = Signal(object)


class _ComponentWorker(QRunnable):
    def __init__(self, capability: ComponentCapability) -> None:
        super().__init__()
        self.signals = _ComponentSignals()
        self._capability = capability
        self.cancellation = Event()

    def run(self) -> None:
        def report(percent: int | None, message: str) -> None:
            self.signals.progress.emit(self._capability, percent, message)

        try:
            install_product_component(
                self._capability,
                on_progress=report,
                cancellation=self.cancellation,
            )
        except LocalAISetupCancelled as exc:
            self.signals.cancelled.emit(self._capability, str(exc))
        except ParsezenError as exc:
            self.signals.failed.emit(self._capability, str(exc))
        except Exception:
            self.signals.failed.emit(
                self._capability,
                "Se produjo un error inesperado al preparar el componente local.",
            )
        else:
            self.signals.succeeded.emit(self._capability)
        finally:
            self.signals.finished.emit(self._capability)


class LocalAIController(QObject):
    """Own all local-AI background work and expose presentation-neutral events."""

    discovery_succeeded = Signal(object)
    component_readiness = Signal(object)
    discovery_failed = Signal(str)
    discovery_finished = Signal()
    setup_progress = Signal(object, str)
    setup_succeeded = Signal(str)
    setup_cancelled = Signal(str)
    setup_failed = Signal(str)
    setup_finished = Signal()
    component_progress = Signal(object, object, str)
    component_succeeded = Signal(object)
    component_cancelled = Signal(object, str)
    component_failed = Signal(object, str)
    component_finished = Signal(object)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        thread_pool: QThreadPool | None = None,
    ) -> None:
        super().__init__(parent)
        self._thread_pool = thread_pool
        self._discovery_worker: _DiscoveryWorker | None = None
        self._setup_worker: _SetupWorker | None = None
        self._setup_action: LocalAIAction | None = None
        self._component_worker: _ComponentWorker | None = None

    @property
    def discovering(self) -> bool:
        return self._discovery_worker is not None

    @property
    def setting_up(self) -> bool:
        return self._setup_worker is not None

    @property
    def installing_component(self) -> bool:
        return self._component_worker is not None

    @property
    def setup_action(self) -> LocalAIAction | None:
        return self._setup_action

    def discover(self, model: str | None) -> bool:
        if self.discovering or self.setting_up or self.installing_component:
            return False
        worker = _DiscoveryWorker(model)
        worker.signals.succeeded.connect(self.discovery_succeeded)
        worker.signals.component_readiness.connect(self.component_readiness)
        worker.signals.failed.connect(self.discovery_failed)
        worker.signals.finished.connect(self._discovery_done)
        self._discovery_worker = worker
        (self._thread_pool or QThreadPool.globalInstance()).start(worker)
        return True

    def setup(
        self,
        action: LocalAIAction,
    ) -> bool:
        if self.discovering or self.setting_up or self.installing_component:
            return False
        worker = _SetupWorker(action)
        worker.signals.progress.connect(self.setup_progress)
        worker.signals.succeeded.connect(self.setup_succeeded)
        worker.signals.cancelled.connect(self.setup_cancelled)
        worker.signals.failed.connect(self.setup_failed)
        worker.signals.finished.connect(self._setup_done)
        self._setup_action = action
        self._setup_worker = worker
        (self._thread_pool or QThreadPool.globalInstance()).start(worker)
        return True

    def cancel_setup(self) -> bool:
        return False

    def install_component(self, capability: ComponentCapability) -> bool:
        """Prepare one immutable product component in a worker thread."""

        if (
            not isinstance(capability, ComponentCapability)
            or self.discovering
            or self.setting_up
            or self.installing_component
        ):
            return False
        worker = _ComponentWorker(capability)
        worker.signals.progress.connect(self.component_progress)
        worker.signals.succeeded.connect(self.component_succeeded)
        worker.signals.cancelled.connect(self.component_cancelled)
        worker.signals.failed.connect(self.component_failed)
        worker.signals.finished.connect(self._component_done)
        self._component_worker = worker
        (self._thread_pool or QThreadPool.globalInstance()).start(worker)
        return True

    def cancel_component(self) -> bool:
        worker = self._component_worker
        if worker is None:
            return False
        worker.cancellation.set()
        return True

    @Slot()
    def _discovery_done(self) -> None:
        self._discovery_worker = None
        self.discovery_finished.emit()

    @Slot()
    def _setup_done(self) -> None:
        self._setup_worker = None
        self._setup_action = None
        self.setup_finished.emit()

    @Slot(object)
    def _component_done(self, capability: object) -> None:
        if self._component_worker is None:
            return
        self._component_worker = None
        self.component_finished.emit(capability)


__all__ = ["LocalAIAction", "LocalAIController"]
