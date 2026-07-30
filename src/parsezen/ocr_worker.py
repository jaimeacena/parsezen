"""Private OCR subprocess entry point with no Qt dependency."""

from __future__ import annotations

import argparse
import base64
import inspect
import os
import re
import threading
from collections.abc import Callable, Sequence
from multiprocessing.connection import Client, Connection
from pathlib import Path
from typing import Any

from parsezen.cancellation import CancellationToken
from parsezen.errors import ConversionError, ProcessingCancelledError
from parsezen.ocr_protocol import (
    PROTOCOL_VERSION,
    OcrProtocolError,
    normalized_page_numbers,
    receive_message,
    send_message,
)

_AUTH_ENVIRONMENT_VARIABLE = "PARSEZEN_OCR_AUTH"
_GENERIC_WORKER_ERROR = "El OCR local no pudo procesar las páginas seleccionadas."
_SAFE_DIAGNOSTIC_VALUE = re.compile(r"[A-Za-z0-9_.<>-]{1,160}\Z")


def main(argv: Sequence[str] | None = None) -> int:
    """Connect to the authenticated parent and serve exactly one OCR request."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--address", required=True)
    parser.add_argument("--family", choices=("AF_PIPE", "AF_UNIX"), required=True)
    try:
        arguments = parser.parse_args(argv)
        encoded_auth_key = os.environ.pop(_AUTH_ENVIRONMENT_VARIABLE)
        auth_key = base64.urlsafe_b64decode(encoded_auth_key.encode("ascii"))
        if len(auth_key) != 32:
            return 2
        connection = Client(
            address=arguments.address,
            family=arguments.family,
            authkey=auth_key,
        )
    except (KeyError, OSError, ValueError):
        return 2

    with connection:
        return serve_connection(connection)


def serve_connection(
    connection: Connection,
    *,
    converter: Callable[..., dict[int, str]] | None = None,
    exit_on_parent_loss: bool = True,
) -> int:
    """Serve one request; dependency injection keeps the process contract testable."""
    send_message(connection, {"type": "hello", "version": PROTOCOL_VERSION})
    try:
        request = receive_message(connection)
        job_id, source_path, pages, forced_pages = _validated_request(request)
    except (EOFError, OcrProtocolError):
        return 2

    cancellation = CancellationToken()
    completed = threading.Event()
    controller = threading.Thread(
        target=_listen_for_control_messages,
        args=(connection, job_id, cancellation, completed, exit_on_parent_loss),
        name="ocr-worker-control",
        daemon=True,
    )
    controller.start()

    def on_stage(stage: str) -> None:
        send_message(connection, {"type": "stage", "job_id": job_id, "stage": stage})

    def on_progress(current: int, total: int) -> None:
        send_message(
            connection,
            {
                "type": "progress",
                "job_id": job_id,
                "current": current,
                "total": total,
            },
        )

    sent_pages: set[int] = set()

    def on_page_result(page_number: int, markdown: str) -> None:
        if (
            isinstance(page_number, bool)
            or page_number not in pages
            or page_number in sent_pages
            or not isinstance(markdown, str)
        ):
            raise OcrProtocolError("El conversor OCR devolvió una página no válida.")
        sent_pages.add(page_number)
        send_message(
            connection,
            {
                "type": "page",
                "job_id": job_id,
                "page_number": page_number,
                "markdown": markdown,
            },
        )

    if converter is None:
        from parsezen.ocr_conversion import _convert_pdf_pages_in_process

        converter = _convert_pdf_pages_in_process
    try:
        signature = inspect.signature(converter)
        supports_page_results = "on_page_result" in signature.parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        if supports_page_results:
            results = converter(
                source_path,
                pages,
                cancellation=cancellation,
                force_full_page_numbers=forced_pages,
                on_stage=on_stage,
                on_progress=on_progress,
                on_page_result=on_page_result,
            )
        else:
            results = converter(
                source_path,
                pages,
                cancellation=cancellation,
                force_full_page_numbers=forced_pages,
                on_stage=on_stage,
                on_progress=on_progress,
            )
        cancellation.check()
        for page_number in sorted(results):
            if page_number not in sent_pages:
                on_page_result(page_number, results[page_number])
        completed.set()
        send_message(connection, {"type": "complete", "job_id": job_id})
        return 0
    except ProcessingCancelledError:
        completed.set()
        send_message(connection, {"type": "cancelled", "job_id": job_id})
        return 0
    except ConversionError as exc:
        completed.set()
        send_message(
            connection,
            {
                "type": "error",
                "job_id": job_id,
                "code": "conversion_failed",
                "message": str(exc),
                "diagnostic": _safe_failure_diagnostic(exc),
            },
        )
        return 0
    except Exception as exc:
        completed.set()
        send_message(
            connection,
            {
                "type": "error",
                "job_id": job_id,
                "code": "unexpected",
                "message": _GENERIC_WORKER_ERROR,
                "diagnostic": _safe_failure_diagnostic(exc),
            },
        )
        return 1


def _safe_failure_diagnostic(exc: BaseException) -> dict[str, str | int]:
    """Return only technical location metadata; never exception text or local variables."""

    failure = exc
    visited: set[int] = set()
    while id(failure) not in visited:
        visited.add(id(failure))
        nested = failure.__cause__ or failure.__context__
        if nested is None:
            break
        failure = nested

    module_name = type(failure).__module__
    function_name = "unknown"
    line_number = 0
    traceback_cursor = failure.__traceback__
    while traceback_cursor is not None:
        frame = traceback_cursor.tb_frame
        candidate_module = frame.f_globals.get("__name__")
        if isinstance(candidate_module, str):
            module_name = candidate_module
        function_name = frame.f_code.co_name
        line_number = traceback_cursor.tb_lineno
        traceback_cursor = traceback_cursor.tb_next

    return {
        "error_type": _safe_diagnostic_value(type(failure).__name__),
        "module": _safe_diagnostic_value(module_name),
        "function": _safe_diagnostic_value(function_name),
        "line": max(0, min(line_number, 10_000_000)),
    }


def _safe_diagnostic_value(value: str) -> str:
    return value if _SAFE_DIAGNOSTIC_VALUE.fullmatch(value) else "unknown"


def _validated_request(request: dict[str, Any]) -> tuple[str, Path, set[int], set[int]]:
    if request.get("type") != "run" or request.get("version") != PROTOCOL_VERSION:
        raise OcrProtocolError("La petición OCR no es compatible.")
    job_id = request.get("job_id")
    raw_source_path = request.get("source_path")
    if (
        not isinstance(job_id, str)
        or len(job_id) != 32
        or not isinstance(raw_source_path, str)
        or not raw_source_path
        or "\0" in raw_source_path
    ):
        raise OcrProtocolError("La petición OCR no es válida.")
    source_path = Path(raw_source_path)
    if source_path.suffix.casefold() != ".pdf" or not source_path.is_file():
        raise OcrProtocolError("La petición OCR no apunta a un PDF local válido.")
    pages = normalized_page_numbers(request.get("page_numbers"), field_name="page_numbers")
    forced_pages = normalized_page_numbers(
        request.get("force_full_page_numbers"),
        field_name="force_full_page_numbers",
    )
    if not pages or not forced_pages <= pages:
        raise OcrProtocolError("La petición OCR no contiene una selección válida.")
    return job_id, source_path, pages, forced_pages


def _listen_for_control_messages(
    connection: Connection,
    job_id: str,
    cancellation: CancellationToken,
    completed: threading.Event,
    exit_on_parent_loss: bool,
) -> None:
    while not completed.is_set():
        try:
            message = receive_message(connection)
        except (EOFError, OSError, OcrProtocolError):
            if not completed.is_set() and exit_on_parent_loss:
                os._exit(3)
            return
        if message.get("type") == "cancel" and message.get("job_id") == job_id:
            cancellation.cancel()


if __name__ == "__main__":
    raise SystemExit(main())
