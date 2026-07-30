"""Hardware-aware Ollama recommendations provided locally by a managed llmfit binary."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import os
import platform
import re
import subprocess
import sys
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from tempfile import mkstemp
from urllib.parse import urlparse

import httpx
from platformdirs import user_cache_path, user_data_path

from parsezen import APP_STORAGE_NAME
from parsezen.errors import LocalModelUnavailableError, ModelRecommendationError
from parsezen.local_models import (
    friendly_model_name,
    is_cloud_model_id,
    is_reasoning_model_id,
    validate_ollama_model_id,
)

LLMFIT_REPOSITORY = "AlexsJones/llmfit"
LLMFIT_RELEASE_API_URL = f"https://api.github.com/repos/{LLMFIT_REPOSITORY}/releases?per_page=10"
LLMFIT_COMPONENT_DIRECTORY = "llmfit"
LLMFIT_EXECUTABLE_NAME = "llmfit.exe"
LLMFIT_LICENSE_FILENAME = "LICENSE.llmfit.txt"
LLMFIT_METADATA_FILENAME = "component.json"
RECOMMENDATION_CACHE_FILENAME = "model-recommendations.json"
RECOMMENDATION_CACHE_SCHEMA_VERSION = 5
OLLAMA_REGISTRY_BASE_URL = "https://registry.ollama.ai"
LLMFIT_UPDATE_INTERVAL = timedelta(days=7)
RECOMMENDATION_CACHE_LIFETIME = timedelta(hours=24)
STALE_RECOMMENDATION_LIFETIME = timedelta(days=90)
LLMFIT_COMMAND_TIMEOUT_SECONDS = 45
MAX_RELEASE_METADATA_BYTES = 1024 * 1024
MAX_LLMFIT_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_LLMFIT_EXECUTABLE_BYTES = 64 * 1024 * 1024
MAX_LLMFIT_LICENSE_BYTES = 128 * 1024
MAX_LLMFIT_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_OLLAMA_MANIFEST_BYTES = 256 * 1024
MAX_OLLAMA_MANIFEST_LAYERS = 64
MAX_OLLAMA_MODEL_BYTES = 10**13
MAX_RECOMMENDATION_MODELS = 3
LLMFIT_QUERY_LIMIT = 500
LLMFIT_USE_CASE = "chat"

_VERSION_PATTERN = re.compile(r"^v?(\d+\.\d+\.\d+)$")
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_ASSET_HOSTS = frozenset(
    {
        "github.com",
        "release-assets.githubusercontent.com",
        "objects.githubusercontent.com",
    }
)

CommandRunner = Callable[[Path], str]
ManifestLoader = Callable[[str], int | None]
logger = logging.getLogger(__name__)


class RecommendationOrigin(StrEnum):
    """How the current recommendation result was obtained."""

    LIVE = "live"
    CACHE = "cache"


class RecommendationRole(StrEnum):
    """Why one model is included in the deliberately diverse short list."""

    BALANCED = "balanced"
    FASTER = "faster"
    CAPACITY = "capacity"
    ALTERNATIVE = "alternative"


@dataclass(frozen=True, slots=True)
class ModelRecommendation:
    """One llmfit-ranked model that can be installed directly through Ollama."""

    model_id: str
    display_name: str
    description: str
    download_size_bytes: int | None
    recommended_context: int | None
    estimated_tokens_per_second: float | None
    memory_required_gb: float | None
    score: float | None
    parameter_count_b: float | None = None
    role: RecommendationRole = RecommendationRole.BALANCED

    def menu_label(self, position: int) -> str:
        """Return a compact non-technical label for the installation menu."""
        size = (
            f" · {_format_decimal(self.download_size_bytes / 1_000_000_000)} GB"
            if self.download_size_bytes is not None
            else ""
        )
        suffix = " — mejor opción" if self.role is RecommendationRole.BALANCED else ""
        return f"{self.display_name}{size}{suffix}"


@dataclass(frozen=True, slots=True)
class ModelRecommendations:
    """Three purposeful model profiles and the hardware summary shown to the user."""

    models: tuple[ModelRecommendation, ...]
    hardware_summary: str
    llmfit_version: str
    generated_at: datetime
    origin: RecommendationOrigin = RecommendationOrigin.LIVE


@dataclass(frozen=True, slots=True)
class LlmfitComponent:
    """One verified managed llmfit executable."""

    executable: Path
    version: str
    executable_sha256: str
    license_path: Path
    license_sha256: str
    checked_at: datetime


@dataclass(frozen=True, slots=True)
class _ReleaseAsset:
    version: str
    url: str
    archive_sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class _LlmfitPackage:
    executable: bytes
    license_text: bytes


def get_llmfit_component_directory() -> Path:
    """Return the private per-user directory for the independently updated helper."""
    return user_data_path(APP_STORAGE_NAME, appauthor=False) / LLMFIT_COMPONENT_DIRECTORY


def get_recommendation_cache_path() -> Path:
    """Return the private cache used when the recommendation service is temporarily unavailable."""
    return user_cache_path(APP_STORAGE_NAME, appauthor=False) / RECOMMENDATION_CACHE_FILENAME


def ensure_llmfit(
    *,
    component_directory: Path | None = None,
    transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
    platform_name: str | None = None,
    machine_name: str | None = None,
) -> LlmfitComponent:
    """Install or refresh llmfit, retaining a verified existing copy if the network fails."""
    current_time = _as_utc(now or datetime.now(UTC))
    directory = component_directory or get_llmfit_component_directory()
    installed = _load_installed_component(directory)
    if installed is not None and _is_recent(
        installed.checked_at, current_time, LLMFIT_UPDATE_INTERVAL
    ):
        return installed

    try:
        asset = _fetch_latest_release(
            transport=transport,
            platform_name=platform_name or sys.platform,
            machine_name=machine_name or platform.machine(),
        )
        if installed is not None and _version_tuple(installed.version) >= _version_tuple(
            asset.version
        ):
            refreshed = replace(installed, checked_at=current_time)
            _write_component_metadata(directory, refreshed)
            return refreshed
        archive = _download_release_asset(asset, transport=transport)
        package = _extract_llmfit_package(archive)
        component = _install_llmfit_executable(
            directory,
            package,
            version=asset.version,
            checked_at=current_time,
        )
        return component
    except (ModelRecommendationError, OSError, subprocess.SubprocessError) as exc:
        logger.warning(
            "llmfit_prepare_failed error_type=%s reason=%s",
            type(exc).__name__,
            str(exc) if isinstance(exc, ModelRecommendationError) else "system_error",
        )
        if installed is not None:
            return installed
        raise ModelRecommendationError(
            "No se pudo preparar el recomendador automático. "
            "Comprueba la conexión o añade un modelo por su nombre."
        ) from None


def recommend_ollama_models(
    *,
    component_directory: Path | None = None,
    cache_path: Path | None = None,
    transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
    force_refresh: bool = False,
    executable: Path | None = None,
    component_version: str | None = None,
    runner: CommandRunner | None = None,
) -> ModelRecommendations:
    """Return up to three diverse, locally computed Ollama recommendations."""
    current_time = _as_utc(now or datetime.now(UTC))
    resolved_cache_path = cache_path or get_recommendation_cache_path()
    cached = _load_recommendation_cache(resolved_cache_path)
    if (
        not force_refresh
        and cached is not None
        and _is_recent(cached.generated_at, current_time, RECOMMENDATION_CACHE_LIFETIME)
    ):
        return replace(cached, origin=RecommendationOrigin.CACHE)

    try:
        if executable is None:
            component = ensure_llmfit(
                component_directory=component_directory,
                transport=transport,
                now=current_time,
            )
            executable = component.executable
            component_version = component.version
        version = _normalize_version(component_version or "0.0.0")
        output = (runner or _run_llmfit)(executable)
        manifest_cache: dict[str, int | None] = {}

        def load_manifest(model_id: str) -> int | None:
            if model_id not in manifest_cache:
                if len(manifest_cache) >= MAX_RECOMMENDATION_MODELS:
                    return None
                manifest_cache[model_id] = _fetch_ollama_manifest_size(
                    model_id,
                    transport=transport,
                )
            return manifest_cache[model_id]

        recommendations = parse_llmfit_recommendations(
            output,
            version=version,
            generated_at=current_time,
            manifest_loader=load_manifest,
        )
        try:
            _write_recommendation_cache(resolved_cache_path, recommendations)
        except ModelRecommendationError:
            pass
        return recommendations
    except (ModelRecommendationError, OSError, subprocess.SubprocessError) as exc:
        logger.warning(
            "model_recommendation_failed error_type=%s reason=%s",
            type(exc).__name__,
            str(exc) if isinstance(exc, ModelRecommendationError) else "system_error",
        )
        if cached is not None and _is_recent(
            cached.generated_at,
            current_time,
            STALE_RECOMMENDATION_LIFETIME,
        ):
            return replace(cached, origin=RecommendationOrigin.CACHE)
        raise ModelRecommendationError(
            "No se pudieron calcular recomendaciones ahora. "
            "Puedes añadir cualquier modelo escribiendo su nombre."
        ) from None


def parse_llmfit_recommendations(
    output: str,
    *,
    version: str,
    generated_at: datetime | None = None,
    manifest_loader: ManifestLoader | None = None,
) -> ModelRecommendations:
    """Validate, consolidate and resolve ranked model families available through Ollama."""
    if len(output.encode("utf-8")) > MAX_LLMFIT_OUTPUT_BYTES:
        raise ModelRecommendationError("llmfit devolvió demasiados datos.")
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ModelRecommendationError("llmfit devolvió una respuesta incompatible.") from exc
    if not isinstance(payload, dict):
        raise ModelRecommendationError("llmfit devolvió una respuesta incompatible.")
    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        raise ModelRecommendationError("llmfit no devolvió una lista de modelos.")

    grouped_models = _group_ranked_models(raw_models)
    selected_groups = _select_recommendation_groups(grouped_models)
    recommendations: list[ModelRecommendation] = []
    seen_ids: set[str] = set()
    for group, role in selected_groups:
        recommendation = _parse_recommendation_group(
            group,
            manifest_loader=manifest_loader,
            role=role,
        )
        if recommendation is None:
            continue
        canonical_id = _canonical_model_id(recommendation.model_id)
        if canonical_id in seen_ids:
            continue
        seen_ids.add(canonical_id)
        recommendations.append(recommendation)
    if not recommendations:
        raise ModelRecommendationError(
            "llmfit no encontró ahora mismo un modelo de Ollama adecuado para este equipo."
        )
    system = payload.get("system")
    hardware_summary = _hardware_summary(system)
    return ModelRecommendations(
        models=tuple(recommendations),
        hardware_summary=hardware_summary,
        llmfit_version=_normalize_version(version),
        generated_at=_as_utc(generated_at or datetime.now(UTC)),
    )


def _fetch_latest_release(
    *,
    transport: httpx.BaseTransport | None,
    platform_name: str,
    machine_name: str,
) -> _ReleaseAsset:
    target = _release_target(platform_name, machine_name)
    with httpx.Client(
        timeout=15.0,
        follow_redirects=False,
        trust_env=False,
        transport=transport,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "Parsezen"},
    ) as client:
        try:
            with client.stream("GET", LLMFIT_RELEASE_API_URL) as response:
                response.raise_for_status()
                content = _read_limited_response(response, MAX_RELEASE_METADATA_BYTES)
        except httpx.HTTPError as exc:
            raise ModelRecommendationError("No se pudo comprobar la versión de llmfit.") from exc
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ModelRecommendationError("La actualización de llmfit no es válida.") from exc
    if not isinstance(payload, list):
        raise ModelRecommendationError("La actualización de llmfit no es válida.")
    for release in payload:
        candidate = _compatible_release_asset(release, target=target)
        if candidate is not None:
            return candidate
    raise ModelRecommendationError(
        "No hay ahora mismo una versión compatible y completa de llmfit."
    )


def _compatible_release_asset(release: object, *, target: str) -> _ReleaseAsset | None:
    """Return a verifiable asset, ignoring drafts and partially published releases."""
    if (
        not isinstance(release, dict)
        or release.get("draft") is True
        or release.get("prerelease") is True
    ):
        return None
    try:
        version = _normalize_version(release.get("tag_name"))
    except ModelRecommendationError:
        return None
    expected_name = f"llmfit-v{version}-{target}.zip"
    assets = release.get("assets")
    if not isinstance(assets, list):
        return None
    matching = [
        asset for asset in assets if isinstance(asset, dict) and asset.get("name") == expected_name
    ]
    if len(matching) != 1:
        return None
    asset = matching[0]
    url = asset.get("browser_download_url")
    digest = asset.get("digest")
    size = asset.get("size")
    if (
        not isinstance(url, str)
        or not isinstance(digest, str)
        or not digest.startswith("sha256:")
        or not isinstance(size, int)
        or isinstance(size, bool)
        or not 0 < size <= MAX_LLMFIT_ARCHIVE_BYTES
    ):
        return None
    archive_sha256 = digest.removeprefix("sha256:").casefold()
    if not _SHA256_PATTERN.fullmatch(archive_sha256):
        return None
    try:
        _validate_release_url(url, version, expected_name)
    except ModelRecommendationError:
        return None
    return _ReleaseAsset(
        version=version,
        url=url,
        archive_sha256=archive_sha256,
        size=size,
    )


def _download_release_asset(
    asset: _ReleaseAsset,
    *,
    transport: httpx.BaseTransport | None,
) -> bytes:
    with httpx.Client(
        timeout=httpx.Timeout(connect=15.0, read=60.0, write=15.0, pool=15.0),
        follow_redirects=True,
        trust_env=False,
        transport=transport,
        headers={"User-Agent": "Parsezen"},
    ) as client:
        try:
            with client.stream("GET", asset.url) as response:
                response.raise_for_status()
                for redirect in (*response.history, response):
                    _validate_download_host(str(redirect.url))
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_LLMFIT_ARCHIVE_BYTES:
                        raise ModelRecommendationError(
                            "La descarga de llmfit supera el límite seguro."
                        )
        except httpx.HTTPError as exc:
            raise ModelRecommendationError("No se pudo descargar llmfit.") from exc
    if len(data) != asset.size:
        raise ModelRecommendationError("La descarga de llmfit quedó incompleta.")
    if hashlib.sha256(data).hexdigest() != asset.archive_sha256:
        raise ModelRecommendationError(
            "La descarga de llmfit no superó la comprobación de integridad."
        )
    return bytes(data)


def _fetch_ollama_manifest_size(
    model_id: str,
    *,
    transport: httpx.BaseTransport | None,
) -> int | None:
    """Verify one inferred tag against Ollama's registry and return its real layer size."""
    try:
        validated = validate_ollama_model_id(model_id)
    except LocalModelUnavailableError:
        return None
    repository, separator, tag = validated.rpartition(":")
    if not separator:
        repository = validated
        tag = "latest"
    registry_repository = repository if "/" in repository else f"library/{repository}"
    url = f"{OLLAMA_REGISTRY_BASE_URL}/v2/{registry_repository}/manifests/{tag}"
    try:
        with httpx.Client(
            timeout=10.0,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={
                "Accept": "application/vnd.docker.distribution.manifest.v2+json",
                "User-Agent": "Parsezen",
            },
        ) as client:
            with client.stream("GET", url) as response:
                if response.status_code == 404 or response.is_redirect:
                    return None
                response.raise_for_status()
                content = _read_limited_response(response, MAX_OLLAMA_MANIFEST_BYTES)
    except (httpx.HTTPError, ModelRecommendationError):
        return None
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("schemaVersion") != 2
        or not isinstance(payload.get("layers"), list)
    ):
        return None
    layers = payload["layers"]
    if not 0 < len(layers) <= MAX_OLLAMA_MANIFEST_LAYERS:
        return None
    total_size = 0
    has_model_layer = False
    for layer in layers:
        if not isinstance(layer, dict):
            return None
        digest = layer.get("digest")
        size = layer.get("size")
        media_type = layer.get("mediaType")
        if (
            not isinstance(digest, str)
            or not digest.startswith("sha256:")
            or _SHA256_PATTERN.fullmatch(digest.removeprefix("sha256:").casefold()) is None
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 < size <= MAX_OLLAMA_MODEL_BYTES
            or not isinstance(media_type, str)
        ):
            return None
        has_model_layer = has_model_layer or media_type == "application/vnd.ollama.image.model"
        total_size += size
        if total_size > MAX_OLLAMA_MODEL_BYTES:
            return None
    return total_size if has_model_layer else None


def _extract_llmfit_package(archive: bytes) -> _LlmfitPackage:
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as package:
            candidates = [
                info
                for info in package.infolist()
                if not info.is_dir()
                and Path(info.filename).name.casefold() == LLMFIT_EXECUTABLE_NAME
            ]
            if len(candidates) != 1:
                raise ModelRecommendationError(
                    "El paquete de llmfit no contiene el ejecutable esperado."
                )
            candidate = candidates[0]
            if (
                candidate.flag_bits & 0x1
                or candidate.file_size <= 0
                or candidate.file_size > MAX_LLMFIT_EXECUTABLE_BYTES
                or candidate.compress_size <= 0
                or candidate.file_size > candidate.compress_size * 200
            ):
                raise ModelRecommendationError(
                    "El ejecutable de llmfit no supera los límites seguros."
                )
            with package.open(candidate) as executable_file:
                executable = executable_file.read(MAX_LLMFIT_EXECUTABLE_BYTES + 1)
            license_candidates = [
                info
                for info in package.infolist()
                if not info.is_dir()
                and Path(info.filename).name.casefold() in {"license", "license.txt"}
            ]
            if len(license_candidates) != 1:
                raise ModelRecommendationError(
                    "El paquete de llmfit no contiene una licencia identificable."
                )
            license_info = license_candidates[0]
            if not 0 < license_info.file_size <= MAX_LLMFIT_LICENSE_BYTES:
                raise ModelRecommendationError(
                    "La licencia de llmfit no supera los límites seguros."
                )
            with package.open(license_info) as license_file:
                license_text = license_file.read(MAX_LLMFIT_LICENSE_BYTES + 1)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ModelRecommendationError("El paquete descargado de llmfit no es válido.") from exc
    if len(executable) > MAX_LLMFIT_EXECUTABLE_BYTES or not executable.startswith(b"MZ"):
        raise ModelRecommendationError("El ejecutable descargado de llmfit no es válido.")
    if len(license_text) > MAX_LLMFIT_LICENSE_BYTES:
        raise ModelRecommendationError("La licencia descargada de llmfit no es válida.")
    try:
        decoded_license = license_text.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ModelRecommendationError("La licencia descargada de llmfit no es válida.") from exc
    if "MIT License" not in decoded_license or "Copyright" not in decoded_license:
        raise ModelRecommendationError("La licencia descargada de llmfit no es la esperada.")
    return _LlmfitPackage(executable=executable, license_text=license_text)


def _install_llmfit_executable(
    directory: Path,
    package: _LlmfitPackage,
    *,
    version: str,
    checked_at: datetime,
) -> LlmfitComponent:
    temporary_path: Path | None = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = mkstemp(
            dir=directory,
            prefix=".llmfit-",
            suffix=".exe",
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as executable_file:
            executable_file.write(package.executable)
            executable_file.flush()
            os.fsync(executable_file.fileno())
        _validate_downloaded_executable(temporary_path, version)
        executable = directory / LLMFIT_EXECUTABLE_NAME
        os.replace(temporary_path, executable)
        temporary_path = None
        license_path = directory / LLMFIT_LICENSE_FILENAME
        _atomic_write_bytes(license_path, package.license_text)
        component = LlmfitComponent(
            executable=executable,
            version=version,
            executable_sha256=hashlib.sha256(package.executable).hexdigest(),
            license_path=license_path,
            license_sha256=hashlib.sha256(package.license_text).hexdigest(),
            checked_at=checked_at,
        )
        _write_component_metadata(directory, component)
        return component
    except (OSError, subprocess.SubprocessError) as exc:
        raise ModelRecommendationError("No se pudo instalar el recomendador local.") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _validate_downloaded_executable(executable: Path, version: str) -> None:
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run(
        [str(executable), "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
        creationflags=creation_flags,
        env=_private_llmfit_environment(),
    )
    if completed.stdout.strip() != f"llmfit {version}":
        raise ModelRecommendationError("El ejecutable de llmfit no confirmó su versión.")


def _run_llmfit(executable: Path) -> str:
    if executable.is_symlink() or not executable.is_file():
        raise ModelRecommendationError("El recomendador local no está disponible.")
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [
                str(executable),
                "recommend",
                "--json",
                "--limit",
                str(LLMFIT_QUERY_LIMIT),
                "--use-case",
                LLMFIT_USE_CASE,
                "--no-dashboard",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=LLMFIT_COMMAND_TIMEOUT_SECONDS,
            creationflags=creation_flags,
            env=_private_llmfit_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ModelRecommendationError("llmfit no pudo analizar este equipo.") from exc
    if len(completed.stdout.encode("utf-8")) > MAX_LLMFIT_OUTPUT_BYTES:
        raise ModelRecommendationError("llmfit devolvió demasiados datos.")
    return completed.stdout


def _private_llmfit_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("LOCALMAXXING_API_KEY", None)
    environment["LLMFIT_DASHBOARD_HOST"] = "127.0.0.1"
    return environment


_PACKAGING_MARKERS = frozenset(
    {
        "awq",
        "bnb",
        "exl2",
        "fp8",
        "fp16",
        "gguf",
        "gptq",
        "mlx",
        "onnx",
        "quantized",
        "qat",
        "unsloth",
    }
)
_MODEL_SIZE_TOKEN = re.compile(r"^\d+(?:\.\d+)?b$")
_QUANTIZATION_TOKEN = re.compile(r"^Q(\d+)(_[A-Z0-9_]+)?$", flags=re.IGNORECASE)


def _group_ranked_models(raw_models: list[object]) -> list[list[dict[str, object]]]:
    groups: dict[tuple[str, ...], list[dict[str, object]]] = {}
    for position, item in enumerate(raw_models):
        if not isinstance(item, dict):
            continue
        identity = _model_identity(item)
        key = identity if identity is not None else ("unresolved", str(position))
        groups.setdefault(key, []).append(item)
    return list(groups.values())


@dataclass(frozen=True, slots=True)
class _RecommendationGroupProfile:
    group: list[dict[str, object]]
    position: int
    score: float
    speed: float | None
    parameter_count_b: float | None
    safe_fit: bool
    model_ids: frozenset[str]


def _select_recommendation_groups(
    groups: list[list[dict[str, object]]],
) -> list[tuple[list[dict[str, object]], RecommendationRole]]:
    profiles = [
        profile
        for position, group in enumerate(groups)
        if (profile := _recommendation_group_profile(group, position)) is not None
    ]
    if not profiles:
        return []
    ranked = sorted(profiles, key=lambda item: (-item.score, item.position))
    balanced = ranked[0]
    minimum_fast_score = max(0.0, balanced.score - 12.0)
    fast_candidates = [
        profile
        for profile in ranked[1:]
        if profile.safe_fit
        and profile.speed is not None
        and profile.score >= minimum_fast_score
        and _profiles_are_distinct(profile, (balanced,))
        and (balanced.speed is None or profile.speed > balanced.speed * 1.05)
    ]
    faster = max(
        fast_candidates,
        key=lambda item: (item.speed or 0.0, item.score, -item.position),
        default=None,
    )

    minimum_capacity_score = max(0.0, balanced.score - 20.0)
    excluded = (balanced,) if faster is None else (balanced, faster)
    capacity_candidates = [
        profile
        for profile in ranked[1:]
        if profile.safe_fit
        and profile.parameter_count_b is not None
        and (
            balanced.parameter_count_b is None
            or profile.parameter_count_b > balanced.parameter_count_b
        )
        and (profile.speed is None or profile.speed >= 5.0)
        and profile.score >= minimum_capacity_score
        and _profiles_are_distinct(profile, excluded)
    ]
    capacity = max(
        capacity_candidates,
        key=lambda item: (
            item.parameter_count_b or 0.0,
            item.score,
            item.speed or 0.0,
            -item.position,
        ),
        default=None,
    )

    chosen: list[tuple[_RecommendationGroupProfile, RecommendationRole]] = [
        (balanced, RecommendationRole.BALANCED)
    ]
    if faster is not None:
        chosen.append((faster, RecommendationRole.FASTER))
    if capacity is not None:
        chosen.append((capacity, RecommendationRole.CAPACITY))
    for profile in ranked[1:]:
        if len(chosen) >= MAX_RECOMMENDATION_MODELS:
            break
        if any(profile is selected for selected, _role in chosen):
            continue
        if not _profiles_are_distinct(profile, tuple(selected for selected, _role in chosen)):
            continue
        chosen.append((profile, RecommendationRole.ALTERNATIVE))

    chosen.sort(key=lambda item: _recommendation_role_order(item[1]))
    return [(profile.group, role) for profile, role in chosen[:MAX_RECOMMENDATION_MODELS]]


def _recommendation_group_profile(
    group: list[dict[str, object]],
    position: int,
) -> _RecommendationGroupProfile | None:
    eligible = [item for item in group if _recommendation_item_is_eligible(item)]
    model_ids = frozenset(
        model_id
        for item in eligible
        if (model_id := _validated_ollama_name(item.get("ollama_name"))) is not None
    )
    if not eligible or not model_ids:
        return None
    speeds = [
        speed
        for item in eligible
        if (
            speed := _bounded_number(
                item.get("estimated_tps"),
                minimum=0.0,
                maximum=100_000.0,
            )
        )
        is not None
    ]
    parameter_counts = [
        count
        for item in eligible
        if (count := _parameter_count_billions(item.get("parameter_count"))) is not None
    ]
    return _RecommendationGroupProfile(
        group=group,
        position=position,
        score=max((_ranking_score(item) for item in eligible), default=0.0),
        speed=min(speeds, default=None),
        parameter_count_b=max(parameter_counts, default=None),
        safe_fit=any(_fit_level_has_headroom(item.get("fit_level")) for item in eligible),
        model_ids=model_ids,
    )


def _profiles_are_distinct(
    candidate: _RecommendationGroupProfile,
    selected: tuple[_RecommendationGroupProfile, ...],
) -> bool:
    return all(candidate.model_ids.isdisjoint(item.model_ids) for item in selected)


def _recommendation_role_order(role: RecommendationRole) -> int:
    return {
        RecommendationRole.BALANCED: 0,
        RecommendationRole.FASTER: 1,
        RecommendationRole.CAPACITY: 2,
        RecommendationRole.ALTERNATIVE: 3,
    }[role]


def _fit_level_has_headroom(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return value.casefold().replace("_", " ") in {"perfect", "good"}


def _parameter_count_billions(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return _bounded_number(value, minimum=0.001, maximum=10_000.0)
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([bmk]?)\s*", value, flags=re.IGNORECASE)
    if match is None:
        return None
    count = float(match.group(1))
    unit = match.group(2).casefold()
    if unit == "m":
        count /= 1_000
    elif unit == "k":
        count /= 1_000_000
    return count if 0.001 <= count <= 10_000.0 else None


def _model_identity(item: dict[str, object]) -> tuple[str, ...] | None:
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    leaf = name.rsplit("/", 1)[-1].casefold()
    parts = [part for part in re.split(r"[^a-z0-9.]+", leaf) if part]
    normalized: list[str] = []
    for part in parts:
        if part in _PACKAGING_MARKERS or re.fullmatch(r"\d+bit", part):
            break
        normalized.append("instruct" if part == "instruction" else part)
    return tuple(normalized) if len(normalized) >= 2 else None


def _parse_recommendation_group(
    group: list[dict[str, object]],
    *,
    manifest_loader: ManifestLoader | None,
    role: RecommendationRole,
) -> ModelRecommendation | None:
    eligible = [item for item in group if _recommendation_item_is_eligible(item)]
    if not eligible:
        return None
    anchors: list[tuple[dict[str, object], str]] = []
    for item in eligible:
        model_id = _validated_ollama_name(item.get("ollama_name"))
        if model_id is not None:
            anchors.append((item, model_id))
    if not anchors:
        return None

    ranking_item = max(eligible, key=_ranking_score)
    anchor_item, resolved_model_id = max(anchors, key=lambda pair: _ranking_score(pair[0]))
    resolved_size: int | None = None
    if manifest_loader is not None:
        for candidate in _specific_ollama_candidates(ranking_item, anchors):
            try:
                candidate_size = manifest_loader(candidate)
            except (ModelRecommendationError, OSError, ValueError, httpx.HTTPError):
                candidate_size = None
            if candidate_size is not None:
                resolved_model_id = candidate
                resolved_size = candidate_size
                break
    if is_reasoning_model_id(resolved_model_id):
        return None

    recommendation = _parse_recommendation(anchor_item)
    if recommendation is None:
        return None
    speeds = [
        value
        for item in eligible
        if (
            value := _bounded_number(
                item.get("estimated_tps"),
                minimum=0.0,
                maximum=100_000.0,
            )
        )
        is not None
    ]
    memory_values = [
        value
        for item in eligible
        if (
            value := _bounded_number(
                item.get("memory_required_gb"),
                minimum=0.01,
                maximum=10_000.0,
            )
        )
        is not None
    ]
    contexts = [
        value
        for item in eligible
        if (
            value := _bounded_integer(
                item.get("effective_context_length"),
                minimum=512,
                maximum=262_144,
            )
        )
        is not None
    ]
    scores = [
        value
        for item in eligible
        if (value := _bounded_number(item.get("score"), minimum=0.0, maximum=100.0)) is not None
    ]
    parameter_counts = [
        count
        for item in eligible
        if (count := _parameter_count_billions(item.get("parameter_count"))) is not None
    ]
    estimated_tps = min(speeds, default=recommendation.estimated_tokens_per_second)
    memory_required = max(memory_values, default=recommendation.memory_required_gb)
    context = max(contexts, default=recommendation.recommended_context)
    score = max(scores, default=recommendation.score)
    fit_level = ranking_item.get("fit_level")
    description = _recommendation_description(
        fit_level,
        estimated_tps=estimated_tps,
        memory_required_gb=memory_required,
    )
    display_name = _resolved_model_display_name(
        resolved_model_id,
        parameter_count=_optional_text(anchor_item.get("parameter_count")),
    )
    return replace(
        recommendation,
        model_id=resolved_model_id,
        display_name=display_name,
        description=description,
        download_size_bytes=resolved_size or recommendation.download_size_bytes,
        recommended_context=context,
        estimated_tokens_per_second=estimated_tps,
        memory_required_gb=memory_required,
        score=score,
        parameter_count_b=max(parameter_counts, default=recommendation.parameter_count_b),
        role=role,
    )


def _specific_ollama_candidates(
    item: dict[str, object],
    anchors: list[tuple[dict[str, object], str]],
) -> tuple[str, ...]:
    identity = _model_identity(item)
    quantization = _ollama_quantization_tag(item.get("best_quant"))
    if identity is None or quantization is None:
        return ()
    candidates: list[str] = []
    for _anchor_item, anchor_id in anchors:
        repository, separator, anchor_tag = anchor_id.rpartition(":")
        if not separator:
            repository = anchor_id
            anchor_tag = "latest"
        size_token = next(
            (
                part.casefold()
                for part in anchor_tag.split("-")
                if _MODEL_SIZE_TOKEN.fullmatch(part)
            ),
            None,
        )
        if size_token is None or size_token not in identity:
            continue
        suffix = identity[identity.index(size_token) :]
        candidate = f"{repository}:{'-'.join(suffix)}-{quantization}"
        try:
            candidate = validate_ollama_model_id(candidate)
        except LocalModelUnavailableError:
            continue
        if _canonical_model_id(candidate) != _canonical_model_id(anchor_id):
            candidates.append(candidate)
    return tuple(dict.fromkeys(candidates))


def _ollama_quantization_tag(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = _QUANTIZATION_TOKEN.fullmatch(value.strip())
    if match is None:
        return None
    suffix = (match.group(2) or "").upper()
    return f"q{match.group(1)}{suffix}"


def _ranking_score(item: dict[str, object]) -> float:
    return _bounded_number(item.get("score"), minimum=0.0, maximum=100.0) or 0.0


def _validated_ollama_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        model_id = validate_ollama_model_id(value)
    except LocalModelUnavailableError:
        return None
    return None if is_cloud_model_id(model_id) else model_id


def _recommendation_item_is_eligible(item: dict[str, object]) -> bool:
    fit_level = item.get("fit_level")
    if isinstance(fit_level, str) and fit_level.casefold().replace("_", " ") in {
        "too tight",
        "does not fit",
    }:
        return False
    category = item.get("category")
    return not (isinstance(category, str) and category.casefold() == "embedding")


def _resolved_model_display_name(model_id: str, *, parameter_count: str | None) -> str:
    repository, _separator, tag = model_id.rpartition(":")
    family = (repository or model_id).rsplit("/", 1)[-1]
    quantization_match = re.search(r"-(q\d+(?:_[a-z0-9]+)*)$", tag, flags=re.IGNORECASE)
    details: dict[str, object] = {"family": family}
    if parameter_count is not None:
        details["parameter_size"] = parameter_count
    if quantization_match is not None:
        details["quantization_level"] = quantization_match.group(1)
    return friendly_model_name(model_id, details).removesuffix(" — recomendado")


def _parse_recommendation(item: object) -> ModelRecommendation | None:
    if not isinstance(item, dict) or not _recommendation_item_is_eligible(item):
        return None
    model_id = _validated_ollama_name(item.get("ollama_name"))
    if model_id is None:
        return None
    fit_level = item.get("fit_level")

    parameter_count = _optional_text(item.get("parameter_count"))
    display_name = friendly_model_name(
        model_id,
        {"parameter_size": parameter_count} if parameter_count is not None else None,
    ).removesuffix(" — recomendado")
    disk_size_gb = _bounded_number(item.get("disk_size_gb"), minimum=0.01, maximum=1_000.0)
    effective_context = _bounded_integer(
        item.get("effective_context_length"),
        minimum=512,
        maximum=262_144,
    )
    estimated_tps = _bounded_number(item.get("estimated_tps"), minimum=0.0, maximum=100_000.0)
    memory_required = _bounded_number(
        item.get("memory_required_gb"),
        minimum=0.01,
        maximum=10_000.0,
    )
    score = _bounded_number(item.get("score"), minimum=0.0, maximum=100.0)
    return ModelRecommendation(
        model_id=model_id,
        display_name=display_name,
        description=_recommendation_description(
            fit_level,
            estimated_tps=estimated_tps,
            memory_required_gb=memory_required,
        ),
        download_size_bytes=round(disk_size_gb * 1_000_000_000)
        if disk_size_gb is not None
        else None,
        recommended_context=effective_context,
        estimated_tokens_per_second=estimated_tps,
        memory_required_gb=memory_required,
        score=score,
        parameter_count_b=_parameter_count_billions(parameter_count),
    )


def _recommendation_description(
    fit_level: object,
    *,
    estimated_tps: float | None,
    memory_required_gb: float | None,
) -> str:
    parts = [_friendly_fit(fit_level)]
    if estimated_tps is not None:
        parts.append(f"velocidad estimada: {_format_decimal(estimated_tps)} tokens/s")
    if memory_required_gb is not None:
        parts.append(f"usa unos {_format_decimal(memory_required_gb)} GB de memoria")
    return " · ".join(parts)


def _hardware_summary(system: object) -> str:
    if not isinstance(system, dict):
        return "Recomendaciones calculadas en este equipo"
    gpu_name: str | None = None
    gpu_vram: float | None = None
    gpus = system.get("gpus")
    if isinstance(gpus, list):
        candidates: list[tuple[int, float, str]] = []
        for gpu in gpus:
            if not isinstance(gpu, dict):
                continue
            name = _optional_text(gpu.get("name"))
            vram = _bounded_number(gpu.get("vram_gb"), minimum=0.0, maximum=10_000.0)
            backend = _optional_text(gpu.get("backend"))
            if name is not None and vram is not None:
                candidates.append(
                    (1 if backend and backend.casefold() == "cuda" else 0, vram, name)
                )
        if candidates:
            _priority, gpu_vram, gpu_name = max(candidates)
    if gpu_name is None:
        gpu_name = _optional_text(system.get("gpu_name"))
        gpu_vram = _bounded_number(system.get("gpu_vram_gb"), minimum=0.0, maximum=10_000.0)
    total_ram = _bounded_number(system.get("total_ram_gb"), minimum=0.0, maximum=10_000.0)
    parts: list[str] = []
    if gpu_name is not None:
        short_name = re.sub(r"^(NVIDIA\s+)?GeForce\s+", "", gpu_name, flags=re.IGNORECASE)
        parts.append(short_name)
    if gpu_vram is not None:
        parts.append(f"{_format_decimal(gpu_vram)} GB de VRAM")
    if total_ram is not None:
        parts.append(f"{math.ceil(total_ram):d} GB de RAM")
    return " · ".join(parts) if parts else "Recomendaciones calculadas en este equipo"


def _load_installed_component(directory: Path) -> LlmfitComponent | None:
    metadata_path = directory / LLMFIT_METADATA_FILENAME
    executable = directory / LLMFIT_EXECUTABLE_NAME
    license_path = directory / LLMFIT_LICENSE_FILENAME
    try:
        if metadata_path.is_symlink() or executable.is_symlink() or license_path.is_symlink():
            return None
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        version = _normalize_version(payload.get("version"))
        executable_sha256 = payload.get("executable_sha256")
        license_sha256 = payload.get("license_sha256")
        checked_at = _parse_datetime(payload.get("checked_at"))
        if (
            not isinstance(executable_sha256, str)
            or not _SHA256_PATTERN.fullmatch(executable_sha256)
            or not isinstance(license_sha256, str)
            or not _SHA256_PATTERN.fullmatch(license_sha256)
            or checked_at is None
            or not executable.is_file()
            or not 0 < executable.stat().st_size <= MAX_LLMFIT_EXECUTABLE_BYTES
            or not license_path.is_file()
            or not 0 < license_path.stat().st_size <= MAX_LLMFIT_LICENSE_BYTES
        ):
            return None
        actual_sha256 = hashlib.sha256(executable.read_bytes()).hexdigest()
        actual_license_sha256 = hashlib.sha256(license_path.read_bytes()).hexdigest()
        if actual_sha256 != executable_sha256 or actual_license_sha256 != license_sha256:
            return None
        return LlmfitComponent(
            executable,
            version,
            executable_sha256,
            license_path,
            license_sha256,
            checked_at,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ModelRecommendationError):
        return None


def _write_component_metadata(directory: Path, component: LlmfitComponent) -> None:
    payload = {
        "version": component.version,
        "executable_sha256": component.executable_sha256,
        "license_sha256": component.license_sha256,
        "checked_at": component.checked_at.isoformat(),
        "repository": LLMFIT_REPOSITORY,
    }
    _atomic_write_json(directory / LLMFIT_METADATA_FILENAME, payload)


def _load_recommendation_cache(path: Path) -> ModelRecommendations | None:
    try:
        if path.is_symlink() or path.stat().st_size > MAX_LLMFIT_OUTPUT_BYTES:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        if payload.get("schema_version") != RECOMMENDATION_CACHE_SCHEMA_VERSION:
            return None
        version = _normalize_version(payload.get("llmfit_version"))
        generated_at = _parse_datetime(payload.get("generated_at"))
        hardware_summary = _optional_text(payload.get("hardware_summary"))
        raw_models = payload.get("models")
        if generated_at is None or hardware_summary is None or not isinstance(raw_models, list):
            return None
        models: list[ModelRecommendation] = []
        for item in raw_models[:MAX_RECOMMENDATION_MODELS]:
            if not isinstance(item, dict):
                return None
            raw_model_id = item.get("model_id")
            if not isinstance(raw_model_id, str):
                return None
            model_id = validate_ollama_model_id(raw_model_id)
            if is_reasoning_model_id(model_id):
                return None
            display_name = _optional_text(item.get("display_name"))
            description = _optional_text(item.get("description"))
            if display_name is None or description is None:
                return None
            raw_role = item.get("role")
            if not isinstance(raw_role, str):
                return None
            try:
                role = RecommendationRole(raw_role)
            except (TypeError, ValueError):
                return None
            download_size = _bounded_integer(
                item.get("download_size_bytes"), minimum=1, maximum=10**13
            )
            context = _bounded_integer(
                item.get("recommended_context"), minimum=512, maximum=262_144
            )
            models.append(
                ModelRecommendation(
                    model_id=model_id,
                    display_name=display_name,
                    description=description,
                    download_size_bytes=download_size,
                    recommended_context=context,
                    estimated_tokens_per_second=_bounded_number(
                        item.get("estimated_tokens_per_second"), minimum=0.0, maximum=100_000.0
                    ),
                    memory_required_gb=_bounded_number(
                        item.get("memory_required_gb"), minimum=0.01, maximum=10_000.0
                    ),
                    score=_bounded_number(item.get("score"), minimum=0.0, maximum=100.0),
                    parameter_count_b=_bounded_number(
                        item.get("parameter_count_b"), minimum=0.001, maximum=10_000.0
                    ),
                    role=role,
                )
            )
        if not models:
            return None
        return ModelRecommendations(
            models=tuple(models),
            hardware_summary=hardware_summary,
            llmfit_version=version,
            generated_at=generated_at,
            origin=RecommendationOrigin.CACHE,
        )
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ModelRecommendationError,
        LocalModelUnavailableError,
    ):
        return None


def _write_recommendation_cache(path: Path, recommendations: ModelRecommendations) -> None:
    payload = {
        "schema_version": RECOMMENDATION_CACHE_SCHEMA_VERSION,
        "generated_at": recommendations.generated_at.isoformat(),
        "llmfit_version": recommendations.llmfit_version,
        "hardware_summary": recommendations.hardware_summary,
        "models": [
            {
                "model_id": item.model_id,
                "display_name": item.display_name,
                "description": item.description,
                "download_size_bytes": item.download_size_bytes,
                "recommended_context": item.recommended_context,
                "estimated_tokens_per_second": item.estimated_tokens_per_second,
                "memory_required_gb": item.memory_required_gb,
                "score": item.score,
                "parameter_count_b": item.parameter_count_b,
                "role": item.role.value,
            }
            for item in recommendations.models
        ],
    }
    _atomic_write_json(path, payload)


def _atomic_write_json(path: Path, payload: object) -> None:
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = mkstemp(
            dir=path.parent,
            prefix=f".{path.stem}-",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output_file:
            json.dump(payload, output_file, ensure_ascii=False, indent=2)
            output_file.write("\n")
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except (OSError, TypeError, UnicodeError) as exc:
        raise ModelRecommendationError("No se pudo guardar el estado del recomendador.") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = mkstemp(
            dir=path.parent,
            prefix=f".{path.stem}-",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as output_file:
            output_file.write(payload)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as exc:
        raise ModelRecommendationError("No se pudo guardar la licencia de llmfit.") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _release_target(platform_name: str, machine_name: str) -> str:
    if platform_name.casefold() != "win32":
        raise ModelRecommendationError(
            "Las recomendaciones administradas están disponibles en Windows."
        )
    machine = machine_name.casefold()
    if machine in {"amd64", "x86_64"}:
        return "x86_64-pc-windows-msvc"
    if machine in {"arm64", "aarch64"}:
        return "aarch64-pc-windows-msvc"
    raise ModelRecommendationError("Este tipo de procesador no es compatible con llmfit.")


def _read_limited_response(response: httpx.Response, maximum_bytes: int) -> bytes:
    data = bytearray()
    for chunk in response.iter_bytes():
        data.extend(chunk)
        if len(data) > maximum_bytes:
            raise ModelRecommendationError("La respuesta remota supera el límite seguro.")
    return bytes(data)


def _validate_release_url(url: str, version: str, filename: str) -> None:
    parsed = urlparse(url)
    expected_path = f"/{LLMFIT_REPOSITORY}/releases/download/v{version}/{filename}"
    if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.path != expected_path:
        raise ModelRecommendationError("La descarga de llmfit apunta a un origen inesperado.")


def _validate_download_host(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in _ASSET_HOSTS:
        raise ModelRecommendationError("La descarga de llmfit redirigió a un origen inesperado.")


def _normalize_version(value: object) -> str:
    if not isinstance(value, str):
        raise ModelRecommendationError("La versión de llmfit no es válida.")
    match = _VERSION_PATTERN.fullmatch(value.strip())
    if match is None:
        raise ModelRecommendationError("La versión de llmfit no es válida.")
    return match.group(1)


def _version_tuple(version: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in _normalize_version(version).split("."))  # type: ignore[return-value]


def _canonical_model_id(model_id: str) -> str:
    normalized = model_id.casefold()
    final_segment = normalized.rsplit("/", 1)[-1]
    return normalized if ":" in final_segment else f"{normalized}:latest"


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _as_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _is_recent(value: datetime, now: datetime, lifetime: timedelta) -> bool:
    age = now - value
    return timedelta(0) <= age <= lifetime


def _bounded_number(value: object, *, minimum: float, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if minimum <= number <= maximum else None


def _bounded_integer(value: object, *, minimum: int, maximum: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if minimum <= value <= maximum else None


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 500 or any(char in normalized for char in "\r\n\0"):
        return None
    return normalized


def _friendly_fit(value: object) -> str:
    if not isinstance(value, str):
        return "Compatible con este equipo"
    known = {
        "perfect": "Funcionará con holgura",
        "good": "Buen ajuste para este equipo",
        "marginal": "Funcionará con poca memoria libre",
    }
    return known.get(value.casefold().replace("_", " "), "Compatible con este equipo")


def _format_decimal(value: float) -> str:
    rounded = f"{value:.1f}".rstrip("0").rstrip(".")
    return rounded.replace(".", ",")
