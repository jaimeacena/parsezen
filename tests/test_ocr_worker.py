from __future__ import annotations

import threading
from multiprocessing import Pipe
from pathlib import Path

from parsezen.errors import ProcessingCancelledError
from parsezen.ocr_protocol import PROTOCOL_VERSION, receive_message, send_message
from parsezen.ocr_worker import serve_connection


def _request(source: Path, job_id: str) -> dict[str, object]:
    return {
        "type": "run",
        "version": PROTOCOL_VERSION,
        "job_id": job_id,
        "source_path": str(source),
        "page_numbers": [1, 2],
        "force_full_page_numbers": [2],
    }


def test_worker_serves_one_authenticated_contract_request(tmp_path: Path) -> None:
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-local")
    parent, worker = Pipe(duplex=True)
    exit_codes: list[int] = []

    def convert(
        received_source: Path,
        pages: set[int],
        *,
        cancellation,
        force_full_page_numbers: set[int],
        on_stage,
        on_progress,
    ) -> dict[int, str]:
        assert received_source == source
        assert pages == {1, 2}
        assert force_full_page_numbers == {2}
        assert not cancellation.is_cancelled
        on_stage("preparing")
        on_stage("running")
        on_progress(1, 2)
        on_progress(2, 2)
        return {1: "", 2: "Dos"}

    thread = threading.Thread(
        target=lambda: exit_codes.append(
            serve_connection(worker, converter=convert, exit_on_parent_loss=False)
        )
    )
    thread.start()
    assert receive_message(parent) == {"type": "hello", "version": PROTOCOL_VERSION}
    job_id = "a" * 32
    send_message(parent, _request(source, job_id))

    messages = []
    while not messages or messages[-1]["type"] != "complete":
        messages.append(receive_message(parent))

    thread.join(timeout=3)
    parent.close()
    worker.close()
    assert not thread.is_alive()
    assert exit_codes == [0]
    assert messages == [
        {"type": "stage", "job_id": job_id, "stage": "preparing"},
        {"type": "stage", "job_id": job_id, "stage": "running"},
        {"type": "progress", "job_id": job_id, "current": 1, "total": 2},
        {"type": "progress", "job_id": job_id, "current": 2, "total": 2},
        {"type": "page", "job_id": job_id, "page_number": 1, "markdown": ""},
        {"type": "page", "job_id": job_id, "page_number": 2, "markdown": "Dos"},
        {"type": "complete", "job_id": job_id},
    ]


def test_worker_receives_cancellation_while_ocr_is_active(tmp_path: Path) -> None:
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-local")
    parent, worker = Pipe(duplex=True)
    exit_codes: list[int] = []
    running = threading.Event()

    def convert(_source: Path, _pages: set[int], *, cancellation, on_stage, **_kwargs):
        on_stage("preparing")
        on_stage("running")
        running.set()
        assert cancellation._event.wait(timeout=3)
        raise ProcessingCancelledError

    thread = threading.Thread(
        target=lambda: exit_codes.append(
            serve_connection(worker, converter=convert, exit_on_parent_loss=False)
        )
    )
    thread.start()
    assert receive_message(parent)["type"] == "hello"
    job_id = "b" * 32
    send_message(parent, _request(source, job_id))
    assert running.wait(timeout=3)
    send_message(parent, {"type": "cancel", "job_id": job_id})

    final_message: dict[str, object] = {}
    while final_message.get("type") != "cancelled":
        final_message = receive_message(parent)

    thread.join(timeout=3)
    parent.close()
    worker.close()
    assert not thread.is_alive()
    assert exit_codes == [0]
    assert final_message == {"type": "cancelled", "job_id": job_id}


def test_worker_error_diagnostic_never_contains_exception_text(tmp_path: Path) -> None:
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-local")
    parent, worker = Pipe(duplex=True)
    private_sentinel = "PRIVATE-OCR-CONTENT"

    def fail(*_args, **_kwargs):
        raise RuntimeError(private_sentinel)

    thread = threading.Thread(
        target=lambda: serve_connection(worker, converter=fail, exit_on_parent_loss=False)
    )
    thread.start()
    assert receive_message(parent)["type"] == "hello"
    job_id = "c" * 32
    send_message(parent, _request(source, job_id))
    message = receive_message(parent)

    thread.join(timeout=3)
    parent.close()
    worker.close()
    assert message["type"] == "error"
    assert message["diagnostic"]["error_type"] == "RuntimeError"
    assert private_sentinel not in repr(message)
