from __future__ import annotations

import logging
import subprocess
import threading
from multiprocessing import Pipe

import pytest

import parsezen.offline_translation_executor as executor
from parsezen.cancellation import CancellationToken
from parsezen.errors import ProcessingCancelledError, TranslationError
from parsezen.offline_translation_executor import (
    OfflineTranslationSession,
    _accept_worker,
    _private_listener,
    _start_worker_process,
    _stop_worker,
)
from parsezen.offline_translation_protocol import (
    PROTOCOL_VERSION,
    OfflineTranslationProtocolError,
    receive_message,
    send_message,
)


def test_real_translation_worker_completes_the_private_handshake() -> None:
    auth_key = b"t" * 32
    process = None
    connection = None
    with _private_listener(auth_key) as (listener, address, family):
        try:
            process = _start_worker_process(address, family, auth_key)
            connection = _accept_worker(listener, process, None)
            assert receive_message(connection) == {
                "type": "hello",
                "version": PROTOCOL_VERSION,
            }
            send_message(connection, {"type": "invalid"})
            assert process.wait(timeout=10) == 2
        finally:
            if connection is not None:
                connection.close()
            if process is not None and process.poll() is None:
                _stop_worker(process)


class _SessionProcess:
    def __init__(self, stopped: threading.Event) -> None:
        self.stopped = stopped
        self.terminated = False

    def poll(self) -> int | None:
        return 0 if self.stopped.is_set() else None

    def wait(self, timeout: float) -> int:
        assert self.stopped.wait(timeout)
        return 0

    def terminate(self) -> None:
        self.terminated = True
        self.stopped.set()

    def kill(self) -> None:
        self.terminated = True
        self.stopped.set()


class _ListenerContext:
    def __init__(self) -> None:
        self.exited = False

    def __exit__(self, *_args: object) -> None:
        self.exited = True


def test_session_translates_reports_progress_and_closes_cleanly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    parent, worker = Pipe(duplex=True)
    stopped = threading.Event()
    process = _SessionProcess(stopped)
    listener_context = _ListenerContext()
    session = OfflineTranslationSession()
    session._connection = parent
    session._process = process  # type: ignore[assignment]
    session._listener_context = listener_context
    progress: list[tuple[int, int]] = []
    ready: list[bool] = []

    def serve() -> None:
        request = receive_message(worker)
        job_id = request["job_id"]
        send_message(worker, {"type": "ready", "job_id": job_id})
        send_message(
            worker,
            {"type": "progress", "job_id": job_id, "current": 1, "total": 1},
        )
        send_message(worker, {"type": "result", "job_id": job_id, "markdown": "Traducido"})
        assert receive_message(worker) == {"type": "close", "version": PROTOCOL_VERSION}
        stopped.set()
        worker.close()

    thread = threading.Thread(target=serve)
    thread.start()
    with caplog.at_level(logging.INFO):
        result = session.translate(
            "Text long enough to translate.",
            "Español",
            on_progress=lambda current, total: progress.append((current, total)),
            on_engine_ready=lambda: ready.append(True),
        )
        session.close()
    thread.join(timeout=3)

    assert result == "Traducido"
    assert progress == [(1, 1)]
    assert ready == [True]
    assert listener_context.exited is True
    assert not thread.is_alive()
    assert "translation_worker_completed" in caplog.text


def test_session_discards_a_worker_after_an_invalid_response() -> None:
    parent, worker = Pipe(duplex=True)
    stopped = threading.Event()
    process = _SessionProcess(stopped)
    listener_context = _ListenerContext()
    session = OfflineTranslationSession()
    session._connection = parent
    session._process = process  # type: ignore[assignment]
    session._listener_context = listener_context

    def serve() -> None:
        request = receive_message(worker)
        send_message(worker, {"type": "unknown", "job_id": request["job_id"]})
        stopped.wait(3)
        worker.close()

    thread = threading.Thread(target=serve)
    thread.start()
    with pytest.raises(TranslationError, match="terminó de forma inesperada"):
        session.translate("Text long enough to translate.", "Español")
    thread.join(timeout=3)

    assert process.terminated is True
    assert listener_context.exited is True
    assert not thread.is_alive()


class _MessageConnection:
    def __init__(self, messages: list[dict[str, object]]) -> None:
        self.messages = messages
        self.closed = False

    def poll(self, _timeout: float) -> bool:
        return bool(self.messages)

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self, return_code: int | None = None) -> None:
        self.return_code = return_code
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.return_code

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.return_code = -9

    def wait(self, timeout: float) -> int:
        del timeout
        return self.return_code or 0


def _patch_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor, "receive_message", lambda connection: connection.messages.pop(0))


def test_receive_result_accepts_ready_progress_and_translation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_messages(monkeypatch)
    connection = _MessageConnection(
        [
            {"type": "ready", "job_id": "job"},
            {"type": "progress", "job_id": "job", "current": 1, "total": 2},
            {"type": "result", "job_id": "job", "markdown": "Traducido"},
        ]
    )
    ready: list[bool] = []
    progress: list[tuple[int, int]] = []

    result = OfflineTranslationSession()._receive_result(
        connection,  # type: ignore[arg-type]
        _FakeProcess(),  # type: ignore[arg-type]
        "job",
        on_progress=lambda current, total: progress.append((current, total)),
        on_engine_ready=lambda: ready.append(True),
        cancellation=None,
    )

    assert result == "Traducido"
    assert ready == [True]
    assert progress == [(1, 2)]


@pytest.mark.parametrize(
    ("messages", "error"),
    [
        ([{"type": "result", "job_id": "other", "markdown": "Text"}], "otro trabajo"),
        (
            [
                {"type": "ready", "job_id": "job"},
                {"type": "ready", "job_id": "job"},
            ],
            "dos veces",
        ),
        (
            [{"type": "progress", "job_id": "job", "current": 0, "total": 1}],
            "progreso",
        ),
        ([{"type": "result", "job_id": "job", "markdown": "\0"}], "traducción"),
        ([{"type": "error", "job_id": "job", "code": "bad", "message": "x"}], "error"),
        ([{"type": "unknown", "job_id": "job"}], "desconocido"),
    ],
)
def test_receive_result_rejects_invalid_worker_messages(
    monkeypatch: pytest.MonkeyPatch,
    messages: list[dict[str, object]],
    error: str,
) -> None:
    _patch_messages(monkeypatch)

    with pytest.raises(OfflineTranslationProtocolError, match=error):
        OfflineTranslationSession()._receive_result(
            _MessageConnection(messages),  # type: ignore[arg-type]
            _FakeProcess(),  # type: ignore[arg-type]
            "job",
            on_progress=None,
            on_engine_ready=None,
            cancellation=None,
        )


def test_receive_result_surfaces_cancelled_engine_error_and_dead_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_messages(monkeypatch)
    with pytest.raises(ProcessingCancelledError):
        OfflineTranslationSession()._receive_result(
            _MessageConnection([{"type": "cancelled", "job_id": "job"}]),  # type: ignore[arg-type]
            _FakeProcess(),  # type: ignore[arg-type]
            "job",
            on_progress=None,
            on_engine_ready=None,
            cancellation=None,
        )
    with pytest.raises(TranslationError, match="falló"):
        OfflineTranslationSession()._receive_result(
            _MessageConnection(
                [
                    {
                        "type": "error",
                        "job_id": "job",
                        "code": "translation_failed",
                        "message": "El motor falló",
                    }
                ]
            ),  # type: ignore[arg-type]
            _FakeProcess(),  # type: ignore[arg-type]
            "job",
            on_progress=None,
            on_engine_ready=None,
            cancellation=None,
        )
    with pytest.raises(TranslationError, match="terminó inesperadamente"):
        OfflineTranslationSession()._receive_result(
            _MessageConnection([]),  # type: ignore[arg-type]
            _FakeProcess(1),  # type: ignore[arg-type]
            "job",
            on_progress=None,
            on_engine_ready=None,
            cancellation=None,
        )


def test_closed_or_dead_sessions_fail_without_reusing_the_worker() -> None:
    closed = OfflineTranslationSession()
    closed.close()
    closed.close()
    with pytest.raises(TranslationError, match="ya se cerró"):
        closed.translate("Text", "Español")

    dead = OfflineTranslationSession()
    dead._connection = _MessageConnection([])  # type: ignore[assignment]
    dead._process = _FakeProcess(1)  # type: ignore[assignment]
    with pytest.raises(TranslationError, match="ya no está disponible"):
        dead._ensure_started(None)


def test_nested_translation_context_reuses_one_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[bool] = []
    monkeypatch.setattr(
        OfflineTranslationSession,
        "close",
        lambda _self: closed.append(True),
    )

    with executor.offline_translation_session() as outer:
        with executor.offline_translation_session() as inner:
            assert inner is outer

    assert closed == [True]


def test_cancelled_translation_forces_the_worker_after_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _MessageConnection([])
    process = _FakeProcess()
    cancellation = CancellationToken()
    cancellation.cancel()
    sent: list[dict[str, object]] = []
    times = iter((0.0, executor._CANCEL_GRACE_SECONDS + 1))
    monkeypatch.setattr(executor.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(executor, "send_message", lambda _connection, message: sent.append(message))
    monkeypatch.setattr(executor, "_stop_worker", lambda worker: worker.terminate())

    with pytest.raises(ProcessingCancelledError):
        OfflineTranslationSession()._receive_result(
            connection,  # type: ignore[arg-type]
            process,  # type: ignore[arg-type]
            "job",
            on_progress=None,
            on_engine_ready=None,
            cancellation=cancellation,
        )

    assert sent == [{"type": "cancel", "job_id": "job"}]
    assert process.terminated


def test_translation_process_shutdown_handles_timeout_and_nonzero_exit() -> None:
    with pytest.raises(TranslationError, match="inesperadamente"):
        executor._wait_for_clean_exit(_FakeProcess(3))  # type: ignore[arg-type]

    process = _FakeProcess()

    def timeout(timeout: float) -> int:
        del timeout
        if not process.killed:
            raise subprocess.TimeoutExpired("worker", 1)
        return -9

    process.wait = timeout  # type: ignore[method-assign]
    executor._stop_worker(process)  # type: ignore[arg-type]
    assert process.terminated and process.killed


class _PrivateContext:
    def __init__(self) -> None:
        self.exited = False

    def __enter__(self) -> tuple[object, str, str]:
        return object(), "address", "AF_PIPE"

    def __exit__(self, *_args: object) -> None:
        self.exited = True


def test_ensure_started_performs_one_handshake_and_reuses_the_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _PrivateContext()
    connection = _MessageConnection([])
    process = _FakeProcess()
    monkeypatch.setattr(executor, "_private_listener", lambda _key: context)
    monkeypatch.setattr(executor, "_start_worker_process", lambda *_args: process)
    monkeypatch.setattr(executor, "_accept_worker", lambda *_args: connection)
    monkeypatch.setattr(
        executor,
        "_receive_until_deadline",
        lambda *_args: {"type": "hello", "version": PROTOCOL_VERSION},
    )
    session = OfflineTranslationSession()

    assert session._ensure_started(None) == (connection, process)
    assert session._ensure_started(None) == (connection, process)
    session._release_worker()

    assert connection.closed
    assert process.terminated
    assert context.exited


def test_ensure_started_cleans_up_an_incompatible_handshake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _PrivateContext()
    connection = _MessageConnection([])
    process = _FakeProcess()
    monkeypatch.setattr(executor, "_private_listener", lambda _key: context)
    monkeypatch.setattr(executor, "_start_worker_process", lambda *_args: process)
    monkeypatch.setattr(executor, "_accept_worker", lambda *_args: connection)
    monkeypatch.setattr(
        executor,
        "_receive_until_deadline",
        lambda *_args: {"type": "hello", "version": -1},
    )
    monkeypatch.setattr(executor, "_stop_worker", lambda worker: worker.terminate())

    with pytest.raises(OfflineTranslationProtocolError, match="incompatible"):
        OfflineTranslationSession()._ensure_started(None)

    assert connection.closed
    assert process.terminated
    assert context.exited


def test_start_translation_worker_builds_source_and_frozen_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    process = _FakeProcess()

    def popen(command: list[str], **kwargs: object) -> _FakeProcess:
        captured["command"] = command
        captured.update(kwargs)
        return process

    monkeypatch.setattr(executor.subprocess, "Popen", popen)
    monkeypatch.setattr(executor.sys, "frozen", False, raising=False)
    assert executor._start_worker_process("address", "family", b"a" * 32) is process
    assert captured["command"][1:3] == [  # type: ignore[index]
        "-m",
        "parsezen.offline_translation_worker",
    ]
    assert "PARSEZEN_TRANSLATION_AUTH" in captured["env"]  # type: ignore[operator]

    monkeypatch.setattr(executor.sys, "frozen", True)
    executor._start_worker_process("address", "family", b"b" * 32)
    assert captured["command"][1] == "--translation-worker"  # type: ignore[index]


class _AcceptListener:
    def __init__(self, result: object) -> None:
        self.result = result

    def accept(self) -> object:
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def test_translation_accept_and_deadline_helpers_cover_failure_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _MessageConnection([])
    assert (
        executor._accept_worker(
            _AcceptListener(connection),  # type: ignore[arg-type]
            _FakeProcess(),  # type: ignore[arg-type]
            None,
        )
        is connection
    )
    with pytest.raises(TranslationError, match="canal privado"):
        executor._accept_worker(
            _AcceptListener(OSError("closed")),  # type: ignore[arg-type]
            _FakeProcess(),  # type: ignore[arg-type]
            None,
        )
    with pytest.raises(TranslationError, match="iniciarse"):
        executor._accept_worker(
            _AcceptListener(connection),  # type: ignore[arg-type]
            _FakeProcess(1),  # type: ignore[arg-type]
            None,
        )

    _patch_messages(monkeypatch)
    assert executor._receive_until_deadline(
        _MessageConnection([{"type": "hello"}]),  # type: ignore[arg-type]
        _FakeProcess(),  # type: ignore[arg-type]
        None,
    ) == {"type": "hello"}
    with pytest.raises(EOFError):
        executor._receive_until_deadline(
            _MessageConnection([]),  # type: ignore[arg-type]
            _FakeProcess(1),  # type: ignore[arg-type]
            None,
        )
    monkeypatch.setattr(executor, "_STARTUP_TIMEOUT_SECONDS", 0)
    with pytest.raises(TranslationError, match="demasiado"):
        executor._receive_until_deadline(
            _MessageConnection([]),  # type: ignore[arg-type]
            _FakeProcess(),  # type: ignore[arg-type]
            None,
        )


def test_close_falls_back_to_forced_stop_when_the_channel_is_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _MessageConnection([])
    process = _FakeProcess()
    session = OfflineTranslationSession()
    session._connection = connection  # type: ignore[assignment]
    session._process = process  # type: ignore[assignment]
    monkeypatch.setattr(
        executor,
        "send_message",
        lambda *_args: (_ for _ in ()).throw(BrokenPipeError()),
    )
    monkeypatch.setattr(executor, "_stop_worker", lambda worker: worker.terminate())

    session.close()

    assert process.terminated
    assert connection.closed
