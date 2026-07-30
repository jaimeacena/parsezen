from __future__ import annotations

from collections import deque
from pathlib import Path

import pytest

import parsezen.ocr_executor as executor_module
import parsezen.ocr_protocol as protocol_module
from parsezen.cancellation import CancellationToken
from parsezen.errors import ConversionError, ProcessingCancelledError
from parsezen.ocr_protocol import (
    PROTOCOL_VERSION,
    OcrProtocolError,
    normalized_page_numbers,
    receive_message,
    send_message,
)


class _FakeConnection:
    def __init__(self, messages: list[dict[str, object]] | None = None) -> None:
        self._messages = deque(messages or [])
        self.sent: list[dict[str, object]] = []

    def poll(self, _timeout: float) -> bool:
        return bool(self._messages)

    def recv_bytes(self, _maxlength: int) -> bytes:
        message = self._messages.popleft()
        import json

        return json.dumps(message).encode("utf-8")

    def send_bytes(self, payload: bytes) -> None:
        import json

        self.sent.append(json.loads(payload.decode("utf-8")))


class _FakeProcess:
    def __init__(self) -> None:
        self.exit_code: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True
        self.exit_code = 1

    def wait(self, timeout: float) -> int:
        assert timeout > 0
        assert self.exit_code is not None
        return self.exit_code

    def kill(self) -> None:
        self.exit_code = 1


def test_protocol_round_trips_bounded_json_without_pickle() -> None:
    connection = _FakeConnection()
    send_message(connection, {"type": "hello", "version": PROTOCOL_VERSION})

    reader = _FakeConnection(connection.sent)
    assert receive_message(reader) == {"type": "hello", "version": PROTOCOL_VERSION}


def test_protocol_rejects_an_oversized_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(protocol_module, "MAX_MESSAGE_BYTES", 20)

    with pytest.raises(OcrProtocolError, match="límite"):
        send_message(_FakeConnection(), {"type": "page", "markdown": "x" * 30})


@pytest.mark.parametrize(
    "value",
    [[0], [-1], [True], [1.5], ["1"], None],
)
def test_protocol_rejects_invalid_page_numbers(value: object) -> None:
    with pytest.raises(OcrProtocolError, match="páginas válidas"):
        normalized_page_numbers(value, field_name="page_numbers")


def test_parent_accepts_only_expected_page_results() -> None:
    job_id = "a" * 32
    connection = _FakeConnection(
        [
            {"type": "stage", "job_id": job_id, "stage": "preparing"},
            {"type": "stage", "job_id": job_id, "stage": "running"},
            {"type": "page", "job_id": job_id, "page_number": 2, "markdown": ""},
            {"type": "complete", "job_id": job_id},
        ]
    )

    page_results: list[tuple[int, str]] = []
    result = executor_module._receive_worker_result(
        connection,
        _FakeProcess(),  # type: ignore[arg-type]
        job_id,
        {2},
        None,
        on_page_result=lambda page, markdown: page_results.append((page, markdown)),
    )

    assert result == {2: ""}
    assert page_results == [(2, "")]


def test_parent_reports_only_valid_monotonic_ocr_progress() -> None:
    job_id = "p" * 32
    connection = _FakeConnection(
        [
            {"type": "stage", "job_id": job_id, "stage": "preparing"},
            {"type": "stage", "job_id": job_id, "stage": "running"},
            {"type": "progress", "job_id": job_id, "current": 1, "total": 2},
            {"type": "progress", "job_id": job_id, "current": 2, "total": 2},
            {"type": "complete", "job_id": job_id},
        ]
    )
    progress: list[tuple[int, int]] = []

    result = executor_module._receive_worker_result(
        connection,
        _FakeProcess(),  # type: ignore[arg-type]
        job_id,
        {1, 2},
        None,
        on_progress=lambda current, total: progress.append((current, total)),
    )

    assert result == {}
    assert progress == [(1, 2), (2, 2)]


def test_parent_rejects_non_monotonic_ocr_progress() -> None:
    job_id = "q" * 32
    connection = _FakeConnection(
        [
            {"type": "stage", "job_id": job_id, "stage": "preparing"},
            {"type": "stage", "job_id": job_id, "stage": "running"},
            {"type": "progress", "job_id": job_id, "current": 1, "total": 2},
            {"type": "progress", "job_id": job_id, "current": 1, "total": 2},
        ]
    )

    with pytest.raises(OcrProtocolError, match="progreso no válido"):
        executor_module._receive_worker_result(
            connection,
            _FakeProcess(),  # type: ignore[arg-type]
            job_id,
            {1, 2},
            None,
        )


def test_parent_rejects_a_page_outside_the_requested_selection() -> None:
    job_id = "c" * 32
    connection = _FakeConnection(
        [
            {"type": "stage", "job_id": job_id, "stage": "preparing"},
            {"type": "stage", "job_id": job_id, "stage": "running"},
            {"type": "page", "job_id": job_id, "page_number": 3, "markdown": "Tres"},
        ]
    )

    with pytest.raises(OcrProtocolError, match="página no válida"):
        executor_module._receive_worker_result(
            connection,
            _FakeProcess(),  # type: ignore[arg-type]
            job_id,
            {2},
            None,
        )


def test_parent_rejects_an_out_of_order_worker_stage() -> None:
    job_id = "d" * 32
    connection = _FakeConnection([{"type": "stage", "job_id": job_id, "stage": "running"}])

    with pytest.raises(OcrProtocolError, match="etapa no válida"):
        executor_module._receive_worker_result(
            connection,
            _FakeProcess(),  # type: ignore[arg-type]
            job_id,
            {1},
            None,
        )


def test_parent_rejects_an_unknown_worker_error_code() -> None:
    job_id = "e" * 32
    connection = _FakeConnection(
        [
            {
                "type": "error",
                "job_id": job_id,
                "code": "arbitrary",
                "message": "Mensaje inyectado.",
            }
        ]
    )

    with pytest.raises(OcrProtocolError, match="error no válido"):
        executor_module._receive_worker_result(
            connection,
            _FakeProcess(),  # type: ignore[arg-type]
            job_id,
            {1},
            None,
        )


def test_parent_logs_only_validated_worker_diagnostics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    job_id = "f" * 32
    connection = _FakeConnection(
        [
            {
                "type": "error",
                "job_id": job_id,
                "code": "conversion_failed",
                "message": "Mensaje seguro para la interfaz.",
                "diagnostic": {
                    "error_type": "ModuleNotFoundError",
                    "module": "docling.models.plugins.defaults",
                    "function": "ocr_engines",
                    "line": 3,
                },
            }
        ]
    )

    with (
        caplog.at_level("WARNING"),
        pytest.raises(ConversionError, match="Mensaje seguro"),
    ):
        executor_module._receive_worker_result(
            connection,
            _FakeProcess(),  # type: ignore[arg-type]
            job_id,
            {1},
            None,
        )

    assert "ModuleNotFoundError" in caplog.text
    assert "docling.models.plugins.defaults" in caplog.text


def test_parent_rejects_untrusted_worker_diagnostic_text() -> None:
    job_id = "g" * 32
    connection = _FakeConnection(
        [
            {
                "type": "error",
                "job_id": job_id,
                "code": "conversion_failed",
                "message": "Mensaje seguro.",
                "diagnostic": {
                    "error_type": "RuntimeError: private text",
                    "module": "worker",
                    "function": "run",
                    "line": 1,
                },
            }
        ]
    )

    with pytest.raises(OcrProtocolError, match="diagnóstico no válido"):
        executor_module._receive_worker_result(
            connection,
            _FakeProcess(),  # type: ignore[arg-type]
            job_id,
            {1},
            None,
        )


def test_parent_hard_stops_a_running_worker_that_ignores_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = "b" * 32
    cancellation = CancellationToken()
    cancellation.cancel()
    connection = _FakeConnection(
        [
            {"type": "stage", "job_id": job_id, "stage": "preparing"},
            {"type": "stage", "job_id": job_id, "stage": "running"},
        ]
    )
    process = _FakeProcess()
    monkeypatch.setattr(executor_module, "_CANCEL_GRACE_SECONDS", 0.0)

    with pytest.raises(ProcessingCancelledError):
        executor_module._receive_worker_result(
            connection,
            process,  # type: ignore[arg-type]
            job_id,
            {1},
            cancellation,
        )

    assert process.terminated
    assert connection.sent == [{"type": "cancel", "job_id": job_id}]


def test_worker_command_never_contains_the_document_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Process:
        pass

    def start(command: list[str], **kwargs: object) -> Process:
        captured["command"] = command
        captured["environment"] = kwargs["env"]
        return Process()

    monkeypatch.setattr(executor_module.subprocess, "Popen", start)
    private_document = tmp_path / "cliente-secreto.pdf"

    executor_module._start_worker_process("private-address", "AF_PIPE", b"x" * 32)

    command = captured["command"]
    assert isinstance(command, list)
    assert str(private_document) not in command
    assert executor_module._AUTH_ENVIRONMENT_VARIABLE in captured["environment"]


def test_parent_keeps_unicode_pdf_copy_until_worker_scope_finishes(tmp_path: Path) -> None:
    source = tmp_path / "gestión.pdf"
    source.write_bytes(b"%PDF-local")

    with executor_module._worker_source_path(source) as worker_source:
        temporary_path = worker_source
        assert worker_source.read_bytes() == b"%PDF-local"
        str(worker_source).encode("ascii")
        assert source.read_bytes() == b"%PDF-local"

    assert not temporary_path.exists()
    assert source.read_bytes() == b"%PDF-local"


def test_real_worker_process_completes_the_private_handshake() -> None:
    auth_key = b"h" * 32
    process = None
    connection = None
    with executor_module._private_listener(auth_key) as (listener, address, family):
        try:
            process = executor_module._start_worker_process(address, family, auth_key)
            connection = executor_module._accept_worker(listener, process, None)
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
                executor_module._stop_worker(process)
