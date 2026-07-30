"""Bounded JSON protocol for the private offline-translation process."""

from __future__ import annotations

import json
from typing import Any

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 4 * 1024 * 1024


class OfflineTranslationProtocolError(Exception):
    """A translation worker message did not satisfy the private protocol."""


def send_message(connection: Any, message: dict[str, Any]) -> None:
    """Serialize a bounded JSON object without pickle."""

    if not isinstance(message.get("type"), str):
        raise OfflineTranslationProtocolError("El mensaje de traducción no declara un tipo.")
    try:
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise OfflineTranslationProtocolError(
            "El mensaje de traducción no se puede serializar."
        ) from exc
    if len(payload) > MAX_MESSAGE_BYTES:
        raise OfflineTranslationProtocolError("El mensaje de traducción supera el límite seguro.")
    connection.send_bytes(payload)


def receive_message(connection: Any) -> dict[str, Any]:
    """Receive a bounded JSON object without constructing arbitrary objects."""

    try:
        payload = connection.recv_bytes(MAX_MESSAGE_BYTES)
        message = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OfflineTranslationProtocolError(
            "El canal de traducción recibió un mensaje no válido."
        ) from exc
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise OfflineTranslationProtocolError(
            "El canal de traducción recibió un mensaje no válido."
        )
    return message
