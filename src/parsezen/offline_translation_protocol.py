"""Bounded JSON protocol for the private offline-translation process."""

from __future__ import annotations

from typing import Any

from parsezen.workers.private_channel import receive_bounded_json, send_bounded_json

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 4 * 1024 * 1024


class OfflineTranslationProtocolError(Exception):
    """A translation worker message did not satisfy the private protocol."""


def send_message(connection: Any, message: dict[str, Any]) -> None:
    """Serialize a bounded JSON object without pickle."""

    send_bounded_json(
        connection,
        message,
        max_bytes=MAX_MESSAGE_BYTES,
        error_type=OfflineTranslationProtocolError,
        invalid_type_message="El mensaje de traducción no declara un tipo.",
        serialization_message="El mensaje de traducción no se puede serializar.",
        size_message="El mensaje de traducción supera el límite seguro.",
    )


def receive_message(connection: Any) -> dict[str, Any]:
    """Receive a bounded JSON object without constructing arbitrary objects."""

    return receive_bounded_json(
        connection,
        max_bytes=MAX_MESSAGE_BYTES,
        error_type=OfflineTranslationProtocolError,
        invalid_message="El canal de traducción recibió un mensaje no válido.",
    )
