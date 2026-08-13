"""Small authenticated-process protocol used by the local OCR boundary."""

from __future__ import annotations

from typing import Any

from parsezen.workers.private_channel import receive_bounded_json, send_bounded_json

PROTOCOL_VERSION = 2
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
MAX_OCR_PAGES = 10_000
MAX_TOTAL_RESULT_BYTES = 64 * 1024 * 1024


class OcrProtocolError(Exception):
    """An OCR worker message did not satisfy the private protocol."""


def send_message(connection: Any, message: dict[str, Any]) -> None:
    """Serialize one bounded JSON object without using pickle."""
    send_bounded_json(
        connection,
        message,
        max_bytes=MAX_MESSAGE_BYTES,
        error_type=OcrProtocolError,
        invalid_type_message="El mensaje OCR no declara un tipo válido.",
        serialization_message="El mensaje OCR no se puede serializar.",
        size_message="El mensaje OCR supera el límite permitido.",
    )


def receive_message(connection: Any) -> dict[str, Any]:
    """Read and validate one bounded JSON object without deserializing objects."""
    return receive_bounded_json(
        connection,
        max_bytes=MAX_MESSAGE_BYTES,
        error_type=OcrProtocolError,
        invalid_message="El canal OCR recibió un mensaje no válido.",
    )


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
