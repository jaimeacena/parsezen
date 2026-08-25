from __future__ import annotations

from PySide6.QtCore import QRunnable

import parsezen.presentation.local_ai_controller as controller_module
from parsezen.component_readiness import ReadinessStatus
from parsezen.errors import LocalModelUnavailableError
from parsezen.local_ai_policy import ComponentCapability
from parsezen.local_models import LocalAISetupCancelled, OllamaConnection, OllamaStatus
from parsezen.presentation.local_ai_controller import LocalAIAction, LocalAIController


class ImmediatePool:
    def start(self, worker: QRunnable) -> None:
        worker.run()


class HoldingPool:
    def __init__(self) -> None:
        self.worker: QRunnable | None = None

    def start(self, worker: QRunnable) -> None:
        self.worker = worker


def test_local_ai_controller_discovers_without_a_window(monkeypatch) -> None:
    connection = OllamaConnection(OllamaStatus.MISSING_MODEL)
    monkeypatch.setattr(controller_module, "discover_ollama", lambda _model: connection)
    controller = LocalAIController(thread_pool=ImmediatePool())
    succeeded: list[object] = []
    finished: list[bool] = []
    controller.discovery_succeeded.connect(succeeded.append)
    controller.discovery_finished.connect(lambda: finished.append(True))

    assert controller.discover(None)

    assert succeeded == [connection]
    assert finished == [True]
    assert not controller.discovering


def test_local_ai_controller_emits_fixed_component_readiness(monkeypatch) -> None:
    connection = OllamaConnection(OllamaStatus.READY)
    readiness = {ComponentCapability.TRANSLATION: ReadinessStatus.PREPARED}
    monkeypatch.setattr(controller_module, "discover_ollama", lambda _model: connection)
    monkeypatch.setattr(controller_module, "detect_local_hardware", lambda: object())
    monkeypatch.setattr(
        controller_module,
        "inspect_component_catalog",
        lambda _catalog, _hardware: readiness,
    )
    controller = LocalAIController(thread_pool=ImmediatePool())
    observed: list[object] = []
    controller.component_readiness.connect(observed.append)

    assert controller.discover(None)

    assert observed == [readiness]


def test_local_ai_controller_installs_only_a_catalogued_capability(monkeypatch) -> None:
    capability = ComponentCapability.REVIEW
    monkeypatch.setattr(
        controller_module,
        "install_product_component",
        lambda selected, **_kwargs: capability if selected is capability else None,
    )
    controller = LocalAIController(thread_pool=ImmediatePool())
    succeeded: list[object] = []
    finished: list[object] = []
    controller.component_succeeded.connect(succeeded.append)
    controller.component_finished.connect(finished.append)

    assert controller.install_component(capability)
    assert not controller.install_component("review")
    assert succeeded == [capability]
    assert finished == [capability]
    assert not controller.installing_component


def test_local_ai_controller_normalizes_discovery_errors(monkeypatch) -> None:
    failures: list[str] = []
    controller = LocalAIController(thread_pool=ImmediatePool())
    controller.discovery_failed.connect(failures.append)
    monkeypatch.setattr(
        controller_module,
        "discover_ollama",
        lambda _model: (_ for _ in ()).throw(LocalModelUnavailableError("Sin servicio")),
    )
    assert controller.discover(None)
    assert failures == ["Sin servicio"]

    monkeypatch.setattr(
        controller_module,
        "discover_ollama",
        lambda _model: (_ for _ in ()).throw(RuntimeError("unexpected")),
    )
    assert controller.discover(None)
    assert "inesperado" in failures[-1]


def test_local_ai_controller_runs_only_ollama_setup_actions(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        controller_module,
        "configure_ollama_local_only",
        lambda: calls.append("configure"),
    )
    monkeypatch.setattr(
        controller_module,
        "install_ollama",
        lambda report: (calls.append("install"), report(20, "Instalando")),
    )
    monkeypatch.setattr(
        controller_module,
        "start_ollama",
        lambda report: (calls.append("start"), report(100, "Iniciado")),
    )
    monkeypatch.setattr(
        controller_module,
        "restart_ollama_local_only",
        lambda report: (calls.append("protect"), report(100, "Protegido")),
    )
    controller = LocalAIController(thread_pool=ImmediatePool())
    completed: list[str] = []
    controller.setup_succeeded.connect(completed.append)

    assert controller.setup(LocalAIAction.INSTALL)
    assert controller.setup(LocalAIAction.START)
    assert controller.setup(LocalAIAction.PROTECT)

    assert calls == [
        "configure",
        "install",
        "start",
        "configure",
        "start",
        "protect",
    ]
    assert completed == ["", "", ""]


def test_local_ai_controller_does_not_start_arbitrary_model_operations() -> None:
    pool = HoldingPool()
    controller = LocalAIController(thread_pool=pool)

    assert controller.cancel_setup() is False
    assert not controller.install_component("translation")


def test_local_ai_controller_rejects_overlapping_operations() -> None:
    pool = HoldingPool()
    controller = LocalAIController(thread_pool=pool)

    assert controller.discover(None)
    assert not controller.discover(None)
    assert not controller.setup(LocalAIAction.START)


def test_local_ai_controller_normalizes_setup_failures(monkeypatch) -> None:
    failures: list[str] = []
    cancelled: list[str] = []
    controller = LocalAIController(thread_pool=ImmediatePool())
    controller.setup_failed.connect(failures.append)
    controller.setup_cancelled.connect(cancelled.append)
    monkeypatch.setattr(
        controller_module,
        "start_ollama",
        lambda _report: (_ for _ in ()).throw(LocalAISetupCancelled("Cancelado")),
    )
    assert controller.setup(LocalAIAction.START)
    assert cancelled == ["Cancelado"]

    monkeypatch.setattr(
        controller_module,
        "start_ollama",
        lambda _report: (_ for _ in ()).throw(RuntimeError("unexpected")),
    )
    assert controller.setup(LocalAIAction.START)
    assert "inesperado" in failures[-1]
