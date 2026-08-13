"""Small authenticated channel primitives for local child processes."""

from __future__ import annotations

import base64
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from multiprocessing.connection import Connection, Listener
from pathlib import Path
from typing import Any

from parsezen.cancellation import CancellationToken, check_cancelled


class PrivateChannelProcessExited(Exception):
    """The child exited before establishing or completing its handshake."""


class PrivateChannelTimeout(Exception):
    """The child did not establish or answer the channel before its deadline."""


class PrivateChannelAcceptError(Exception):
    """The authenticated listener could not accept its child connection."""


def send_bounded_json(
    connection: Any,
    message: dict[str, Any],
    *,
    max_bytes: int,
    error_type: type[Exception],
    invalid_type_message: str,
    serialization_message: str,
    size_message: str,
) -> None:
    """Serialize one bounded JSON object without allowing pickle."""

    if not isinstance(message.get("type"), str):
        raise error_type(invalid_type_message)
    try:
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise error_type(serialization_message) from exc
    if len(payload) > max_bytes:
        raise error_type(size_message)
    connection.send_bytes(payload)


def receive_bounded_json(
    connection: Any,
    *,
    max_bytes: int,
    error_type: type[Exception],
    invalid_message: str,
) -> dict[str, Any]:
    """Receive one bounded JSON object without constructing arbitrary objects."""

    try:
        payload = connection.recv_bytes(max_bytes)
        message = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise error_type(invalid_message) from exc
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise error_type(invalid_message)
    return message


@contextmanager
def private_listener(
    auth_key: bytes,
    *,
    channel_name: str,
) -> Iterator[tuple[Listener, str, str]]:
    """Open one authenticated OS-local listener and remove its socket afterwards."""

    temporary_directory: tempfile.TemporaryDirectory[str] | None = None
    if os.name == "nt":
        family = "AF_PIPE"
        address = rf"\\.\pipe\parsezen-{channel_name}-{uuid.uuid4().hex}"
    else:
        family = "AF_UNIX"
        temporary_directory = tempfile.TemporaryDirectory(prefix=f"parsezen-{channel_name}-")
        address = str(Path(temporary_directory.name) / "worker.sock")
    listener = Listener(address=address, family=family, authkey=auth_key)
    try:
        yield listener, address, family
    finally:
        listener.close()
        if temporary_directory is not None:
            temporary_directory.cleanup()


def start_private_process(
    address: str,
    family: str,
    auth_key: bytes,
    *,
    auth_environment_variable: str,
    module: str,
    frozen_switch: str,
    executable: str | None = None,
    frozen: bool | None = None,
) -> subprocess.Popen[bytes]:
    """Spawn one hidden local worker with only its authenticated endpoint."""

    environment = os.environ.copy()
    environment[auth_environment_variable] = base64.urlsafe_b64encode(auth_key).decode("ascii")
    process_executable = executable or sys.executable
    is_frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if is_frozen:
        command = [process_executable, frozen_switch, "--address", address, "--family", family]
    else:
        source_root = str(Path(__file__).resolve().parents[2])
        inherited_path = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            os.pathsep.join((source_root, inherited_path)) if inherited_path else source_root
        )
        command = [process_executable, "-m", module, "--address", address, "--family", family]
    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def accept_private_connection(
    listener: Listener,
    process: subprocess.Popen[bytes],
    cancellation: CancellationToken | None,
    *,
    timeout_seconds: float,
    poll_seconds: float,
    thread_name: str,
) -> Connection:
    """Accept without blocking cancellation or a child that exited during startup."""

    accepted: queue.Queue[Connection | BaseException] = queue.Queue(maxsize=1)

    def accept() -> None:
        try:
            accepted.put(listener.accept())
        except BaseException as exc:  # pragma: no cover - platform listener details
            accepted.put(exc)

    threading.Thread(target=accept, name=thread_name, daemon=True).start()
    deadline = time.monotonic() + timeout_seconds
    while True:
        check_cancelled(cancellation)
        if process.poll() is not None:
            raise PrivateChannelProcessExited()
        try:
            result = accepted.get(timeout=poll_seconds)
        except queue.Empty:
            if time.monotonic() >= deadline:
                raise PrivateChannelTimeout() from None
            continue
        if isinstance(result, BaseException):
            raise PrivateChannelAcceptError from result
        return result


def receive_initial_message(
    connection: Connection,
    process: subprocess.Popen[bytes],
    cancellation: CancellationToken | None,
    *,
    receive_message: Callable[[Connection], dict[str, Any]],
    timeout_seconds: float,
    poll_seconds: float,
) -> dict[str, Any]:
    """Wait for one bounded protocol message during the startup handshake."""

    deadline = time.monotonic() + timeout_seconds
    while True:
        check_cancelled(cancellation)
        if connection.poll(poll_seconds):
            return receive_message(connection)
        if process.poll() is not None:
            raise EOFError()
        if time.monotonic() >= deadline:
            raise PrivateChannelTimeout()


def stop_private_process(
    process: subprocess.Popen[bytes],
    *,
    exit_timeout_seconds: float,
) -> None:
    """Terminate and, only after the grace period, kill one local child."""

    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=exit_timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=exit_timeout_seconds)


__all__ = [
    "PrivateChannelAcceptError",
    "PrivateChannelProcessExited",
    "PrivateChannelTimeout",
    "accept_private_connection",
    "private_listener",
    "receive_bounded_json",
    "receive_initial_message",
    "send_bounded_json",
    "start_private_process",
    "stop_private_process",
]
