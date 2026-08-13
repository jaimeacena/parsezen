"""Parent-side lifecycle for the private local OCR process."""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from multiprocessing.connection import Connection, Listener
from pathlib import Path
from typing import Any

from parsezen.cancellation import CancellationToken, check_cancelled
from parsezen.errors import ConversionError, ProcessingCancelledError
from parsezen.ocr_protocol import (
    MAX_OCR_PAGES,
    MAX_TOTAL_RESULT_BYTES,
    PROTOCOL_VERSION,
    OcrProtocolError,
    receive_message,
    send_message,
)
from parsezen.workers.private_channel import (
    PrivateChannelAcceptError,
    PrivateChannelProcessExited,
    PrivateChannelTimeout,
    accept_private_connection,
    private_listener,
    receive_initial_message,
    start_private_process,
    stop_private_process,
)

_AUTH_ENVIRONMENT_VARIABLE = "PARSEZEN_OCR_AUTH"
_STARTUP_TIMEOUT_SECONDS = 30.0
_CANCEL_GRACE_SECONDS = 3.0
_PROCESS_EXIT_TIMEOUT_SECONDS = 3.0
_POLL_SECONDS = 0.05
_VALID_ERROR_CODES = {"conversion_failed", "unexpected"}
_SAFE_DIAGNOSTIC_VALUE = re.compile(r"[A-Za-z0-9_.<>-]{1,160}\Z")

LOGGER = logging.getLogger(__name__)


def run_ocr_worker(
    source_path: Path,
    page_numbers: set[int],
    cancellation: CancellationToken | None = None,
    *,
    force_full_page_numbers: set[int] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    on_page_result: Callable[[int, str], None] | None = None,
) -> dict[int, str]:
    """Run one OCR request outside the long-lived application process."""
    if not page_numbers:
        return {}
    check_cancelled(cancellation)
    selected_pages = _validated_pages(page_numbers)
    forced_pages = _validated_pages(force_full_page_numbers or set()) & selected_pages
    with _worker_source_path(source_path) as worker_source_path:
        return _run_ocr_worker(
            worker_source_path,
            selected_pages,
            forced_pages,
            cancellation,
            on_progress,
            on_page_result,
        )


def _run_ocr_worker(
    source_path: Path,
    selected_pages: set[int],
    forced_pages: set[int],
    cancellation: CancellationToken | None,
    on_progress: Callable[[int, int], None] | None,
    on_page_result: Callable[[int, str], None] | None,
) -> dict[int, str]:
    job_id = uuid.uuid4().hex
    auth_key = secrets.token_bytes(32)
    started_at = time.monotonic()
    LOGGER.info(
        "ocr_worker_started pages=%d forced_pages=%d",
        len(selected_pages),
        len(forced_pages),
    )

    process: subprocess.Popen[bytes] | None = None
    connection: Connection | None = None
    with _private_listener(auth_key) as (listener, address, family):
        try:
            process = _start_worker_process(address, family, auth_key)
            connection = _accept_worker(listener, process, cancellation)
            _expect_hello(connection, process, cancellation)
            LOGGER.info("ocr_worker_ready startup_ms=%d", _elapsed_milliseconds(started_at))
            send_message(
                connection,
                {
                    "type": "run",
                    "version": PROTOCOL_VERSION,
                    "job_id": job_id,
                    "source_path": str(source_path.resolve(strict=True)),
                    "page_numbers": sorted(selected_pages),
                    "force_full_page_numbers": sorted(forced_pages),
                },
            )
            results = _receive_worker_result(
                connection,
                process,
                job_id,
                selected_pages,
                cancellation,
                on_progress=on_progress,
                on_page_result=on_page_result,
                on_stage=lambda stage: LOGGER.info(
                    "ocr_worker_stage stage=%s elapsed_ms=%d",
                    stage,
                    _elapsed_milliseconds(started_at),
                ),
            )
            _wait_for_clean_exit(process)
            LOGGER.info(
                "ocr_worker_completed pages=%d duration_ms=%d",
                len(results),
                _elapsed_milliseconds(started_at),
            )
            return results
        except ProcessingCancelledError:
            LOGGER.info("ocr_worker_cancelled duration_ms=%d", _elapsed_milliseconds(started_at))
            if process is not None:
                _stop_worker(process)
            raise
        except (EOFError, BrokenPipeError, OSError, OcrProtocolError) as exc:
            LOGGER.warning(
                "ocr_worker_failed error_type=%s duration_ms=%d",
                type(exc).__name__,
                _elapsed_milliseconds(started_at),
            )
            if process is not None:
                _stop_worker(process)
            raise ConversionError(
                "El proceso OCR local terminó de forma inesperada. Vuelve a intentarlo."
            ) from exc
        finally:
            if connection is not None:
                connection.close()
            if process is not None and process.poll() is None:
                _stop_worker(process)


@contextmanager
def _worker_source_path(source_path: Path) -> Iterator[Path]:
    """Give the worker an ASCII path and clean it only after that process has exited."""

    try:
        resolved_source = source_path.resolve(strict=True)
    except OSError as exc:
        raise ConversionError("No se pudo preparar el PDF para el OCR local.") from exc
    try:
        str(resolved_source).encode("ascii")
    except UnicodeEncodeError:
        pass
    else:
        yield resolved_source
        return

    with tempfile.TemporaryDirectory(prefix="parsezen-ocr-parent-") as directory:
        temporary_path = Path(directory) / "parsezen.pdf"
        try:
            os.link(resolved_source, temporary_path)
        except OSError:
            try:
                shutil.copyfile(resolved_source, temporary_path)
            except OSError as exc:
                raise ConversionError("No se pudo preparar el PDF para el OCR local.") from exc
        yield temporary_path


def _validated_pages(page_numbers: set[int]) -> set[int]:
    if len(page_numbers) > MAX_OCR_PAGES or any(
        isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1
        for page_number in page_numbers
    ):
        raise ConversionError("La selección de páginas OCR no es válida.")
    return set(page_numbers)


@contextmanager
def _private_listener(auth_key: bytes) -> Iterator[tuple[Listener, str, str]]:
    with private_listener(auth_key, channel_name="ocr") as opened_listener:
        yield opened_listener


def _start_worker_process(
    address: str,
    family: str,
    auth_key: bytes,
) -> subprocess.Popen[bytes]:
    return start_private_process(
        address,
        family,
        auth_key,
        auth_environment_variable=_AUTH_ENVIRONMENT_VARIABLE,
        module="parsezen.ocr_worker",
        frozen_switch="--ocr-worker",
        executable=sys.executable,
        frozen=getattr(sys, "frozen", False),
    )


def _accept_worker(
    listener: Listener,
    process: subprocess.Popen[bytes],
    cancellation: CancellationToken | None,
) -> Connection:
    try:
        return accept_private_connection(
            listener,
            process,
            cancellation,
            timeout_seconds=_STARTUP_TIMEOUT_SECONDS,
            poll_seconds=_POLL_SECONDS,
            thread_name="ocr-worker-accept",
        )
    except PrivateChannelProcessExited as exc:
        raise ConversionError(
            "El proceso OCR local no pudo iniciarse. Comprueba la instalación."
        ) from exc
    except PrivateChannelTimeout as exc:
        raise ConversionError(
            "El proceso OCR local tardó demasiado en iniciarse. Vuelve a intentarlo."
        ) from exc
    except PrivateChannelAcceptError as exc:
        raise ConversionError("No se pudo abrir el canal privado del OCR local.") from exc


def _expect_hello(
    connection: Connection,
    process: subprocess.Popen[bytes],
    cancellation: CancellationToken | None,
) -> None:
    message = _receive_until_deadline(connection, process, cancellation)
    if message != {"type": "hello", "version": PROTOCOL_VERSION}:
        raise OcrProtocolError("El proceso OCR usa un protocolo incompatible.")


def _receive_until_deadline(
    connection: Connection,
    process: subprocess.Popen[bytes],
    cancellation: CancellationToken | None,
) -> dict[str, Any]:
    try:
        return receive_initial_message(
            connection,
            process,
            cancellation,
            receive_message=receive_message,
            timeout_seconds=_STARTUP_TIMEOUT_SECONDS,
            poll_seconds=_POLL_SECONDS,
        )
    except PrivateChannelTimeout as exc:
        raise ConversionError(
            "El proceso OCR local tardó demasiado en responder. Vuelve a intentarlo."
        ) from exc


def _receive_worker_result(
    connection: Connection,
    process: subprocess.Popen[bytes],
    job_id: str,
    selected_pages: set[int],
    cancellation: CancellationToken | None,
    on_progress: Callable[[int, int], None] | None = None,
    on_stage: Callable[[str], None] | None = None,
    on_page_result: Callable[[int, str], None] | None = None,
) -> dict[int, str]:
    results: dict[int, str] = {}
    total_result_bytes = 0
    stage = "starting"
    progress_current = 0
    cancellation_sent_at: float | None = None
    while True:
        if cancellation is not None and cancellation.is_cancelled:
            if cancellation_sent_at is None:
                send_message(connection, {"type": "cancel", "job_id": job_id})
                cancellation_sent_at = time.monotonic()
            elif (
                stage in {"starting", "running"}
                and time.monotonic() - cancellation_sent_at >= _CANCEL_GRACE_SECONDS
            ):
                _stop_worker(process)
                cancellation.check()

        if connection.poll(_POLL_SECONDS):
            message = receive_message(connection)
            if message.get("job_id") != job_id:
                raise OcrProtocolError("El proceso OCR respondió a otro trabajo.")
            message_type = message["type"]
            if message_type == "stage":
                received_stage = message.get("stage")
                expected_stage = "preparing" if stage == "starting" else "running"
                if received_stage != expected_stage:
                    raise OcrProtocolError("El proceso OCR declaró una etapa no válida.")
                stage = received_stage
                if on_stage is not None:
                    on_stage(stage)
            elif message_type == "progress":
                current = message.get("current")
                total = message.get("total")
                if (
                    isinstance(current, bool)
                    or not isinstance(current, int)
                    or isinstance(total, bool)
                    or not isinstance(total, int)
                    or total != len(selected_pages)
                    or current <= progress_current
                    or current > total
                    or stage != "running"
                ):
                    raise OcrProtocolError("El proceso OCR devolvió un progreso no válido.")
                progress_current = current
                if on_progress is not None:
                    on_progress(current, total)
            elif message_type == "page":
                page_number = message.get("page_number")
                markdown = message.get("markdown")
                if (
                    isinstance(page_number, bool)
                    or not isinstance(page_number, int)
                    or page_number not in selected_pages
                    or page_number in results
                    or not isinstance(markdown, str)
                    or stage != "running"
                ):
                    raise OcrProtocolError("El proceso OCR devolvió una página no válida.")
                total_result_bytes += len(markdown.encode("utf-8"))
                if total_result_bytes > MAX_TOTAL_RESULT_BYTES:
                    raise ConversionError("El resultado OCR supera el límite seguro de tamaño.")
                results[page_number] = markdown
                if on_page_result is not None:
                    on_page_result(page_number, markdown)
            elif message_type == "complete":
                if stage != "running":
                    raise OcrProtocolError("El proceso OCR terminó antes de ejecutar el análisis.")
                check_cancelled(cancellation)
                return results
            elif message_type == "cancelled":
                raise ProcessingCancelledError("El procesamiento se canceló de forma segura.")
            elif message_type == "error":
                error_code = message.get("code")
                error_message = message.get("message")
                if (
                    error_code not in _VALID_ERROR_CODES
                    or not isinstance(error_message, str)
                    or not error_message
                    or len(error_message) > 500
                ):
                    raise OcrProtocolError("El proceso OCR devolvió un error no válido.")
                diagnostic = _validated_worker_diagnostic(message.get("diagnostic"))
                LOGGER.warning(
                    "ocr_worker_engine_failure error_type=%s module=%s function=%s line=%d",
                    diagnostic[0],
                    diagnostic[1],
                    diagnostic[2],
                    diagnostic[3],
                )
                raise ConversionError(error_message)
            else:
                raise OcrProtocolError("El proceso OCR devolvió un mensaje desconocido.")
        elif process.poll() is not None:
            raise EOFError


def _validated_worker_diagnostic(value: object) -> tuple[str, str, str, int]:
    if not isinstance(value, dict) or set(value) != {
        "error_type",
        "module",
        "function",
        "line",
    }:
        raise OcrProtocolError("El proceso OCR devolvió un diagnóstico no válido.")
    error_type = value.get("error_type")
    module_name = value.get("module")
    function_name = value.get("function")
    line_number = value.get("line")
    if (
        not isinstance(error_type, str)
        or _SAFE_DIAGNOSTIC_VALUE.fullmatch(error_type) is None
        or not isinstance(module_name, str)
        or _SAFE_DIAGNOSTIC_VALUE.fullmatch(module_name) is None
        or not isinstance(function_name, str)
        or _SAFE_DIAGNOSTIC_VALUE.fullmatch(function_name) is None
        or isinstance(line_number, bool)
        or not isinstance(line_number, int)
        or not 0 <= line_number <= 10_000_000
    ):
        raise OcrProtocolError("El proceso OCR devolvió un diagnóstico no válido.")
    return error_type, module_name, function_name, line_number


def _wait_for_clean_exit(process: subprocess.Popen[bytes]) -> None:
    try:
        exit_code = process.wait(timeout=_PROCESS_EXIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        _stop_worker(process)
        raise ConversionError("El proceso OCR local no pudo cerrarse correctamente.") from exc
    if exit_code != 0:
        raise ConversionError("El proceso OCR local terminó de forma inesperada.")


def _stop_worker(process: subprocess.Popen[bytes]) -> None:
    stop_private_process(process, exit_timeout_seconds=_PROCESS_EXIT_TIMEOUT_SECONDS)


def _elapsed_milliseconds(started_at: float) -> int:
    return max(0, round((time.monotonic() - started_at) * 1000))
