"""Private subprocess entry point for free offline translation."""

from __future__ import annotations

import argparse
import base64
import os
import threading
from collections.abc import Callable, Sequence
from multiprocessing.connection import Client, Connection
from typing import Any

from parsezen.cancellation import CancellationToken
from parsezen.errors import ProcessingCancelledError, TranslationError
from parsezen.offline_translation_protocol import (
    PROTOCOL_VERSION,
    OfflineTranslationProtocolError,
    receive_message,
    send_message,
)

_AUTH_ENVIRONMENT_VARIABLE = "PARSEZEN_TRANSLATION_AUTH"
_GENERIC_WORKER_ERROR = "El traductor offline no pudo completar el documento."


def main(argv: Sequence[str] | None = None) -> int:
    """Connect to the authenticated parent and serve translation requests."""

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--address", required=True)
    parser.add_argument("--family", choices=("AF_PIPE", "AF_UNIX"), required=True)
    try:
        arguments = parser.parse_args(argv)
        encoded_auth_key = os.environ.pop(_AUTH_ENVIRONMENT_VARIABLE)
        auth_key = base64.urlsafe_b64decode(encoded_auth_key.encode("ascii"))
        if len(auth_key) != 32:
            return 2
        connection = Client(arguments.address, family=arguments.family, authkey=auth_key)
    except (KeyError, OSError, ValueError):
        return 2

    with connection:
        return serve_connection(connection)


def serve_connection(
    connection: Connection,
    *,
    translator: Callable[..., str] | None = None,
    exit_on_parent_loss: bool = True,
) -> int:
    """Serve serial requests so one loaded model can translate a complete job."""

    send_message(connection, {"type": "hello", "version": PROTOCOL_VERSION})
    while True:
        try:
            request = receive_message(connection)
        except (EOFError, OfflineTranslationProtocolError):
            return 2
        if request == {"type": "close", "version": PROTOCOL_VERSION}:
            return 0
        try:
            job_id, markdown, target_language, source_language_code = _validated_request(request)
        except OfflineTranslationProtocolError:
            return 2

        cancellation = CancellationToken()
        completed = threading.Event()
        controller = threading.Thread(
            target=_listen_for_control_messages,
            args=(connection, job_id, cancellation, completed, exit_on_parent_loss),
            name="translation-worker-control",
            daemon=True,
        )
        controller.start()

        def on_progress(current: int, total: int, job_identifier: str = job_id) -> None:
            send_message(
                connection,
                {
                    "type": "progress",
                    "job_id": job_identifier,
                    "current": current,
                    "total": total,
                },
            )

        def on_engine_ready(job_identifier: str = job_id) -> None:
            send_message(connection, {"type": "ready", "job_id": job_identifier})

        try:
            if translator is None:
                from parsezen.offline_translation import _translate_markdown_offline_in_process

                active_translator = _translate_markdown_offline_in_process
            else:
                active_translator = translator
            result = active_translator(
                markdown,
                target_language,
                source_language_code=source_language_code,
                on_progress=on_progress,
                on_engine_ready=on_engine_ready,
                cancellation=cancellation,
            )
            cancellation.check()
        except ProcessingCancelledError:
            _complete_controller(completed, controller)
            send_message(connection, {"type": "cancelled", "job_id": job_id})
        except TranslationError as exc:
            _complete_controller(completed, controller)
            send_message(
                connection,
                {
                    "type": "error",
                    "job_id": job_id,
                    "code": "translation_failed",
                    "message": str(exc),
                },
            )
        except Exception:
            _complete_controller(completed, controller)
            send_message(
                connection,
                {
                    "type": "error",
                    "job_id": job_id,
                    "code": "unexpected",
                    "message": _GENERIC_WORKER_ERROR,
                },
            )
        else:
            _complete_controller(completed, controller)
            send_message(connection, {"type": "result", "job_id": job_id, "markdown": result})


def _validated_request(request: dict[str, Any]) -> tuple[str, str, str, str | None]:
    if request.get("type") != "run" or request.get("version") != PROTOCOL_VERSION:
        raise OfflineTranslationProtocolError("La petición de traducción no es compatible.")
    job_id = request.get("job_id")
    markdown = request.get("markdown")
    target_language = request.get("target_language")
    source_language_code = request.get("source_language_code")
    if (
        not isinstance(job_id, str)
        or len(job_id) != 32
        or not isinstance(markdown, str)
        or not markdown.strip()
        or "\0" in markdown
        or not isinstance(target_language, str)
        or not target_language.strip()
        or (source_language_code is not None and not isinstance(source_language_code, str))
    ):
        raise OfflineTranslationProtocolError("La petición de traducción no es válida.")
    return job_id, markdown, target_language, source_language_code


def _complete_controller(completed: threading.Event, controller: threading.Thread) -> None:
    completed.set()
    controller.join(timeout=0.2)


def _listen_for_control_messages(
    connection: Connection,
    job_id: str,
    cancellation: CancellationToken,
    completed: threading.Event,
    exit_on_parent_loss: bool,
) -> None:
    while not completed.wait(0.05):
        try:
            if not connection.poll(0.05):
                continue
            message = receive_message(connection)
        except (EOFError, OSError, OfflineTranslationProtocolError):
            if not completed.is_set() and exit_on_parent_loss:
                os._exit(3)
            return
        if message.get("type") == "cancel" and message.get("job_id") == job_id:
            cancellation.cancel()


if __name__ == "__main__":
    raise SystemExit(main())
