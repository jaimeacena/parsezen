"""Install only Parsezen's frozen Ollama components through loopback.

The public operation accepts a capability, never a model name or endpoint.
Every source identifier, alias, licence and expected digest comes from the
immutable product catalog.  Installation succeeds only after the regular
metadata verifier accepts the final alias.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from importlib.resources import files
from threading import Event
from typing import Final

import httpx

from parsezen.component_catalog import (
    PRODUCT_COMPONENT_CATALOG,
    REVIEW_MODEL_NAME,
    REVIEW_OLLAMA_SOURCE_MODEL,
    TRANSLATION_LICENSE_SHA256,
    TRANSLATION_MODEL_NAME,
    TRANSLATION_OLLAMA_SOURCE_MODEL,
)
from parsezen.component_readiness import ReadinessStatus, evaluate_component_readiness
from parsezen.errors import LocalModelUnavailableError
from parsezen.local_ai_policy import (
    ComponentCapability,
    ComponentVerification,
    verify_component_manifest,
)
from parsezen.local_models import (
    MAX_PULL_RESPONSE_LINE_BYTES,
    OLLAMA_BASE_URL,
    LocalAISetupCancelled,
    LocalHardware,
    detect_local_hardware,
    is_ollama_local_only_configured,
)

ProgressCallback = Callable[[int | None, str], None]
_SOURCE_MODELS: Final = {
    ComponentCapability.TRANSLATION: TRANSLATION_OLLAMA_SOURCE_MODEL,
    ComponentCapability.REVIEW: REVIEW_OLLAMA_SOURCE_MODEL,
}
_MAX_FINALIZE_RESPONSE_BYTES: Final = 64 * 1024


def install_product_component(
    capability: ComponentCapability,
    *,
    on_progress: ProgressCallback | None = None,
    cancellation: Event | None = None,
    transport: httpx.BaseTransport | None = None,
    hardware: LocalHardware | None = None,
    local_only_configured: bool | None = None,
) -> ComponentVerification:
    """Install one fixed product component and verify its final identity."""

    if not isinstance(capability, ComponentCapability) or capability not in _SOURCE_MODELS:
        raise LocalModelUnavailableError("Ese componente local no forma parte de Parsezen.")
    entry = PRODUCT_COMPONENT_CATALOG[capability]
    local_only = (
        is_ollama_local_only_configured()
        if local_only_configured is None
        else local_only_configured
    )
    snapshot = detect_local_hardware() if hardware is None else hardware
    current = verify_component_manifest(entry.manifest, transport=transport)
    if current.valid:
        _report(on_progress, 100, "El componente ya está preparado.")
        return current
    readiness = evaluate_component_readiness(
        entry,
        snapshot,
        current,
        local_only_configured=local_only,
    )
    if readiness.status is not ReadinessStatus.DOWNLOADABLE:
        raise LocalModelUnavailableError(
            "El componente no puede instalarse hasta resolver sus requisitos locales."
        )

    cancel_event = cancellation if cancellation is not None else Event()
    _pull_fixed_source(
        _SOURCE_MODELS[capability],
        on_progress=on_progress,
        cancellation=cancel_event,
        transport=transport,
    )
    _raise_if_cancelled(cancel_event)
    if capability is ComponentCapability.TRANSLATION:
        _create_translation_alias(transport=transport)
    else:
        _copy_review_alias(transport=transport)
    _raise_if_cancelled(cancel_event)

    verified = verify_component_manifest(entry.manifest, transport=transport)
    if not verified.valid:
        raise LocalModelUnavailableError(
            "La descarga terminó, pero la identidad del componente no coincide con Parsezen."
        )
    _report(on_progress, 100, "Componente preparado y verificado.")
    return verified


def _pull_fixed_source(
    source_model: str,
    *,
    on_progress: ProgressCallback | None,
    cancellation: Event,
    transport: httpx.BaseTransport | None,
) -> None:
    if source_model not in _SOURCE_MODELS.values():
        raise LocalModelUnavailableError("La fuente del componente no está aprobada.")
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
                json={"model": source_model, "stream": True},
            ) as response:
                _raise_for_safe_status(response, "descargar")
                for line in response.iter_lines():
                    _raise_if_cancelled(cancellation)
                    if not line:
                        continue
                    if len(line.encode("utf-8")) > MAX_PULL_RESPONSE_LINE_BYTES:
                        raise LocalModelUnavailableError(
                            "Ollama devolvió un progreso de descarga incompatible."
                        )
                    try:
                        update = json.loads(line)
                    except (ValueError, UnicodeError) as exc:
                        raise LocalModelUnavailableError(
                            "Ollama devolvió un progreso de descarga incompatible."
                        ) from exc
                    if not isinstance(update, dict):
                        raise LocalModelUnavailableError(
                            "Ollama devolvió un progreso de descarga incompatible."
                        )
                    if isinstance(update.get("error"), str):
                        raise LocalModelUnavailableError(
                            "Ollama no pudo descargar el componente aprobado."
                        )
                    status = update.get("status")
                    if status == "success":
                        success = True
                    total = update.get("total")
                    completed = update.get("completed")
                    percent = (
                        min(99, max(0, round(completed * 100 / total)))
                        if isinstance(total, int)
                        and not isinstance(total, bool)
                        and total > 0
                        and isinstance(completed, int)
                        and not isinstance(completed, bool)
                        else None
                    )
                    _report(on_progress, percent, "Descargando componente local…")
    except httpx.RequestError as exc:
        raise LocalModelUnavailableError(
            "Se interrumpió la conexión local con Ollama durante la descarga."
        ) from exc
    if not success:
        raise LocalModelUnavailableError("Ollama no confirmó la descarga del componente.")


def _create_translation_alias(*, transport: httpx.BaseTransport | None) -> None:
    license_text = _translation_license_text()
    _post_finalize(
        "/api/create",
        {
            "model": TRANSLATION_MODEL_NAME,
            "from": TRANSLATION_OLLAMA_SOURCE_MODEL,
            "license": license_text,
            "stream": False,
        },
        transport=transport,
    )


def _copy_review_alias(*, transport: httpx.BaseTransport | None) -> None:
    _post_finalize(
        "/api/copy",
        {"source": REVIEW_OLLAMA_SOURCE_MODEL, "destination": REVIEW_MODEL_NAME},
        transport=transport,
    )


def _post_finalize(
    path: str,
    payload: dict[str, object],
    *,
    transport: httpx.BaseTransport | None,
) -> None:
    try:
        with httpx.Client(
            timeout=60.0,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            response = client.post(f"{OLLAMA_BASE_URL}{path}", json=payload)
    except httpx.RequestError as exc:
        raise LocalModelUnavailableError(
            "Se interrumpió la preparación local del componente."
        ) from exc
    _raise_for_safe_status(response, "preparar")
    if len(response.content) > _MAX_FINALIZE_RESPONSE_BYTES:
        raise LocalModelUnavailableError("Ollama devolvió una confirmación incompatible.")
    if path == "/api/create":
        try:
            result = response.json()
        except ValueError as exc:
            raise LocalModelUnavailableError(
                "Ollama devolvió una confirmación incompatible."
            ) from exc
        if not isinstance(result, dict) or result.get("status") != "success":
            raise LocalModelUnavailableError("Ollama no confirmó la preparación del componente.")


def _translation_license_text() -> str:
    try:
        # The upstream file intentionally has no final newline.  Package data
        # is text-normalised by build tools, so remove at most that separator.
        text = files("parsezen.licenses").joinpath("hy_mt2_7b_gguf.txt").read_text("utf-8")
    except (OSError, UnicodeError) as exc:
        raise LocalModelUnavailableError("No se encontró la licencia del componente.") from exc
    text = text.removesuffix("\n")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != TRANSLATION_LICENSE_SHA256:
        raise LocalModelUnavailableError("La licencia incluida no coincide con el componente.")
    return text


def _raise_for_safe_status(response: httpx.Response, action: str) -> None:
    if response.is_redirect:
        raise LocalModelUnavailableError("Ollama intentó redirigir la operación y fue bloqueado.")
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise LocalModelUnavailableError(
            f"Ollama no pudo {action} el componente (HTTP {response.status_code})."
        ) from exc


def _raise_if_cancelled(cancellation: Event) -> None:
    if cancellation.is_set():
        raise LocalAISetupCancelled("Descarga del componente cancelada.")


def _report(callback: ProgressCallback | None, percent: int | None, message: str) -> None:
    if callback is not None:
        callback(percent, message)


__all__ = ["install_product_component"]
