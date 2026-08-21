from __future__ import annotations

import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

import parsezen.ocr_executor as executor
from parsezen.cancellation import CancellationToken
from parsezen.errors import ConversionError, ProcessingCancelledError
from parsezen.ocr_protocol import PROTOCOL_VERSION, OcrProtocolError


class _Connection:
    def __init__(self, messages: list[dict[str, Any]] | None = None) -> None:
        self.messages = messages or []
        self.closed = False

    def poll(self, _timeout: float) -> bool:
        return bool(self.messages)

    def close(self) -> None:
        self.closed = True


class _Process:
    def __init__(self, poll_result: int | None = None) -> None:
        self.poll_result = poll_result
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.poll_result

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.poll_result = -9

    def wait(self, timeout: float) -> int:
        del timeout
        return self.poll_result or 0


def _patch_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor, "receive_message", lambda connection: connection.messages.pop(0))


def test_empty_request_avoids_starting_a_worker() -> None:
    assert executor.run_ocr_worker(Path("unused.pdf"), set()) == {}


def test_page_validation_accepts_positive_integers_and_rejects_unsafe_values() -> None:
    assert executor._validated_pages({3, 1}) == {1, 3}
    for invalid in ({0}, {True}, {"1"}):
        with pytest.raises(ConversionError, match="selección"):
            executor._validated_pages(invalid)  # type: ignore[arg-type]


def test_worker_source_path_uses_ascii_directly_and_copies_unicode(
    tmp_path: Path,
) -> None:
    ascii_source = tmp_path / "book.pdf"
    ascii_source.write_bytes(b"pdf")
    with executor._worker_source_path(ascii_source) as prepared:
        assert prepared == ascii_source.resolve()

    unicode_source = tmp_path / "libro-á.pdf"
    unicode_source.write_bytes(b"unicode-pdf")
    with executor._worker_source_path(unicode_source) as prepared:
        assert prepared != unicode_source
        assert prepared.name == "parsezen.pdf"
        assert prepared.read_bytes() == b"unicode-pdf"
    assert not prepared.exists()

    with pytest.raises(ConversionError, match="preparar"):
        with executor._worker_source_path(tmp_path / "missing.pdf"):
            pass


def test_receive_worker_result_accepts_the_strict_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_messages(monkeypatch)
    job_id = "job"
    connection = _Connection(
        [
            {"type": "stage", "job_id": job_id, "stage": "preparing"},
            {"type": "stage", "job_id": job_id, "stage": "running"},
            {"type": "progress", "job_id": job_id, "current": 1, "total": 1},
            {"type": "page", "job_id": job_id, "page_number": 2, "markdown": "Text"},
            {"type": "complete", "job_id": job_id},
        ]
    )
    progress: list[tuple[int, int]] = []
    pages: list[tuple[int, str]] = []
    stages: list[str] = []

    result = executor._receive_worker_result(
        connection,  # type: ignore[arg-type]
        _Process(),  # type: ignore[arg-type]
        job_id,
        {2},
        None,
        on_progress=lambda current, total: progress.append((current, total)),
        on_page_result=lambda page, text: pages.append((page, text)),
        on_stage=stages.append,
    )

    assert result == {2: "Text"}
    assert progress == [(1, 1)]
    assert pages == [(2, "Text")]
    assert stages == ["preparing", "running"]


@pytest.mark.parametrize(
    "messages",
    [
        [{"type": "complete", "job_id": "other"}],
        [{"type": "stage", "job_id": "job", "stage": "running"}],
        [
            {"type": "stage", "job_id": "job", "stage": "preparing"},
            {"type": "progress", "job_id": "job", "current": 0, "total": 1},
        ],
        [
            {"type": "stage", "job_id": "job", "stage": "preparing"},
            {"type": "stage", "job_id": "job", "stage": "running"},
            {"type": "page", "job_id": "job", "page_number": 3, "markdown": "Text"},
        ],
        [{"type": "complete", "job_id": "job"}],
        [{"type": "error", "job_id": "job", "code": "invalid", "message": "No"}],
        [{"type": "unknown", "job_id": "job"}],
    ],
)
def test_receive_worker_result_rejects_invalid_protocol_messages(
    monkeypatch: pytest.MonkeyPatch,
    messages: list[dict[str, Any]],
) -> None:
    _patch_messages(monkeypatch)

    with pytest.raises(OcrProtocolError):
        executor._receive_worker_result(
            _Connection(messages),  # type: ignore[arg-type]
            _Process(),  # type: ignore[arg-type]
            "job",
            {2},
            None,
        )


def test_receive_worker_result_reports_worker_errors_and_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_messages(monkeypatch)
    diagnostic = {
        "error_type": "RuntimeError",
        "module": "ocr.engine",
        "function": "run",
        "line": 42,
    }
    with pytest.raises(ConversionError, match="OCR failed"):
        executor._receive_worker_result(
            _Connection(
                [
                    {
                        "type": "error",
                        "job_id": "job",
                        "code": "conversion_failed",
                        "message": "OCR failed",
                        "diagnostic": diagnostic,
                    }
                ]
            ),  # type: ignore[arg-type]
            _Process(),  # type: ignore[arg-type]
            "job",
            {1},
            None,
        )
    with pytest.raises(ProcessingCancelledError):
        executor._receive_worker_result(
            _Connection([{"type": "cancelled", "job_id": "job"}]),  # type: ignore[arg-type]
            _Process(),  # type: ignore[arg-type]
            "job",
            {1},
            None,
        )


def test_run_worker_orchestrates_the_private_process(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _Connection()
    process = _Process()
    sent: list[dict[str, Any]] = []

    @contextmanager
    def listener(_auth_key: bytes):
        yield object(), "private-address", "AF_PIPE"

    monkeypatch.setattr(executor, "_private_listener", listener)
    monkeypatch.setattr(executor, "_start_worker_process", lambda *_args: process)
    monkeypatch.setattr(executor, "_accept_worker", lambda *_args: connection)
    monkeypatch.setattr(executor, "_expect_hello", lambda *_args: None)
    monkeypatch.setattr(executor, "send_message", lambda _connection, message: sent.append(message))
    monkeypatch.setattr(
        executor,
        "_receive_worker_result",
        lambda *_args, **_kwargs: {2: "OCR"},
    )
    monkeypatch.setattr(executor, "_wait_for_clean_exit", lambda _process: None)

    result = executor._run_ocr_worker(
        Path(__file__),
        {2},
        {2},
        None,
        None,
        None,
    )

    assert result == {2: "OCR"}
    assert sent[0]["type"] == "run"
    assert sent[0]["page_numbers"] == [2]
    assert sent[0]["force_full_page_numbers"] == [2]
    assert connection.closed


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ProcessingCancelledError("cancelled"), ProcessingCancelledError),
        (OcrProtocolError("invalid"), ConversionError),
    ],
)
def test_run_worker_stops_the_process_on_controlled_failures(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected: type[Exception],
) -> None:
    connection = _Connection()
    process = _Process()

    @contextmanager
    def listener(_auth_key: bytes):
        yield object(), "private-address", "AF_PIPE"

    monkeypatch.setattr(executor, "_private_listener", listener)
    monkeypatch.setattr(executor, "_start_worker_process", lambda *_args: process)
    monkeypatch.setattr(executor, "_accept_worker", lambda *_args: connection)
    monkeypatch.setattr(executor, "_expect_hello", lambda *_args: None)
    monkeypatch.setattr(executor, "send_message", lambda *_args: None)
    monkeypatch.setattr(
        executor,
        "_receive_worker_result",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )
    monkeypatch.setattr(executor, "_stop_worker", lambda worker: worker.terminate())

    with pytest.raises(expected):
        executor._run_ocr_worker(
            Path(__file__),
            {1},
            set(),
            None,
            None,
            None,
        )

    assert process.terminated
    assert connection.closed


def test_cancelled_worker_is_force_stopped_after_the_grace_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection()
    process = _Process()
    cancellation = CancellationToken()
    cancellation.cancel()
    sent: list[dict[str, Any]] = []
    times = iter((0.0, executor._CANCEL_GRACE_SECONDS + 1))
    monkeypatch.setattr(executor.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(executor, "send_message", lambda _connection, message: sent.append(message))
    monkeypatch.setattr(executor, "_stop_worker", lambda worker: worker.terminate())

    with pytest.raises(ProcessingCancelledError):
        executor._receive_worker_result(
            connection,  # type: ignore[arg-type]
            process,  # type: ignore[arg-type]
            "job",
            {1},
            cancellation,
        )

    assert sent == [{"type": "cancel", "job_id": "job"}]
    assert process.terminated


def test_ocr_worker_is_force_stopped_after_the_total_execution_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection()
    process = _Process()
    monkeypatch.setattr(executor.time, "monotonic", lambda: 11.0)
    monkeypatch.setattr(executor, "_stop_worker", lambda worker: worker.terminate())

    with pytest.raises(ConversionError, match="tiempo máximo de ejecución"):
        executor._receive_worker_result(
            connection,  # type: ignore[arg-type]
            process,  # type: ignore[arg-type]
            "job",
            {1},
            None,
            execution_deadline=10.0,
        )

    assert process.terminated


def test_ocr_execution_deadline_scales_with_the_bounded_page_batch() -> None:
    assert executor._ocr_execution_timeout_seconds(1) == 180.0
    assert executor._ocr_execution_timeout_seconds(8) == 1_440.0
    assert executor._ocr_execution_timeout_seconds(100) == 1_800.0


def test_start_worker_process_builds_an_isolated_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    sentinel = _Process()

    def popen(command: list[str], **kwargs: Any) -> _Process:
        captured["command"] = command
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(executor.subprocess, "Popen", popen)
    monkeypatch.setattr(executor.sys, "frozen", False, raising=False)
    result = executor._start_worker_process("address", "family", b"a" * 32)

    assert result is sentinel
    assert captured["command"][1:3] == ["-m", "parsezen.ocr_worker"]
    assert captured["stdin"] is subprocess.DEVNULL
    assert "PARSEZEN_OCR_AUTH" in captured["env"]

    monkeypatch.setattr(executor.sys, "frozen", True)
    executor._start_worker_process("address", "family", b"b" * 32)
    assert captured["command"][1] == "--ocr-worker"


class _Listener:
    def __init__(self, result: object) -> None:
        self.result = result

    def accept(self) -> object:
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def test_accept_worker_handles_success_failure_exit_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection()
    assert (
        executor._accept_worker(
            _Listener(connection),  # type: ignore[arg-type]
            _Process(),  # type: ignore[arg-type]
            None,
        )
        is connection
    )
    with pytest.raises(ConversionError, match="canal privado"):
        executor._accept_worker(
            _Listener(OSError("closed")),  # type: ignore[arg-type]
            _Process(),  # type: ignore[arg-type]
            None,
        )
    with pytest.raises(ConversionError, match="iniciarse"):
        executor._accept_worker(
            _Listener(connection),  # type: ignore[arg-type]
            _Process(1),  # type: ignore[arg-type]
            None,
        )

    class _BlockingListener:
        def accept(self) -> object:
            threading.Event().wait(1)
            return connection

    monkeypatch.setattr(executor, "_STARTUP_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(executor, "_POLL_SECONDS", 0)
    with pytest.raises(ConversionError, match="demasiado"):
        executor._accept_worker(
            _BlockingListener(),  # type: ignore[arg-type]
            _Process(),  # type: ignore[arg-type]
            None,
        )


def test_worker_diagnostics_are_strictly_allowlisted() -> None:
    valid = {
        "error_type": "RuntimeError",
        "module": "ocr.engine",
        "function": "run",
        "line": 0,
    }
    assert executor._validated_worker_diagnostic(valid) == (
        "RuntimeError",
        "ocr.engine",
        "run",
        0,
    )
    for invalid in (None, {**valid, "path": "secret"}, {**valid, "line": True}):
        with pytest.raises(OcrProtocolError, match="diagnóstico"):
            executor._validated_worker_diagnostic(invalid)


def test_handshake_and_deadline_helpers_handle_success_exit_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_messages(monkeypatch)
    connection = _Connection([{"type": "hello", "version": PROTOCOL_VERSION}])
    executor._expect_hello(
        connection,  # type: ignore[arg-type]
        _Process(),  # type: ignore[arg-type]
        None,
    )
    with pytest.raises(OcrProtocolError, match="incompatible"):
        executor._expect_hello(
            _Connection([{"type": "hello", "version": -1}]),  # type: ignore[arg-type]
            _Process(),  # type: ignore[arg-type]
            None,
        )
    with pytest.raises(EOFError):
        executor._receive_until_deadline(
            _Connection(),  # type: ignore[arg-type]
            _Process(1),  # type: ignore[arg-type]
            None,
        )
    monkeypatch.setattr(executor, "_STARTUP_TIMEOUT_SECONDS", 0)
    with pytest.raises(ConversionError, match="demasiado"):
        executor._receive_until_deadline(
            _Connection(),  # type: ignore[arg-type]
            _Process(),  # type: ignore[arg-type]
            None,
        )


def test_process_lifecycle_handles_nonzero_timeout_and_forced_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ConversionError, match="inesperada"):
        executor._wait_for_clean_exit(_Process(2))  # type: ignore[arg-type]

    timed_out = _Process()

    def wait_timeout(timeout: float) -> int:
        del timeout
        if not timed_out.killed:
            raise subprocess.TimeoutExpired("worker", 1)
        return -9

    timed_out.wait = wait_timeout  # type: ignore[method-assign]
    with pytest.raises(ConversionError, match="cerrarse"):
        executor._wait_for_clean_exit(timed_out)  # type: ignore[arg-type]
    assert timed_out.terminated

    forced = _Process()

    def stop_timeout(timeout: float) -> int:
        del timeout
        if not forced.killed:
            raise subprocess.TimeoutExpired("worker", 1)
        return -9

    forced.wait = stop_timeout  # type: ignore[method-assign]
    executor._stop_worker(forced)  # type: ignore[arg-type]
    assert forced.terminated and forced.killed


def test_cancelled_token_is_checked_before_starting(tmp_path: Path) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"pdf")
    cancellation = CancellationToken()
    cancellation.cancel()

    with pytest.raises(ProcessingCancelledError):
        executor.run_ocr_worker(source, {1}, cancellation)
