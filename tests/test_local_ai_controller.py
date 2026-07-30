from __future__ import annotations

from PySide6.QtCore import QRunnable

import parsezen.presentation.local_ai_controller as controller_module
from parsezen.errors import LocalModelUnavailableError
from parsezen.local_models import (
    LocalAISetupCancelled,
    OllamaConnection,
    OllamaStatus,
)
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
    monkeypatch.setattr(
        controller_module,
        "discover_ollama",
        lambda _model: connection,
    )
    controller = LocalAIController(thread_pool=ImmediatePool())
    succeeded: list[object] = []
    finished: list[bool] = []
    controller.discovery_succeeded.connect(succeeded.append)
    controller.discovery_finished.connect(lambda: finished.append(True))

    assert controller.discover(None)

    assert succeeded == [connection]
    assert finished == [True]
    assert not controller.discovering


def test_local_ai_controller_reports_recommendation_failures(monkeypatch) -> None:
    monkeypatch.setattr(
        controller_module,
        "recommend_ollama_models",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("hardware failed")),
    )
    controller = LocalAIController(thread_pool=ImmediatePool())
    failures: list[str] = []
    controller.recommendations_failed.connect(failures.append)

    assert controller.recommend()

    assert failures
    assert "añadir un modelo" in failures[0]
    assert not controller.recommending


def test_local_ai_controller_emits_setup_progress_and_completion(monkeypatch) -> None:
    deleted: list[str] = []
    monkeypatch.setattr(
        controller_module,
        "delete_ollama_model",
        deleted.append,
    )
    controller = LocalAIController(thread_pool=ImmediatePool())
    progress: list[tuple[object, str]] = []
    completed: list[str] = []
    controller.setup_progress.connect(lambda percent, message: progress.append((percent, message)))
    controller.setup_succeeded.connect(completed.append)

    assert controller.setup(
        LocalAIAction.DELETE_MODEL,
        model_id="example:latest",
    )

    assert deleted == ["example:latest"]
    assert progress[0][0] is None
    assert progress[-1][0] == 100
    assert completed == ["example:latest"]
    assert not controller.setting_up


def test_local_ai_controller_cancels_only_model_downloads() -> None:
    pool = HoldingPool()
    controller = LocalAIController(thread_pool=pool)

    assert controller.setup(
        LocalAIAction.PULL_MODEL,
        model_id="example:latest",
    )
    assert controller.setup_action is LocalAIAction.PULL_MODEL
    assert controller.cancel_setup()
    assert not controller.cancel_setup()
    assert pool.worker is not None


def test_local_ai_controller_rejects_overlapping_operations() -> None:
    pool = HoldingPool()
    controller = LocalAIController(thread_pool=pool)

    assert controller.discover(None)
    assert not controller.discover(None)
    assert not controller.recommend()
    assert not controller.setup(LocalAIAction.START)


def test_local_ai_controller_normalizes_discovery_and_recommendation_errors(
    monkeypatch,
) -> None:
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

    recommendation_failures: list[str] = []
    controller.recommendations_failed.connect(recommendation_failures.append)
    monkeypatch.setattr(
        controller_module,
        "recommend_ollama_models",
        lambda **_kwargs: (_ for _ in ()).throw(LocalModelUnavailableError("Sin hardware")),
    )
    assert controller.recommend(force_refresh=True)
    assert recommendation_failures == ["Sin hardware"]

    succeeded: list[object] = []
    controller.recommendations_succeeded.connect(succeeded.append)
    expected = object()
    monkeypatch.setattr(
        controller_module,
        "recommend_ollama_models",
        lambda **_kwargs: expected,
    )
    assert controller.recommend()
    assert succeeded == [expected]


def test_local_ai_controller_runs_setup_actions_and_normalizes_failures(
    monkeypatch,
) -> None:
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
    monkeypatch.setattr(
        controller_module,
        "pull_ollama_model",
        lambda model_id, **_kwargs: calls.append(f"pull:{model_id}"),
    )
    controller = LocalAIController(thread_pool=ImmediatePool())

    assert controller.setup(LocalAIAction.INSTALL)
    assert controller.setup(LocalAIAction.START)
    assert controller.setup(LocalAIAction.PROTECT)
    assert controller.setup(LocalAIAction.PULL_MODEL, model_id="example:latest")
    assert calls == [
        "configure",
        "install",
        "start",
        "configure",
        "start",
        "protect",
        "pull:example:latest",
    ]

    failures: list[str] = []
    cancelled: list[str] = []
    controller.setup_failed.connect(failures.append)
    controller.setup_cancelled.connect(cancelled.append)
    assert controller.setup(LocalAIAction.PULL_MODEL)
    assert "no es válida" in failures[-1]

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
