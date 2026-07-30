"""Small authenticated-process protocol used by the local OCR boundary."""

from __future__ import annotations

import json
from typing import Any

PROTOCOL_VERSION = 2
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
MAX_OCR_PAGES = 10_000
MAX_TOTAL_RESULT_BYTES = 64 * 1024 * 1024


class OcrProtocolError(Exception):
    """An OCR worker message did not satisfy the private protocol."""


def send_message(connection: Any, message: dict[str, Any]) -> None:
    """Serialize one bounded JSON object without using pickle."""
    if not isinstance(message.get("type"), str):
        raise OcrProtocolError("El mensaje OCR no declara un tipo válido.")
    try:
        payload = json.dumps(
            message,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise OcrProtocolError("El mensaje OCR no se puede serializar.") from exc
    if len(payload) > MAX_MESSAGE_BYTES:
        raise OcrProtocolError("El mensaje OCR supera el límite permitido.")
    connection.send_bytes(payload)


def receive_message(connection: Any) -> dict[str, Any]:
    """Read and validate one bounded JSON object without deserializing objects."""
    try:
        payload = connection.recv_bytes(MAX_MESSAGE_BYTES)
        message = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OcrProtocolError("El canal OCR recibió un mensaje no válido.") from exc
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise OcrProtocolError("El canal OCR recibió un mensaje no válido.")
    return message


def normalized_page_numbers(value: Any, *, field_name: str) -> set[int]:
    """Validate a bounded JSON page-number array."""
    if not isinstance(value, list) or len(value) > MAX_OCR_PAGES:
        raise OcrProtocolError(f"El campo {field_name} no contiene páginas válidas.")
    pages: set[int] = set()
    for page_number in value:
        if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
            raise OcrProtocolError(f"El campo {field_name} no contiene páginas válidas.")
        pages.add(page_number)
    return pages
