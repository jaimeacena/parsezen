from __future__ import annotations

import threading
from multiprocessing import Pipe

import pytest

import parsezen.offline_translation_worker as worker_module
from parsezen.errors import ProcessingCancelledError, TranslationError
from parsezen.offline_translation_protocol import (
    PROTOCOL_VERSION,
    receive_message,
    send_message,
)
from parsezen.offline_translation_worker import serve_connection


def _request(job_id: str, markdown: str = "Enough local text to translate.") -> dict[str, object]:
    return {
        "type": "run",
        "version": PROTOCOL_VERSION,
        "job_id": job_id,
        "markdown": markdown,
        "target_language": "es",
        "source_language_code": "en",
    }


def test_worker_reuses_one_process_for_serial_translation_requests() -> None:
    parent, worker = Pipe(duplex=True)
    exit_codes: list[int] = []
    translated: list[str] = []

    def translate(markdown: str, _target: str, *, on_progress, on_engine_ready, **_kwargs) -> str:
        translated.append(markdown)
        on_engine_ready()
        on_progress(1, 1)
        return f"ES:{markdown}"

    thread = threading.Thread(
        target=lambda: exit_codes.append(
            serve_connection(worker, translator=translate, exit_on_parent_loss=False)
        )
    )
    thread.start()
    assert receive_message(parent) == {"type": "hello", "version": PROTOCOL_VERSION}

    for index in range(2):
        job_id = f"{index + 1:032x}"
        source = f"Document {index + 1} with enough text."
        send_message(parent, _request(job_id, source))
        messages: list[dict[str, object]] = []
        while not messages or messages[-1]["type"] != "result":
            messages.append(receive_message(parent))
        assert messages == [
            {"type": "ready", "job_id": job_id},
            {"type": "progress", "job_id": job_id, "current": 1, "total": 1},
            {"type": "result", "job_id": job_id, "markdown": f"ES:{source}"},
        ]

    send_message(parent, {"type": "close", "version": PROTOCOL_VERSION})
    thread.join(timeout=3)
    parent.close()
    worker.close()
    assert not thread.is_alive()
    assert exit_codes == [0]
    assert translated == [
        "Document 1 with enough text.",
        "Document 2 with enough text.",
    ]


def test_worker_receives_cancellation_during_translation() -> None:
    parent, worker = Pipe(duplex=True)
    running = threading.Event()
    exit_codes: list[int] = []

    def translate(_markdown: str, _target: str, *, cancellation, on_engine_ready, **_kwargs) -> str:
        on_engine_ready()
        running.set()
        assert cancellation._event.wait(timeout=3)
        raise ProcessingCancelledError

    thread = threading.Thread(
        target=lambda: exit_codes.append(
            serve_connection(worker, translator=translate, exit_on_parent_loss=False)
        )
    )
    thread.start()
    assert receive_message(parent)["type"] == "hello"
    job_id = "a" * 32
    send_message(parent, _request(job_id))
    assert running.wait(timeout=3)
    send_message(parent, {"type": "cancel", "job_id": job_id})

    final_message: dict[str, object] = {}
    while final_message.get("type") != "cancelled":
        final_message = receive_message(parent)
    send_message(parent, {"type": "close", "version": PROTOCOL_VERSION})
    thread.join(timeout=3)
    parent.close()
    worker.close()
    assert not thread.is_alive()
    assert exit_codes == [0]
    assert final_message == {"type": "cancelled", "job_id": job_id}


@pytest.mark.parametrize(
    ("raised", "code", "message"),
    [
        (TranslationError("No language package"), "translation_failed", "No language package"),
        (RuntimeError("private details"), "unexpected", "offline no pudo completar"),
    ],
)
def test_worker_returns_safe_structured_engine_errors(
    raised: Exception,
    code: str,
    message: str,
) -> None:
    parent, worker = Pipe(duplex=True)
    exit_codes: list[int] = []

    def translate(*_args, **_kwargs) -> str:
        raise raised

    thread = threading.Thread(
        target=lambda: exit_codes.append(
            serve_connection(worker, translator=translate, exit_on_parent_loss=False)
        )
    )
    thread.start()
    assert receive_message(parent)["type"] == "hello"
    job_id = "b" * 32
    send_message(parent, _request(job_id))
    response = receive_message(parent)
    assert response["type"] == "error"
    assert response["code"] == code
    assert message in response["message"]
    send_message(parent, {"type": "close", "version": PROTOCOL_VERSION})
    thread.join(timeout=3)
    parent.close()
    worker.close()
    assert exit_codes == [0]


def test_worker_rejects_invalid_requests_and_missing_parent_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, worker = Pipe(duplex=True)
    result: list[int] = []
    thread = threading.Thread(
        target=lambda: result.append(
            serve_connection(worker, translator=lambda *_args: "unused", exit_on_parent_loss=False)
        )
    )
    thread.start()
    assert receive_message(parent)["type"] == "hello"
    send_message(parent, {"type": "invalid"})
    thread.join(timeout=3)
    parent.close()
    worker.close()
    assert result == [2]

    monkeypatch.delenv("PARSEZEN_TRANSLATION_AUTH", raising=False)
    assert worker_module.main(["--address", "unused", "--family", "AF_PIPE"]) == 2
    monkeypatch.setenv("PARSEZEN_TRANSLATION_AUTH", "dGlueQ==")
    assert worker_module.main(["--address", "unused", "--family", "AF_PIPE"]) == 2


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "invalid", "version": PROTOCOL_VERSION},
        _request("short"),
        _request("c" * 32, "\0"),
        {**_request("d" * 32), "target_language": ""},
        {**_request("e" * 32), "source_language_code": 5},
    ],
)
def test_request_validation_rejects_unsafe_protocol_values(
    payload: dict[str, object],
) -> None:
    with pytest.raises(worker_module.OfflineTranslationProtocolError):
        worker_module._validated_request(payload)
