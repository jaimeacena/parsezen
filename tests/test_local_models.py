from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from threading import Event

import httpx
import pytest

import parsezen.local_models as local_models_module
from parsezen.errors import LocalModelUnavailableError
from parsezen.local_models import (
    OLLAMA_BASE_URL,
    LocalAISetupCancelled,
    OllamaConnection,
    OllamaModel,
    OllamaStatus,
    choose_ollama_model,
    configure_ollama_local_only,
    context_for_model,
    delete_ollama_model,
    discover_ollama,
    friendly_model_name,
    install_ollama,
    is_ollama_local_only_configured,
    is_reasoning_model_id,
    list_ollama_models,
    pull_ollama_model,
    recommend_context_window,
    restart_ollama_local_only,
    start_ollama,
    validate_document_model_id,
    validate_ollama_model_id,
)


def test_lists_native_ollama_models_with_friendly_names() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "model": "gemma3:4b",
                        "size": 4_280_418_296,
                        "details": {
                            "family": "gemma3",
                            "parameter_size": "4.0B",
                            "quantization_level": "Q8_0",
                            "context_length": 262_144,
                        },
                    },
                    {
                        "model": "qwen3:4b-instruct-2507-q8_0",
                        "size": 4_280_418_281,
                        "details": {
                            "family": "qwen3",
                            "parameter_size": "4.0B",
                            "quantization_level": "Q8_0",
                        },
                    },
                ]
            },
        )

    models = list_ollama_models(
        transport=httpx.MockTransport(respond),
        vram_mebibytes=8_151,
    )

    assert len(models) == 2
    assert models[0].model_id == "gemma3:4b"
    assert models[0].display_name == "Gemma 3 4B Q8"
    assert models[0].recommended_context == 8_192
    assert models[1].model_id == "qwen3:4b-instruct-2507-q8_0"
    assert requests[0].url == f"{OLLAMA_BASE_URL}/api/tags"


def test_filters_unsafe_or_duplicate_model_identifiers() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "models": [
                    {"model": "safe-model"},
                    {"name": "safe-model"},
                    {"model": "bad\nmodel"},
                    {"model": "x" * 201},
                    {"model": 42},
                    {"model": "glm-4.7:cloud"},
                    {"model": "gpt-oss:120b-cloud"},
                ]
            },
        )
    )

    assert [
        model.model_id for model in list_ollama_models(transport=transport, vram_mebibytes=4_096)
    ] == ["safe-model"]


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(200, json={"data": []}), "incompatible"),
        (httpx.Response(200, content=b"not json"), "incompatible"),
        (httpx.Response(503), "HTTP 503"),
        (
            httpx.Response(307, headers={"Location": "https://example.com/models"}),
            "redirigir",
        ),
    ],
)
def test_explains_incompatible_or_failed_model_discovery(
    response: httpx.Response,
    message: str,
) -> None:
    with pytest.raises(LocalModelUnavailableError, match=message):
        list_ollama_models(
            transport=httpx.MockTransport(lambda _request: response),
            vram_mebibytes=4_096,
        )


def test_rejects_an_unreasonably_large_model_list() -> None:
    payload = {"models": [{"model": f"model-{index}"} for index in range(201)]}

    with pytest.raises(LocalModelUnavailableError, match="demasiados modelos"):
        list_ollama_models(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)),
            vram_mebibytes=4_096,
        )


def test_discovery_reports_that_ollama_is_installed_but_stopped() -> None:
    def unavailable() -> tuple[OllamaModel, ...]:
        raise local_models_module._OllamaConnectionError("No se pudo contactar con Ollama.")

    result = discover_ollama(model_loader=unavailable, installed_checker=lambda: True)

    assert result.status is OllamaStatus.STOPPED
    assert result.models == ()


def test_finds_ollama_from_the_current_windows_path(tmp_path: Path, monkeypatch) -> None:
    executable = tmp_path / "ollama.exe"
    monkeypatch.setattr(local_models_module.shutil, "which", lambda _name: str(executable))

    assert local_models_module.find_ollama_executable() == executable
    assert local_models_module.is_ollama_installed()


def test_discovery_reports_that_ollama_is_not_installed() -> None:
    def unavailable() -> tuple[OllamaModel, ...]:
        raise local_models_module._OllamaConnectionError("No se pudo contactar con Ollama.")

    result = discover_ollama(model_loader=unavailable, installed_checker=lambda: False)

    assert result.status is OllamaStatus.NOT_INSTALLED


def test_discovery_reports_a_missing_model_without_treating_ollama_as_broken() -> None:
    result = discover_ollama(model_loader=tuple, local_only_checker=lambda: True)

    assert result.status is OllamaStatus.MISSING_MODEL
    assert "todavía no tiene modelos" in (result.message or "")


def test_discovery_requires_local_only_mode_before_offering_model_installation() -> None:
    result = discover_ollama(model_loader=tuple, local_only_checker=lambda: False)

    assert result.status is OllamaStatus.LOCAL_ONLY_REQUIRED


def test_discovery_restores_only_a_previously_selected_installed_model() -> None:
    models = (
        OllamaModel("model-a", "Model A"),
        OllamaModel("model-b", "Model B"),
    )

    result = discover_ollama(
        "model-b",
        model_loader=lambda: models,
        local_only_checker=lambda: True,
    )

    assert result == OllamaConnection(
        status=OllamaStatus.READY,
        models=models,
        selected_model="model-b",
    )
    assert choose_ollama_model(models, None) is None


def test_discovery_refuses_models_until_ollama_cloud_is_disabled() -> None:
    models = (OllamaModel("model-a", "Model A"),)

    result = discover_ollama(
        model_loader=lambda: models,
        local_only_checker=lambda: False,
    )

    assert result.status is OllamaStatus.LOCAL_ONLY_REQUIRED
    assert result.models == ()
    assert "solo local" in (result.message or "")


def test_detects_ollama_local_only_environment_or_server_configuration(
    tmp_path: Path,
) -> None:
    config = tmp_path / "server.json"
    config.write_text(json.dumps({"disable_ollama_cloud": True}), encoding="utf-8")

    assert is_ollama_local_only_configured(environment={}, config_path=config)
    assert is_ollama_local_only_configured(
        environment={"OLLAMA_NO_CLOUD": "1"},
        config_path=tmp_path / "missing.json",
    )
    config.write_text(json.dumps({"disable_ollama_cloud": False}), encoding="utf-8")
    assert not is_ollama_local_only_configured(environment={}, config_path=config)


def test_guided_local_only_configuration_preserves_existing_values(tmp_path: Path) -> None:
    config = tmp_path / ".ollama" / "server.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"keep_alive": "10m"}), encoding="utf-8")

    assert configure_ollama_local_only(config)
    assert json.loads(config.read_text(encoding="utf-8")) == {
        "keep_alive": "10m",
        "disable_ollama_cloud": True,
    }
    assert not configure_ollama_local_only(config)


def test_guided_local_only_configuration_backs_up_invalid_json(tmp_path: Path) -> None:
    config = tmp_path / ".ollama" / "server.json"
    config.parent.mkdir()
    config.write_text("{broken", encoding="utf-8")

    configure_ollama_local_only(config)

    assert json.loads(config.read_text(encoding="utf-8")) == {"disable_ollama_cloud": True}
    assert (config.parent / "server.invalid.json").read_text(encoding="utf-8") == "{broken"


def test_guided_install_uses_the_exact_silent_winget_package(monkeypatch) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(local_models_module, "is_ollama_installed", lambda: False)
    monkeypatch.setattr(local_models_module, "_wait_for_ollama_install", lambda: True)
    monkeypatch.setattr(local_models_module.shutil, "which", lambda name: "winget.exe")
    monkeypatch.setattr(local_models_module.subprocess, "run", run)

    install_ollama()

    assert commands == [
        [
            "winget.exe",
            "install",
            "--id",
            "Ollama.Ollama",
            "--exact",
            "--scope",
            "user",
            "--silent",
            "--accept-package-agreements",
            "--accept-source-agreements",
            "--disable-interactivity",
        ]
    ]


def test_guided_install_falls_back_to_the_verified_official_installer(monkeypatch) -> None:
    fallback_calls: list[object] = []
    progress: list[tuple[int | None, str]] = []

    monkeypatch.setattr(local_models_module, "is_ollama_installed", lambda: False)
    monkeypatch.setattr(local_models_module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        local_models_module,
        "_wait_for_ollama_install",
        lambda: True,
    )
    monkeypatch.setattr(
        local_models_module,
        "_install_ollama_from_official_download",
        lambda callback: fallback_calls.append(callback),
    )

    install_ollama(lambda percent, message: progress.append((percent, message)))

    assert len(fallback_calls) == 1
    assert progress[-1] == (100, "Ollama está instalado.")


def test_official_installer_download_is_bounded_verified_and_silent(monkeypatch) -> None:
    payload = b"x" * (1024 * 1024)
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            content=payload,
            headers={"content-length": str(len(payload))},
        )
    )
    client = httpx.Client(transport=transport)
    commands: list[list[str]] = []
    progress: list[tuple[int | None, str]] = []
    monkeypatch.setattr(local_models_module.httpx, "Client", lambda **_kwargs: client)
    monkeypatch.setattr(local_models_module, "_has_valid_ollama_signature", lambda _path: True)
    monkeypatch.setattr(
        local_models_module.subprocess,
        "run",
        lambda command, **_kwargs: (
            commands.append(command) or subprocess.CompletedProcess(command, 0)
        ),
    )

    local_models_module._install_ollama_from_official_download(
        lambda percent, message: progress.append((percent, message))
    )

    assert commands[0][1:] == ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"]
    assert progress[0][1] == "Descargando el instalador oficial de Ollama…"
    assert progress[-1][1] == "Instalando Ollama…"


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(503), "descargar"),
        (
            httpx.Response(
                200,
                content=b"x",
                headers={"content-length": str(local_models_module.MAX_OLLAMA_INSTALLER_BYTES + 1)},
            ),
            "tamaño permitido",
        ),
    ],
)
def test_official_installer_rejects_failed_or_oversized_downloads(
    monkeypatch,
    response: httpx.Response,
    message: str,
) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _request: response))
    monkeypatch.setattr(local_models_module.httpx, "Client", lambda **_kwargs: client)

    with pytest.raises(LocalModelUnavailableError, match=message):
        local_models_module._install_ollama_from_official_download(None)


def test_official_installer_rejects_an_unverified_or_failed_executable(monkeypatch) -> None:
    payload = b"x" * (1024 * 1024)
    client_type = httpx.Client

    def client() -> httpx.Client:
        return client_type(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=payload))
        )

    monkeypatch.setattr(local_models_module.httpx, "Client", lambda **_kwargs: client())
    monkeypatch.setattr(local_models_module, "_has_valid_ollama_signature", lambda _path: False)
    with pytest.raises(LocalModelUnavailableError, match="firma oficial"):
        local_models_module._install_ollama_from_official_download(None)

    monkeypatch.setattr(local_models_module, "_has_valid_ollama_signature", lambda _path: True)
    monkeypatch.setattr(
        local_models_module.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 5),
    )
    with pytest.raises(LocalModelUnavailableError, match="no pudo completarse"):
        local_models_module._install_ollama_from_official_download(None)

    monkeypatch.setattr(
        local_models_module.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("blocked")),
    )
    with pytest.raises(LocalModelUnavailableError, match="no pudo instalar"):
        local_models_module._install_ollama_from_official_download(None)


def test_signature_readiness_and_model_disk_helpers_handle_system_failures(
    tmp_path: Path,
    monkeypatch,
) -> None:
    installer = tmp_path / "OllamaSetup.exe"
    installer.write_bytes(b"exe")
    monkeypatch.setattr(
        local_models_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0),
    )
    assert local_models_module._has_valid_ollama_signature(installer)
    monkeypatch.setattr(
        local_models_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1),
    )
    assert not local_models_module._has_valid_ollama_signature(installer)
    monkeypatch.setattr(
        local_models_module.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("missing")),
    )
    assert not local_models_module._has_valid_ollama_signature(installer)

    ready_client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200)))
    monkeypatch.setattr(local_models_module.httpx, "Client", lambda **_kwargs: ready_client)
    assert local_models_module._ollama_api_is_ready()

    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path / "missing" / "models"))
    assert local_models_module._ollama_model_free_bytes() > 0


def test_guided_install_is_explicitly_limited_to_windows(monkeypatch) -> None:
    monkeypatch.setattr(local_models_module, "is_ollama_installed", lambda: False)
    monkeypatch.setattr(local_models_module.sys, "platform", "linux")

    with pytest.raises(LocalModelUnavailableError, match="disponible en Windows"):
        install_ollama()


def test_guided_start_uses_the_windows_app_and_forces_local_only_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    executable = tmp_path / "ollama.exe"
    application = tmp_path / "ollama app.exe"
    executable.write_bytes(b"exe")
    application.write_bytes(b"app")
    readiness: Iterator[bool] = iter((False, True))
    launches: list[tuple[list[str], dict[str, object]]] = []

    monkeypatch.setattr(local_models_module, "_ollama_api_is_ready", lambda: next(readiness))
    monkeypatch.setattr(local_models_module, "find_ollama_executable", lambda: executable)
    monkeypatch.setattr(local_models_module.time, "sleep", lambda _seconds: None)

    def launch(command: list[str], **kwargs):
        launches.append((command, kwargs))
        return object()

    monkeypatch.setattr(local_models_module.subprocess, "Popen", launch)

    start_ollama(timeout_seconds=1)

    assert launches[0][0] == [str(application)]
    assert launches[0][1]["env"]["OLLAMA_NO_CLOUD"] == "1"


def test_guided_start_explains_when_ollama_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(local_models_module, "_ollama_api_is_ready", lambda: False)
    monkeypatch.setattr(local_models_module, "find_ollama_executable", lambda: None)

    with pytest.raises(LocalModelUnavailableError, match="no está instalado"):
        start_ollama()


def test_guided_restart_applies_privacy_and_restarts_known_processes(monkeypatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(local_models_module.sys, "platform", "win32")
    monkeypatch.setattr(
        local_models_module,
        "configure_ollama_local_only",
        lambda: calls.append("configure"),
    )
    monkeypatch.setattr(local_models_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        local_models_module,
        "start_ollama",
        lambda callback: calls.append(("start", callback)),
    )

    def stop(command: list[str], **_kwargs) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(local_models_module.subprocess, "run", stop)

    restart_ollama_local_only()

    assert calls[0] == "configure"
    assert ["taskkill", "/IM", "ollama app.exe", "/T", "/F"] in calls
    assert ["taskkill", "/IM", "ollama.exe", "/T", "/F"] in calls
    assert calls[-1] == ("start", None)


def test_guided_model_pull_reports_progress_and_uses_only_the_local_api() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url == f"{OLLAMA_BASE_URL}/api/pull"
        assert json.loads(request.content) == {"model": "qwen3:4b", "stream": True}
        return httpx.Response(
            200,
            content=(
                b'{"status":"downloading","total":100,"completed":25}\n'
                b'{"status":"verifying sha256 digest"}\n'
                b'{"status":"success"}\n'
            ),
        )

    progress: list[tuple[int | None, str]] = []
    pull_ollama_model(
        "qwen3:4b",
        on_progress=lambda percent, message: progress.append((percent, message)),
        transport=httpx.MockTransport(respond),
        available_bytes=10_000_000_000,
    )

    assert requests[0].method == "POST"
    assert progress[0] == (25, "Descargando el modelo…")
    assert progress[-1] == (100, "Modelo instalado.")


def test_model_delete_uses_only_the_local_ollama_api() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    delete_ollama_model(
        "qwen3:4b",
        transport=httpx.MockTransport(respond),
    )

    assert len(requests) == 1
    assert requests[0].method == "DELETE"
    assert requests[0].url == f"{OLLAMA_BASE_URL}/api/delete"
    assert json.loads(requests[0].content) == {"model": "qwen3:4b"}


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(307, headers={"Location": "https://example.com"}), "redirigir"),
        (httpx.Response(404), "no encuentra"),
        (httpx.Response(503), "HTTP 503"),
    ],
)
def test_model_delete_explains_invalid_local_responses(
    response: httpx.Response,
    message: str,
) -> None:
    with pytest.raises(LocalModelUnavailableError, match=message):
        delete_ollama_model(
            "qwen3:4b",
            transport=httpx.MockTransport(lambda _request: response),
        )


def test_guided_model_pull_can_be_cancelled() -> None:
    cancellation = Event()
    cancellation.set()
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, content=b'{"status":"downloading"}\n')
    )

    with pytest.raises(LocalAISetupCancelled, match="cancelada"):
        pull_ollama_model(
            "qwen3:1.7b",
            cancellation=cancellation,
            transport=transport,
            available_bytes=10_000_000_000,
        )


@pytest.mark.parametrize(
    ("model_id", "available_bytes", "expected_size", "message"),
    [
        ("https://ollama.com/model", 10_000_000_000, None, "formato"),
        ("qwen3:8b", 1, 5_200_000_000, "GB libres"),
    ],
)
def test_model_pull_rejects_unsafe_names_or_insufficient_space(
    model_id: str,
    available_bytes: int,
    expected_size: int | None,
    message: str,
) -> None:
    with pytest.raises(LocalModelUnavailableError, match=message):
        pull_ollama_model(
            model_id,
            available_bytes=available_bytes,
            expected_download_size_bytes=expected_size,
        )


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(307, headers={"Location": "https://example.com"}), "redirigir"),
        (httpx.Response(503), "HTTP 503"),
        (httpx.Response(200, content=b"not-json\n"), "incompatible"),
        (httpx.Response(200, content=b'{"status":"downloading"}\n'), "no confirmó"),
        (httpx.Response(200, content=b'{"error":"failed"}\n'), "no pudo completar"),
    ],
)
def test_guided_model_pull_explains_invalid_local_responses(
    response: httpx.Response,
    message: str,
) -> None:
    with pytest.raises(LocalModelUnavailableError, match=message):
        pull_ollama_model(
            "qwen3:1.7b",
            transport=httpx.MockTransport(lambda _request: response),
            available_bytes=10_000_000_000,
        )


def test_guided_model_pull_accepts_any_valid_local_catalog_name() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=(
                b'{"status":"downloading","total":2000000000,"completed":1000000000}\n'
                b'{"status":"success"}\n'
            ),
        )

    pull_ollama_model(
        "example/modern-model:7b-q4_K_M",
        transport=httpx.MockTransport(respond),
        available_bytes=10_000_000_000,
    )

    assert json.loads(requests[0].content)["model"] == "example/modern-model:7b-q4_K_M"


def test_custom_model_pull_stops_when_ollama_announces_an_unsafe_size() -> None:
    response = httpx.Response(
        200,
        content=b'{"status":"downloading","total":10000000000,"completed":1}\n',
    )

    with pytest.raises(LocalModelUnavailableError, match="espacio suficiente"):
        pull_ollama_model(
            "modern-model:latest",
            transport=httpx.MockTransport(lambda _request: response),
            available_bytes=1_000_000_000,
        )


@pytest.mark.parametrize(
    ("raw_model_id", "expected"),
    [
        (" gemma3:4b ", "gemma3:4b"),
        ("example/model:latest", "example/model:latest"),
        ("llama3.2", "llama3.2"),
    ],
)
def test_validates_official_ollama_model_name_format(
    raw_model_id: str,
    expected: str,
) -> None:
    assert validate_ollama_model_id(raw_model_id) == expected


@pytest.mark.parametrize("model_id", ["", "two words", "https://host/model", "model:cloud"])
def test_rejects_unsafe_or_cloud_model_names(model_id: str) -> None:
    with pytest.raises(LocalModelUnavailableError):
        validate_ollama_model_id(model_id)


def test_saved_model_maps_only_to_the_same_canonical_model() -> None:
    models = (
        OllamaModel(
            "qwen3:4b-instruct-2507-q8_0",
            "Qwen3 4B Instruct Q8",
            recommended_context=8_192,
        ),
    )

    assert choose_ollama_model(models, "qwen3:4b-instruct-2507-q8_0") == models[0].model_id
    assert context_for_model(models, models[0].model_id) == 8_192


def test_model_without_tag_matches_the_latest_id_returned_by_ollama() -> None:
    models = (OllamaModel("gemma3:latest", "Gemma3"),)

    assert choose_ollama_model(models, "gemma3") == "gemma3:latest"


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("qwen3:4b", True),
        ("qwen3:4b-q8_0", True),
        ("vendor/qwen3.5:9b", False),
        ("deepseek-r1:8b", True),
        ("qwq:32b", True),
        ("phi4-reasoning:14b", True),
        ("magistral:24b", True),
        ("qwen3:4b-instruct", False),
        ("qwen3-instruct:4b", False),
        ("gemma3:4b", False),
        ("llama3.2:3b", False),
    ],
)
def test_identifies_reasoning_models_that_are_not_document_transformers(
    model_id: str,
    expected: bool,
) -> None:
    assert is_reasoning_model_id(model_id) is expected


def test_reasoning_model_cannot_be_restored_or_validated_for_documents() -> None:
    models = (
        OllamaModel("qwen3:4b", "Qwen3 4B"),
        OllamaModel("qwen3:4b-instruct", "Qwen3 4B Instruct"),
    )

    assert choose_ollama_model(models, "qwen3:4b") is None
    assert choose_ollama_model(models, "qwen3:4b-instruct") == "qwen3:4b-instruct"
    with pytest.raises(LocalModelUnavailableError, match="Instruct"):
        validate_document_model_id("qwen3:4b")


@pytest.mark.parametrize(
    ("size_bytes", "vram", "maximum", "expected"),
    [
        (4_280_000_000, 8_151, 262_144, 8_192),
        (4_280_000_000, 6_144, 262_144, 4_096),
        (4_280_000_000, 16_384, 262_144, 16_384),
        (4_280_000_000, 16_384, 8_192, 8_192),
        (4_280_000_000, 16_384, 2_048, 2_048),
        (None, None, None, 4_096),
    ],
)
def test_context_recommendation_is_conservative_and_capped_by_the_model(
    size_bytes: int | None,
    vram: int | None,
    maximum: int | None,
    expected: int,
) -> None:
    assert (
        recommend_context_window(
            size_bytes=size_bytes,
            vram_mebibytes=vram,
            max_context=maximum,
        )
        == expected
    )


def test_friendly_name_uses_only_the_canonical_ollama_name() -> None:
    assert friendly_model_name("qwen3:latest") == "Qwen3"
    assert friendly_model_name("qwen3:4b") == "Qwen3 4B"
