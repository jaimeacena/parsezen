"""Bounded access to Ollama's fixed, local native API."""

from __future__ import annotations

import json
from time import monotonic

import httpx

from parsezen.cancellation import CancellationToken, check_cancelled
from parsezen.errors import ImprovementError
from parsezen.improvement_contracts import MAX_LOCAL_AI_OUTPUT_CHARACTERS
from parsezen.local_models import OLLAMA_BASE_URL


def request_local_ai(
    client: httpx.Client,
    model: str,
    context_window: int,
    instructions: str,
    document_fragment: str,
    cancellation: CancellationToken | None,
    *,
    prediction_characters: int | None = None,
) -> str:
    """Return one deterministic local transformation with strict output bounds."""

    request_payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": document_fragment},
        ],
        "stream": True,
        "think": False,
        "options": {
            "temperature": 0,
            "num_ctx": context_window,
            "num_predict": prediction_token_limit(
                prediction_characters
                if prediction_characters is not None
                else len(document_fragment),
                context_window,
            ),
            "seed": 0,
        },
    }
    check_cancelled(cancellation)
    read_timeout = client.timeout.read
    deadline = (
        monotonic() + float(read_timeout)
        if isinstance(read_timeout, int | float) and read_timeout > 0
        else None
    )
    with client.stream(
        "POST",
        f"{OLLAMA_BASE_URL}/api/chat",
        json=request_payload,
    ) as response:
        if response.is_redirect:
            raise ImprovementError("Ollama intentó redirigir la solicitud y fue bloqueado.")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ImprovementError(
                f"El modelo local respondió con el estado HTTP {response.status_code}."
            ) from exc

        content_parts: list[str] = []
        content_length = 0
        saw_message = False
        for line in response.iter_lines():
            check_cancelled(cancellation)
            if deadline is not None and monotonic() > deadline:
                raise ImprovementError("El modelo local superó el tiempo máximo de respuesta.")
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                message = payload["message"]
                content = message["content"]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise ImprovementError("Ollama devolvió una respuesta incompatible.") from exc
            if not isinstance(content, str):
                raise ImprovementError("Ollama devolvió una respuesta incompatible.")
            saw_message = True
            content_length += len(content)
            if content_length > MAX_LOCAL_AI_OUTPUT_CHARACTERS:
                raise ImprovementError("La respuesta del modelo supera el tamaño permitido.")
            content_parts.append(content)
        check_cancelled(cancellation)
    if not saw_message:
        raise ImprovementError("Ollama devolvió una respuesta incompatible.")
    return "".join(content_parts)


def prediction_token_limit(source_characters: int, context_window: int) -> int:
    """Bound runaway generations while leaving ample room for Latin-script output."""

    proportional_limit = (source_characters + 2) // 3 + 128
    context_limit = min(max(context_window // 2, 128), 2_048)
    return max(128, min(proportional_limit, context_limit))
