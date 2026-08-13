"""Parent-side lifecycle for one private offline-translation process."""

from __future__ import annotations

import hashlib
import logging
import secrets
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from multiprocessing.connection import Connection, Listener
from typing import Any

from parsezen.cancellation import CancellationToken, check_cancelled
from parsezen.errors import ProcessingCancelledError, TranslationError
from parsezen.offline_translation_protocol import (
    PROTOCOL_VERSION,
    OfflineTranslationProtocolError,
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

_AUTH_ENVIRONMENT_VARIABLE = "PARSEZEN_TRANSLATION_AUTH"
_STARTUP_TIMEOUT_SECONDS = 30.0
_CANCEL_GRACE_SECONDS = 5.0
_PROCESS_EXIT_TIMEOUT_SECONDS = 3.0
_POLL_SECONDS = 0.05
_VALID_ERROR_CODES = {"translation_failed", "unexpected"}

LOGGER = logging.getLogger(__name__)

_ACTIVE_SESSION: ContextVar[OfflineTranslationSession | None] = ContextVar(
    "parsezen_offline_translation_session",
    default=None,
)


class OfflineTranslationSession:
    """Lazily own one worker for every offline translation in a document job."""

    def __init__(self) -> None:
        self._listener_context: Any | None = None
        self._connection: Connection | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._closed = False

    def translate(
        self,
        markdown: str,
        target_language: str,
        *,
        source_language_code: str | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        on_engine_ready: Callable[[], None] | None = None,
        cancellation: CancellationToken | None = None,
    ) -> str:
        """Translate one Markdown value while keeping the model in the worker."""

        if self._closed:
            raise TranslationError("La sesión de traducción local ya se cerró.")
        check_cancelled(cancellation)
        started_at = time.monotonic()
        LOGGER.info("translation_worker_started characters=%d", len(markdown))
        try:
            connection, process = self._ensure_started(cancellation)
            job_id = uuid.uuid4().hex
            send_message(
                connection,
                {
                    "type": "run",
                    "version": PROTOCOL_VERSION,
                    "job_id": job_id,
                    "markdown": markdown,
                    "target_language": target_language,
                    "source_language_code": source_language_code,
                },
            )

            def announce_engine_ready() -> None:
                LOGGER.info(
                    "translation_worker_ready elapsed_ms=%d",
                    _elapsed_milliseconds(started_at),
                )
                if on_engine_ready is not None:
                    on_engine_ready()

            result = self._receive_result(
                connection,
                process,
                job_id,
                on_progress=on_progress,
                on_engine_ready=announce_engine_ready,
                cancellation=cancellation,
            )
        except ProcessingCancelledError:
            LOGGER.info(
                "translation_worker_cancelled duration_ms=%d",
                _elapsed_milliseconds(started_at),
            )
            self._release_worker()
            raise
        except TranslationError as exc:
            LOGGER.warning(
                "translation_worker_rejected error_fingerprint=%s duration_ms=%d",
                hashlib.sha256(str(exc).encode("utf-8")).hexdigest()[:12],
                _elapsed_milliseconds(started_at),
            )
            raise
        except (BrokenPipeError, EOFError, OSError, OfflineTranslationProtocolError) as exc:
            LOGGER.warning(
                "translation_worker_failed error_type=%s duration_ms=%d",
                type(exc).__name__,
                _elapsed_milliseconds(started_at),
            )
            self._release_worker()
            raise TranslationError(
                "El proceso de traducción local terminó de forma inesperada. Vuelve a intentarlo."
            ) from exc
        except Exception:
            self._release_worker()
            raise
        LOGGER.info(
            "translation_worker_completed characters=%d duration_ms=%d",
            len(result),
            _elapsed_milliseconds(started_at),
        )
        return result

    def close(self) -> None:
        """Stop the worker and release its model memory."""

        if self._closed:
            return
        self._closed = True
        connection = self._connection
        process = self._process
        if connection is not None and process is not None and process.poll() is None:
            try:
                send_message(connection, {"type": "close", "version": PROTOCOL_VERSION})
                _wait_for_clean_exit(process)
            except (BrokenPipeError, EOFError, OSError, OfflineTranslationProtocolError):
                _stop_worker(process)
        self._release_worker()

    def _release_worker(self) -> None:
        connection = self._connection
        process = self._process
        listener_context = self._listener_context
        self._connection = None
        self._process = None
        self._listener_context = None
        try:
            if connection is not None:
                connection.close()
            if process is not None and process.poll() is None:
                _stop_worker(process)
        finally:
            if listener_context is not None:
                listener_context.__exit__(None, None, None)

    def _ensure_started(
        self,
        cancellation: CancellationToken | None,
    ) -> tuple[Connection, subprocess.Popen[bytes]]:
        if self._connection is not None and self._process is not None:
            if self._process.poll() is not None:
                self._release_worker()
                raise TranslationError("El proceso de traducción local ya no está disponible.")
            return self._connection, self._process

        auth_key = secrets.token_bytes(32)
        listener_context = _private_listener(auth_key)
        listener, address, family = listener_context.__enter__()
        process: subprocess.Popen[bytes] | None = None
        connection: Connection | None = None
        try:
            process = _start_worker_process(address, family, auth_key)
            connection = _accept_worker(listener, process, cancellation)
            message = _receive_until_deadline(connection, process, cancellation)
            if message != {"type": "hello", "version": PROTOCOL_VERSION}:
                raise OfflineTranslationProtocolError(
                    "El proceso de traducción usa un protocolo incompatible."
                )
        except Exception:
            if connection is not None:
                connection.close()
            if process is not None and process.poll() is None:
                _stop_worker(process)
            listener_context.__exit__(None, None, None)
            raise
        self._listener_context = listener_context
        self._connection = connection
        self._process = process
        return connection, process

    def _receive_result(
        self,
        connection: Connection,
        process: subprocess.Popen[bytes],
        job_id: str,
        *,
        on_progress: Callable[[int, int], None] | None,
        on_engine_ready: Callable[[], None] | None,
        cancellation: CancellationToken | None,
    ) -> str:
        cancellation_sent_at: float | None = None
        engine_ready = False
        while True:
            if cancellation is not None and cancellation.is_cancelled:
                if cancellation_sent_at is None:
                    send_message(connection, {"type": "cancel", "job_id": job_id})
                    cancellation_sent_at = time.monotonic()
                elif time.monotonic() - cancellation_sent_at >= _CANCEL_GRACE_SECONDS:
                    _stop_worker(process)
                    cancellation.check()

            if connection.poll(_POLL_SECONDS):
                message = receive_message(connection)
                if message.get("job_id") != job_id:
                    raise OfflineTranslationProtocolError(
                        "El proceso de traducción respondió a otro trabajo."
                    )
                message_type = message["type"]
                if message_type == "ready":
                    if engine_ready:
                        raise OfflineTranslationProtocolError(
                            "El proceso declaró dos veces que el motor estaba preparado."
                        )
                    engine_ready = True
                    if on_engine_ready is not None:
                        on_engine_ready()
                elif message_type == "progress":
                    current = message.get("current")
                    total = message.get("total")
                    if (
                        isinstance(current, bool)
                        or not isinstance(current, int)
                        or isinstance(total, bool)
                        or not isinstance(total, int)
                        or current < 1
                        or total < current
                    ):
                        raise OfflineTranslationProtocolError(
                            "El proceso devolvió un progreso no válido."
                        )
                    if on_progress is not None:
                        on_progress(current, total)
                elif message_type == "result":
                    result = message.get("markdown")
                    if not isinstance(result, str) or not result.strip() or "\0" in result:
                        raise OfflineTranslationProtocolError(
                            "El proceso devolvió una traducción no válida."
                        )
                    check_cancelled(cancellation)
                    return result
                elif message_type == "cancelled":
                    raise ProcessingCancelledError("El procesamiento se canceló de forma segura.")
                elif message_type == "error":
                    code = message.get("code")
                    error_message = message.get("message")
                    if (
                        code not in _VALID_ERROR_CODES
                        or not isinstance(error_message, str)
                        or not error_message
                        or len(error_message) > 500
                    ):
                        raise OfflineTranslationProtocolError(
                            "El proceso devolvió un error no válido."
                        )
                    raise TranslationError(error_message)
                else:
                    raise OfflineTranslationProtocolError(
                        "El proceso devolvió un mensaje desconocido."
                    )
            elif process.poll() is not None:
                raise TranslationError("El proceso de traducción local terminó inesperadamente.")


@contextmanager
def offline_translation_session() -> Iterator[OfflineTranslationSession]:
    """Reuse one short-lived worker across all translation parts of a document."""

    existing = _ACTIVE_SESSION.get()
    if existing is not None:
        yield existing
        return
    session = OfflineTranslationSession()
    token: Token[OfflineTranslationSession | None] = _ACTIVE_SESSION.set(session)
    try:
        yield session
    finally:
        _ACTIVE_SESSION.reset(token)
        session.close()


def translate_markdown_in_worker(
    markdown: str,
    target_language: str,
    **kwargs: Any,
) -> str:
    """Use the active document worker or create a one-shot worker."""

    session = _ACTIVE_SESSION.get()
    if session is not None:
        return session.translate(markdown, target_language, **kwargs)
    with offline_translation_session() as one_shot_session:
        return one_shot_session.translate(markdown, target_language, **kwargs)


@contextmanager
def _private_listener(auth_key: bytes) -> Iterator[tuple[Listener, str, str]]:
    with private_listener(auth_key, channel_name="translation") as opened_listener:
        yield opened_listener


def _start_worker_process(address: str, family: str, auth_key: bytes) -> subprocess.Popen[bytes]:
    return start_private_process(
        address,
        family,
        auth_key,
        auth_environment_variable=_AUTH_ENVIRONMENT_VARIABLE,
        module="parsezen.offline_translation_worker",
        frozen_switch="--translation-worker",
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
            thread_name="translation-worker-accept",
        )
    except PrivateChannelProcessExited as exc:
        raise TranslationError("El proceso de traducción local no pudo iniciarse.") from exc
    except PrivateChannelTimeout as exc:
        raise TranslationError(
            "El proceso de traducción local tardó demasiado en iniciarse."
        ) from exc
    except PrivateChannelAcceptError as exc:
        raise TranslationError("No se pudo abrir el canal privado de traducción.") from exc


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
        raise TranslationError(
            "El proceso de traducción local tardó demasiado en responder."
        ) from exc


def _wait_for_clean_exit(process: subprocess.Popen[bytes]) -> None:
    try:
        exit_code = process.wait(timeout=_PROCESS_EXIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _stop_worker(process)
        return
    if exit_code != 0:
        raise TranslationError("El proceso de traducción local terminó inesperadamente.")


def _stop_worker(process: subprocess.Popen[bytes]) -> None:
    stop_private_process(process, exit_timeout_seconds=_PROCESS_EXIT_TIMEOUT_SECONDS)


def _elapsed_milliseconds(started_at: float) -> int:
    return max(0, round((time.monotonic() - started_at) * 1000))
