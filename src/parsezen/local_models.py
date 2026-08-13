"""Discover installed Ollama models through Ollama's native local API."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory, mkstemp
from threading import Event

import httpx

from parsezen.errors import LocalModelUnavailableError
from parsezen.settings import CONTEXT_WINDOW_PRESETS

OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DISCOVERY_TIMEOUT_SECONDS = 10.0
MAX_DISCOVERED_MODELS = 200
MAX_MODEL_ID_CHARACTERS = 200
DEFAULT_CONTEXT_WINDOW = CONTEXT_WINDOW_PRESETS[0]
OLLAMA_DOWNLOAD_URL = "https://ollama.com/download/OllamaSetup.exe"
MAX_OLLAMA_INSTALLER_BYTES = 1024 * 1024 * 1024
MAX_PULL_RESPONSE_LINE_BYTES = 64 * 1024
OLLAMA_START_TIMEOUT_SECONDS = 45.0
OLLAMA_INSTALL_TIMEOUT_SECONDS = 20 * 60
MODEL_DOWNLOAD_DISK_MARGIN = 1.35
OLLAMA_LIBRARY_URL = "https://ollama.com/library"
OLLAMA_MODEL_ID_PATTERN = re.compile(
    r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)?(?::[a-z0-9][a-z0-9._-]*)?$",
    flags=re.IGNORECASE,
)

ProgressCallback = Callable[[int | None, str], None]


class OllamaStatus(StrEnum):
    """User-facing availability states for the bundled Ollama workflow."""

    READY = "ready"
    NOT_INSTALLED = "not_installed"
    STOPPED = "stopped"
    MISSING_MODEL = "missing_model"
    LOCAL_ONLY_REQUIRED = "local_only_required"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class OllamaModel:
    """One installed model with a friendly label and safe runtime guidance."""

    model_id: str
    display_name: str
    size_bytes: int | None = None
    parameter_size: str | None = None
    quantization: str | None = None
    parent_model: str | None = None
    max_context: int | None = None
    recommended_context: int = DEFAULT_CONTEXT_WINDOW


@dataclass(frozen=True, slots=True)
class OllamaConnection:
    """Current Ollama state and models available to the interface."""

    status: OllamaStatus
    models: tuple[OllamaModel, ...] = ()
    selected_model: str | None = None
    message: str | None = None


class LocalAISetupCancelled(LocalModelUnavailableError):
    """The user cancelled a model download from the guided setup."""


class _OllamaConnectionError(LocalModelUnavailableError):
    """Ollama did not accept a connection on its fixed loopback address."""


class _OllamaResponseError(LocalModelUnavailableError):
    """Ollama answered, but its native API response was not usable."""


def list_ollama_models(
    *,
    transport: httpx.BaseTransport | None = None,
    vram_mebibytes: int | None = None,
) -> tuple[OllamaModel, ...]:
    """Return safe model metadata announced by Ollama's native ``/api/tags`` API."""
    try:
        with httpx.Client(
            timeout=DISCOVERY_TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            response = client.get(f"{OLLAMA_BASE_URL}/api/tags")
    except httpx.RequestError as exc:
        raise _OllamaConnectionError(
            "No se pudo contactar con Ollama. Comprueba que esté iniciado."
        ) from exc

    if response.is_redirect:
        raise _OllamaResponseError("Ollama intentó redirigir la comprobación y fue bloqueado.")
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise _OllamaResponseError(
            f"Ollama respondió con el estado HTTP {response.status_code}."
        ) from exc

    try:
        data = response.json()["models"]
    except (ValueError, KeyError, TypeError) as exc:
        raise _OllamaResponseError("Ollama devolvió una lista de modelos incompatible.") from exc
    if not isinstance(data, list):
        raise _OllamaResponseError("Ollama devolvió una lista de modelos incompatible.")
    if len(data) > MAX_DISCOVERED_MODELS:
        raise _OllamaResponseError(
            "Ollama anunció demasiados modelos para mostrarlos de forma segura."
        )

    detected_vram = detect_nvidia_vram_mebibytes() if vram_mebibytes is None else vram_mebibytes
    parsed: list[OllamaModel] = []
    seen_ids: set[str] = set()
    for item in data:
        model = _parse_model(item, detected_vram)
        if model is None or model.model_id in seen_ids:
            continue
        parsed.append(model)
        seen_ids.add(model.model_id)

    return tuple(sorted(parsed, key=lambda model: model.display_name.casefold()))


def discover_ollama(
    preferred_model: str | None = None,
    *,
    model_loader: Callable[[], tuple[OllamaModel, ...]] | None = None,
    installed_checker: Callable[[], bool] | None = None,
    local_only_checker: Callable[[], bool] | None = None,
) -> OllamaConnection:
    """Describe Ollama without exposing addresses or provider choices to the user."""
    loader = model_loader if model_loader is not None else list_ollama_models
    checker = installed_checker if installed_checker is not None else is_ollama_installed
    strict_local_checker = (
        local_only_checker if local_only_checker is not None else is_ollama_local_only_configured
    )
    try:
        models = loader()
    except _OllamaConnectionError as exc:
        installed = checker()
        return OllamaConnection(
            status=OllamaStatus.STOPPED if installed else OllamaStatus.NOT_INSTALLED,
            message=str(exc),
        )
    except LocalModelUnavailableError as exc:
        return OllamaConnection(status=OllamaStatus.UNAVAILABLE, message=str(exc))

    if not strict_local_checker():
        return OllamaConnection(
            status=OllamaStatus.LOCAL_ONLY_REQUIRED,
            message=(
                "Activa el modo solo local de Ollama antes de procesar documentos. "
                "Parsezen no usará la IA mientras Ollama pueda acceder a su nube."
            ),
        )
    if not models:
        return OllamaConnection(
            status=OllamaStatus.MISSING_MODEL,
            message="Ollama está preparado, pero todavía no tiene modelos instalados.",
        )
    return OllamaConnection(
        status=OllamaStatus.READY,
        models=models,
        selected_model=choose_ollama_model(models, preferred_model),
    )


def choose_ollama_model(
    models: tuple[OllamaModel, ...],
    preferred_model: str | None,
) -> str | None:
    """Restore only a saved model suitable for document transformations."""
    if not preferred_model or is_reasoning_model_id(preferred_model):
        return None
    preferred_canonical = _canonical_ollama_model_id(preferred_model)
    for model in models:
        if (
            not is_reasoning_model_id(model.model_id)
            and _canonical_ollama_model_id(model.model_id) == preferred_canonical
        ):
            return model.model_id
    return None


def validate_ollama_model_id(model_id: str) -> str:
    """Validate one transient catalog name before asking local Ollama to download it."""
    candidate = model_id.strip()
    if not candidate:
        raise LocalModelUnavailableError("Escribe el nombre de un modelo de Ollama.")
    if (
        len(candidate) > MAX_MODEL_ID_CHARACTERS
        or OLLAMA_MODEL_ID_PATTERN.fullmatch(candidate) is None
    ):
        raise LocalModelUnavailableError(
            "Usa el formato modelo:versión, por ejemplo gemma3:4b. No escribas una URL."
        )
    if is_cloud_model_id(candidate):
        raise LocalModelUnavailableError(
            "Ese nombre corresponde a un modelo cloud. Elige uno que se descargue en el equipo."
        )
    return candidate


def validate_document_model_id(model_id: str) -> str:
    """Validate an Ollama model intended to return transformed document text."""
    candidate = validate_ollama_model_id(model_id)
    if is_reasoning_model_id(candidate):
        raise LocalModelUnavailableError(
            "Ese modelo prioriza el razonamiento y no es apto para transformar documentos. "
            "Elige una variante Instruct."
        )
    return candidate


def find_ollama_executable() -> Path | None:
    """Locate the official CLI without relying on a refreshed user ``PATH``."""
    executable = shutil.which("ollama")
    if executable is not None:
        return Path(executable)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidate = Path(local_app_data) / "Programs" / "Ollama" / "ollama.exe"
        if candidate.is_file():
            return candidate
    return None


def is_ollama_installed() -> bool:
    """Check common local executable locations without starting another process."""
    return find_ollama_executable() is not None


def configure_ollama_local_only(config_path: Path | None = None) -> bool:
    """Atomically enable Ollama's documented local-only mode, preserving other settings."""
    path = config_path if config_path is not None else Path.home() / ".ollama" / "server.json"
    payload: dict[str, object] = {}
    try:
        if path.is_symlink():
            raise LocalModelUnavailableError(
                "La configuración de Ollama usa un enlace y no se modificará automáticamente."
            )
        if path.stat().st_size > 64 * 1024:
            raise LocalModelUnavailableError(
                "La configuración de Ollama es demasiado grande para actualizarla con seguridad."
            )
        raw_payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw_payload, dict):
            raise ValueError
        payload = raw_payload
    except FileNotFoundError:
        pass
    except LocalModelUnavailableError:
        raise
    except (OSError, UnicodeError) as exc:
        raise LocalModelUnavailableError(
            "No se pudo leer la configuración de privacidad de Ollama."
        ) from exc
    except (json.JSONDecodeError, ValueError):
        _backup_invalid_ollama_config(path)

    if payload.get("disable_ollama_cloud") is True:
        return False
    payload["disable_ollama_cloud"] = True
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = mkstemp(
            dir=path.parent,
            prefix=".parsezen-ollama-",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as config_file:
            json.dump(payload, config_file, ensure_ascii=False, indent=2)
            config_file.write("\n")
            config_file.flush()
            os.fsync(config_file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except (OSError, UnicodeError, TypeError) as exc:
        raise LocalModelUnavailableError("No se pudo activar el modo privado de Ollama.") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
    return True


def install_ollama(on_progress: ProgressCallback | None = None) -> None:
    """Install the official per-user Windows application without opening a browser."""
    if is_ollama_installed():
        return
    if sys.platform != "win32":
        raise LocalModelUnavailableError(
            "La instalación guiada de Ollama está disponible en Windows."
        )

    _report_progress(on_progress, None, "Preparando la instalación de Ollama…")
    winget = shutil.which("winget")
    if winget is not None:
        try:
            subprocess.run(
                [
                    winget,
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
                ],
                check=False,
                capture_output=True,
                timeout=OLLAMA_INSTALL_TIMEOUT_SECONDS,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            pass
        if _wait_for_ollama_install():
            _report_progress(on_progress, 100, "Ollama está instalado.")
            return

    _install_ollama_from_official_download(on_progress)
    if not _wait_for_ollama_install():
        raise LocalModelUnavailableError(
            "La instalación terminó, pero Windows todavía no encuentra Ollama."
        )
    _report_progress(on_progress, 100, "Ollama está instalado.")


def start_ollama(
    on_progress: ProgressCallback | None = None,
    *,
    timeout_seconds: float = OLLAMA_START_TIMEOUT_SECONDS,
) -> None:
    """Start Ollama's Windows background application and wait for its loopback API."""
    if _ollama_api_is_ready():
        if not is_ollama_local_only_configured():
            raise LocalModelUnavailableError(
                "Ollama ya está activo sin una configuración local verificable. "
                "Protégelo y reinícialo antes de procesar documentos."
            )
        return
    executable = find_ollama_executable()
    if executable is None:
        raise LocalModelUnavailableError("Ollama no está instalado.")

    application = executable.with_name("ollama app.exe")
    command = [str(application)] if application.is_file() else [str(executable), "serve"]
    environment = os.environ.copy()
    environment["OLLAMA_NO_CLOUD"] = "1"
    _report_progress(on_progress, None, "Iniciando Ollama…")
    creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
        subprocess, "DETACHED_PROCESS", 0
    )
    try:
        subprocess.Popen(  # noqa: S603 - fixed executable discovered from trusted locations
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            env=environment,
            creationflags=creation_flags,
        )
    except OSError as exc:
        raise LocalModelUnavailableError("Windows no pudo iniciar Ollama.") from exc

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _ollama_api_is_ready():
            _report_progress(on_progress, 100, "Ollama está preparado.")
            return
        time.sleep(0.5)
    raise LocalModelUnavailableError(
        "Ollama se abrió, pero no terminó de prepararse. Inténtalo de nuevo."
    )


def restart_ollama_local_only(on_progress: ProgressCallback | None = None) -> None:
    """Apply local-only configuration and restart Ollama so it takes effect."""
    configure_ollama_local_only()
    _report_progress(on_progress, None, "Reiniciando Ollama en modo privado…")
    if sys.platform == "win32":
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        for process_name in ("ollama app.exe", "ollama.exe"):
            try:
                subprocess.run(
                    ["taskkill", "/IM", process_name, "/T", "/F"],
                    check=False,
                    capture_output=True,
                    timeout=10,
                    creationflags=creation_flags,
                )
            except (OSError, subprocess.SubprocessError):
                continue
    time.sleep(0.5)
    start_ollama(on_progress)


def pull_ollama_model(
    model_id: str,
    *,
    on_progress: ProgressCallback | None = None,
    cancellation: Event | None = None,
    transport: httpx.BaseTransport | None = None,
    available_bytes: int | None = None,
    expected_download_size_bytes: int | None = None,
) -> None:
    """Download one validated local model through Ollama's streaming local API."""
    model_id = validate_ollama_model_id(model_id)
    free_bytes = _ollama_model_free_bytes() if available_bytes is None else available_bytes
    required_bytes = (
        int(expected_download_size_bytes * MODEL_DOWNLOAD_DISK_MARGIN)
        if isinstance(expected_download_size_bytes, int)
        and not isinstance(expected_download_size_bytes, bool)
        and expected_download_size_bytes > 0
        else None
    )
    if required_bytes is not None and free_bytes < required_bytes:
        required_gib = required_bytes / (1024**3)
        raise LocalModelUnavailableError(
            f"Se necesitan aproximadamente {required_gib:.1f} GB libres para instalar este modelo."
        )

    cancel_event = cancellation if cancellation is not None else Event()
    timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
    success = False
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            with client.stream(
                "POST",
                f"{OLLAMA_BASE_URL}/api/pull",
                json={"model": model_id, "stream": True},
            ) as response:
                if response.is_redirect:
                    raise LocalModelUnavailableError(
                        "Ollama intentó redirigir la descarga y fue bloqueado."
                    )
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise LocalModelUnavailableError(
                        f"Ollama no pudo descargar el modelo (HTTP {response.status_code})."
                    ) from exc
                for line in response.iter_lines():
                    if cancel_event.is_set():
                        raise LocalAISetupCancelled("Descarga del modelo cancelada.")
                    if not line:
                        continue
                    if len(line.encode("utf-8")) > MAX_PULL_RESPONSE_LINE_BYTES:
                        raise LocalModelUnavailableError(
                            "Ollama envió una respuesta de descarga inesperadamente grande."
                        )
                    update = _parse_pull_update(line)
                    error = update.get("error")
                    if isinstance(error, str) and error.strip():
                        raise LocalModelUnavailableError(
                            "Ollama no encontró ese modelo o no pudo completar su descarga. "
                            "Comprueba el nombre en el catálogo."
                        )
                    status = update.get("status")
                    if status == "success":
                        success = True
                        _report_progress(on_progress, 100, "Modelo instalado.")
                        continue
                    total = update.get("total")
                    completed = update.get("completed")
                    if (
                        required_bytes is None
                        and isinstance(total, int)
                        and not isinstance(total, bool)
                        and total > 0
                        and free_bytes < int(total * MODEL_DOWNLOAD_DISK_MARGIN)
                    ):
                        required_gib = int(total * MODEL_DOWNLOAD_DISK_MARGIN) / (1024**3)
                        raise LocalModelUnavailableError(
                            "No hay espacio suficiente para este modelo; necesita aproximadamente "
                            f"{required_gib:.1f} GB libres."
                        )
                    percent = (
                        min(99, max(0, round(completed * 100 / total)))
                        if isinstance(total, int)
                        and not isinstance(total, bool)
                        and total > 0
                        and isinstance(completed, int)
                        and not isinstance(completed, bool)
                        else None
                    )
                    _report_progress(on_progress, percent, _friendly_pull_status(status))
    except httpx.RequestError as exc:
        raise LocalModelUnavailableError(
            "Se interrumpió la conexión local con Ollama durante la descarga."
        ) from exc
    if not success:
        raise LocalModelUnavailableError("Ollama no confirmó la instalación del modelo.")


def delete_ollama_model(
    model_id: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Delete one validated local model through Ollama's native loopback API."""
    model_id = validate_ollama_model_id(model_id)
    try:
        with httpx.Client(
            timeout=DISCOVERY_TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            response = client.request(
                "DELETE",
                f"{OLLAMA_BASE_URL}/api/delete",
                json={"model": model_id},
            )
    except httpx.RequestError as exc:
        raise LocalModelUnavailableError(
            "Se interrumpió la conexión local con Ollama al eliminar el modelo."
        ) from exc
    if response.is_redirect:
        raise LocalModelUnavailableError("Ollama intentó redirigir la eliminación y fue bloqueado.")
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if response.status_code == 404:
            raise LocalModelUnavailableError(
                "Ollama ya no encuentra ese modelo instalado. Actualiza la lista."
            ) from exc
        raise LocalModelUnavailableError(
            f"Ollama no pudo eliminar el modelo (HTTP {response.status_code})."
        ) from exc


def _backup_invalid_ollama_config(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        suffix = 1
        backup = path.with_name("server.invalid.json")
        while backup.exists():
            suffix += 1
            backup = path.with_name(f"server.invalid-{suffix}.json")
        shutil.copy2(path, backup)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise LocalModelUnavailableError(
            "La configuración anterior de Ollama no es válida y no pudo conservarse."
        ) from exc


def _wait_for_ollama_install(timeout_seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if is_ollama_installed():
            return True
        time.sleep(0.5)
    return is_ollama_installed()


def _install_ollama_from_official_download(on_progress: ProgressCallback | None) -> None:
    _report_progress(on_progress, None, "Descargando el instalador oficial de Ollama…")
    with TemporaryDirectory(prefix="parsezen-ollama-") as temporary_directory:
        installer = Path(temporary_directory) / "OllamaSetup.exe"
        downloaded = 0
        try:
            with httpx.Client(
                timeout=httpx.Timeout(600.0, connect=20.0),
                follow_redirects=True,
                trust_env=True,
            ) as client:
                with client.stream("GET", OLLAMA_DOWNLOAD_URL) as response:
                    response.raise_for_status()
                    final_host = response.url.host.casefold()
                    allowed_hosts = (
                        "ollama.com",
                        "github.com",
                        "githubusercontent.com",
                    )
                    if not any(
                        final_host == host or final_host.endswith(f".{host}")
                        for host in allowed_hosts
                    ):
                        raise LocalModelUnavailableError(
                            "La descarga oficial de Ollama redirigió a un servidor inesperado."
                        )
                    content_length = response.headers.get("content-length")
                    total = (
                        int(content_length) if content_length and content_length.isdigit() else 0
                    )
                    if total > MAX_OLLAMA_INSTALLER_BYTES:
                        raise LocalModelUnavailableError(
                            "El instalador de Ollama supera el tamaño permitido."
                        )
                    with installer.open("wb") as installer_file:
                        for chunk in response.iter_bytes(1024 * 1024):
                            downloaded += len(chunk)
                            if downloaded > MAX_OLLAMA_INSTALLER_BYTES:
                                raise LocalModelUnavailableError(
                                    "El instalador de Ollama supera el tamaño permitido."
                                )
                            installer_file.write(chunk)
                            percent = round(downloaded * 100 / total) if total else None
                            _report_progress(
                                on_progress,
                                percent,
                                "Descargando el instalador oficial de Ollama…",
                            )
        except httpx.HTTPError as exc:
            raise LocalModelUnavailableError(
                "No se pudo descargar el instalador oficial de Ollama."
            ) from exc
        if downloaded < 1024 * 1024 or not _has_valid_ollama_signature(installer):
            raise LocalModelUnavailableError(
                "Windows no pudo verificar la firma oficial del instalador de Ollama."
            )
        _report_progress(on_progress, None, "Instalando Ollama…")
        try:
            completed = subprocess.run(
                [
                    str(installer),
                    "/VERYSILENT",
                    "/SUPPRESSMSGBOXES",
                    "/NORESTART",
                    "/SP-",
                ],
                check=False,
                capture_output=True,
                timeout=OLLAMA_INSTALL_TIMEOUT_SECONDS,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise LocalModelUnavailableError("Windows no pudo instalar Ollama.") from exc
        if completed.returncode != 0:
            raise LocalModelUnavailableError("El instalador de Ollama no pudo completarse.")


def _has_valid_ollama_signature(path: Path) -> bool:
    environment = os.environ.copy()
    environment["PARSEZEN_OLLAMA_INSTALLER"] = str(path)
    command = (
        "$signature = Get-AuthenticodeSignature -LiteralPath "
        "$env:PARSEZEN_OLLAMA_INSTALLER; "
        "if ($signature.Status -ne 'Valid' -or "
        "$signature.SignerCertificate.Subject -notlike '*Ollama Inc.*') { exit 1 }"
    )
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            check=False,
            capture_output=True,
            timeout=30,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _ollama_api_is_ready() -> bool:
    try:
        with httpx.Client(
            timeout=1.0,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = client.get(f"{OLLAMA_BASE_URL}/api/version")
    except httpx.RequestError:
        return False
    return response.status_code == 200 and not response.is_redirect


def _ollama_model_free_bytes() -> int:
    configured = os.environ.get("OLLAMA_MODELS")
    model_root = Path(configured) if configured else Path.home() / ".ollama" / "models"
    existing_root = model_root
    while not existing_root.exists() and existing_root != existing_root.parent:
        existing_root = existing_root.parent
    try:
        return shutil.disk_usage(existing_root).free
    except OSError as exc:
        raise LocalModelUnavailableError(
            "No se pudo comprobar el espacio disponible para el modelo."
        ) from exc


def _parse_pull_update(line: str) -> dict[str, object]:
    try:
        update = json.loads(line)
    except json.JSONDecodeError as exc:
        raise LocalModelUnavailableError(
            "Ollama devolvió un progreso de descarga incompatible."
        ) from exc
    if not isinstance(update, dict):
        raise LocalModelUnavailableError("Ollama devolvió un progreso de descarga incompatible.")
    return update


def _friendly_pull_status(status: object) -> str:
    if not isinstance(status, str):
        return "Descargando el modelo…"
    lowered = status.casefold()
    if "manifest" in lowered:
        return "Preparando el modelo…"
    if "verifying" in lowered:
        return "Comprobando la descarga…"
    if "writing" in lowered:
        return "Finalizando la instalación…"
    if "removing" in lowered:
        return "Liberando espacio temporal…"
    return "Descargando el modelo…"


def _report_progress(
    callback: ProgressCallback | None,
    percent: int | None,
    message: str,
) -> None:
    if callback is not None:
        callback(percent, message)


def is_ollama_local_only_configured(
    *,
    config_path: Path | None = None,
) -> bool:
    """Return whether Ollama's persistent server configuration disables cloud features."""

    path = config_path if config_path is not None else Path.home() / ".ollama" / "server.json"
    try:
        if path.is_symlink() or path.stat().st_size > 64 * 1024:
            return False
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("disable_ollama_cloud") is True


def detect_nvidia_vram_mebibytes() -> int | None:
    """Return the largest NVIDIA GPU memory size, or ``None`` on unsupported systems."""
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.total",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=creation_flags,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    values: list[int] = []
    for line in completed.stdout.splitlines():
        try:
            values.append(int(line.strip()))
        except ValueError:
            continue
    return max(values, default=None)


def context_for_model(models: tuple[OllamaModel, ...], model_id: str | None) -> int:
    """Return the automatic recommendation for the selected model."""
    for model in models:
        if model.model_id == model_id:
            return model.recommended_context
    return DEFAULT_CONTEXT_WINDOW


def friendly_model_name(model_id: str, details: dict[str, object] | None = None) -> str:
    """Turn an Ollama identifier into a short name suitable for non-technical users."""
    details = details if details is not None else {}
    family = details.get("family")
    parameter_size = details.get("parameter_size")
    quantization = details.get("quantization_level")
    if isinstance(family, str) and family.strip():
        family_name = _friendly_family(family)
        parts = [family_name]
        if isinstance(parameter_size, str) and parameter_size.strip():
            parts.append(re.sub(r"\.0(?=[A-Z]$)", "", parameter_size.strip().upper()))
        lowered_id = model_id.casefold()
        if "instruct" in lowered_id:
            parts.append("Instruct")
        if isinstance(quantization, str) and quantization.strip():
            parts.append(_friendly_quantization(quantization))
        name = " ".join(dict.fromkeys(parts))
    else:
        raw, _separator, tag = model_id.partition(":")
        raw = raw.replace("_", " ").replace("-", " ")
        name = " ".join(part.capitalize() for part in raw.split()) or "Modelo local"
        if re.fullmatch(r"\d+(?:\.\d+)?b", tag, flags=re.IGNORECASE):
            name = f"{name} {tag.upper()}"

    return name


def is_cloud_model_id(model_id: str) -> bool:
    """Reject Ollama's documented cloud tags even when reached through the local API."""
    _name, separator, tag = model_id.casefold().rpartition(":")
    return bool(separator) and (tag == "cloud" or tag.endswith("-cloud"))


def is_reasoning_model_id(model_id: str) -> bool:
    """Identify model variants that may spend the response budget on hidden reasoning."""
    normalized = model_id.strip().casefold()
    repository, separator, _tag = normalized.rpartition(":")
    model_name = (repository if separator else normalized).rsplit("/", 1)[-1]
    instruction_variant = any(
        marker in normalized for marker in ("instruct", "instruction", "chat")
    )
    if re.match(r"^qwen3(?:$|[_-])", model_name):
        return not instruction_variant
    return bool(
        re.search(
            r"(?:^|[/_.:-])(?:deepseek-r1|r1|qwq|reasoning|thinking|magistral)"
            r"(?:$|[/_.:-])",
            normalized,
        )
    )


def _canonical_ollama_model_id(model_id: str) -> str:
    normalized = model_id.strip().casefold()
    final_segment = normalized.rsplit("/", 1)[-1]
    return normalized if ":" in final_segment else f"{normalized}:latest"


def _parse_model(item: object, vram_mebibytes: int | None) -> OllamaModel | None:
    if not isinstance(item, dict):
        return None
    raw_id = item.get("model", item.get("name"))
    if not isinstance(raw_id, str):
        return None
    model_id = raw_id.strip()
    if (
        not model_id
        or len(model_id) > MAX_MODEL_ID_CHARACTERS
        or any(character in model_id for character in "\r\n\0")
        or is_cloud_model_id(model_id)
    ):
        return None

    raw_details = item.get("details")
    details = raw_details if isinstance(raw_details, dict) else {}
    size = item.get("size")
    size_bytes = size if isinstance(size, int) and not isinstance(size, bool) and size > 0 else None
    parent = details.get("parent_model")
    parent_model = parent.strip() if isinstance(parent, str) and parent.strip() else None
    max_context_value = details.get("context_length")
    max_context = (
        max_context_value
        if isinstance(max_context_value, int)
        and not isinstance(max_context_value, bool)
        and max_context_value > 0
        else None
    )
    recommendation = recommend_context_window(
        size_bytes=size_bytes,
        vram_mebibytes=vram_mebibytes,
        max_context=max_context,
    )
    parameter_size = details.get("parameter_size")
    quantization = details.get("quantization_level")
    return OllamaModel(
        model_id=model_id,
        display_name=friendly_model_name(model_id, details),
        size_bytes=size_bytes,
        parameter_size=parameter_size if isinstance(parameter_size, str) else None,
        quantization=quantization if isinstance(quantization, str) else None,
        parent_model=parent_model,
        max_context=max_context,
        recommended_context=recommendation,
    )


def recommend_context_window(
    *,
    size_bytes: int | None,
    vram_mebibytes: int | None,
    max_context: int | None,
) -> int:
    """Choose a conservative preset from available GPU memory and model size."""
    recommendation = DEFAULT_CONTEXT_WINDOW
    if size_bytes is not None and vram_mebibytes is not None:
        model_mebibytes = size_bytes / (1024 * 1024)
        free_after_weights = vram_mebibytes - model_mebibytes
        if free_after_weights >= 6_000:
            recommendation = 16_384
        elif free_after_weights >= 3_000:
            recommendation = 8_192

    if max_context is not None:
        recommendation = min(recommendation, max_context)
    return recommendation


def _friendly_family(family: str) -> str:
    compact = re.sub(r"[^a-z0-9]+", "", family.casefold())
    known = {
        "qwen3": "Qwen3",
        "qwen2": "Qwen2",
        "llama": "Llama",
        "llama3": "Llama 3",
        "gemma": "Gemma",
        "gemma2": "Gemma 2",
        "gemma3": "Gemma 3",
        "mistral": "Mistral",
        "phi3": "Phi-3",
        "phi4": "Phi-4",
    }
    return known.get(compact, family.strip().title())


def _friendly_quantization(quantization: str) -> str:
    value = quantization.strip().upper()
    match = re.fullmatch(r"(Q\d+)(?:_[A-Z0-9_]+)?", value)
    return match.group(1) if match else value.replace("_", "-")
