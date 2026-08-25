from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest

import parsezen.local_ai_transport as transport_module
from parsezen.cancellation import CancellationToken
from parsezen.errors import ImprovementError, ProcessingCancelledError
from parsezen.improvement_contracts import MAX_LOCAL_AI_OUTPUT_CHARACTERS
from parsezen.processing_metrics import capture_batch_telemetry


class _PeriodicStream(httpx.SyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = chunks

    def __iter__(self) -> Iterator[bytes]:
        yield from self._chunks


def _transport_with_periodic_chunks(*chunks: bytes) -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda _request: httpx.Response(200, stream=_PeriodicStream(tuple(chunks)))
    )


def test_prediction_limit_scales_with_the_fragment_and_context() -> None:
    assert transport_module.prediction_token_limit(5, 8_192) == 130
    assert transport_module.prediction_token_limit(3_000, 8_192) == 1_128
    assert transport_module.prediction_token_limit(20_000, 8_192) == 2_048
    assert transport_module.prediction_token_limit(20_000, 2_048) == 1_024


def test_active_stream_can_outlive_the_configured_idle_timeout_within_total_deadline() -> None:
    transport = _transport_with_periodic_chunks(
        b'{"message":{"content":"respuesta "}}\n',
        b'{"message":{"content":"completa"}}\n',
    )

    with httpx.Client(timeout=httpx.Timeout(1), transport=transport) as client:
        result = transport_module.request_local_ai(
            client,
            "parsezen-local",
            8_192,
            "Return the content.",
            "Content",
            None,
        )

    assert result == "respuesta completa"


def test_stream_has_a_default_total_deadline_derived_from_the_read_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter((0.0, 601.0))
    monkeypatch.setattr(transport_module, "monotonic", clock.__next__)
    transport = _transport_with_periodic_chunks(
        b'{"message":{"content":"partial"}}\n',
    )

    with httpx.Client(timeout=httpx.Timeout(120), transport=transport) as client:
        with pytest.raises(ImprovementError, match="máximo total de generación"):
            transport_module.request_local_ai(
                client,
                "parsezen-local",
                8_192,
                "Return the content.",
                "Content",
                None,
            )


def test_stream_fails_only_after_explicit_total_generation_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter((0.0, 31.0))
    monkeypatch.setattr(transport_module, "monotonic", clock.__next__)
    transport = _transport_with_periodic_chunks(
        b'{"message":{"content":"partial"}}\n',
    )

    with httpx.Client(timeout=httpx.Timeout(30), transport=transport) as client:
        with pytest.raises(ImprovementError, match="máximo total de generación"):
            transport_module.request_local_ai(
                client,
                "parsezen-local",
                8_192,
                "Return the content.",
                "Content",
                None,
                max_generation_seconds=30,
            )


def test_stream_reports_only_privacy_safe_local_inference_metrics() -> None:
    transport = _transport_with_periodic_chunks(
        b'{"message":{"content":"respuesta"},"done":false}\n',
        b'{"message":{"content":""},"done":true,"prompt_eval_count":12,'
        b'"eval_count":4,"total_duration":2500000000,"load_duration":500000000,'
        b'"eval_duration":2000000000}\n',
    )
    metrics = []

    with httpx.Client(timeout=httpx.Timeout(120), transport=transport) as client:
        result = transport_module.request_local_ai(
            client,
            "parsezen-local",
            8_192,
            "Return the content.",
            "Private content that must not enter metrics.",
            None,
            on_metrics=metrics.append,
        )

    assert result == "respuesta"
    assert metrics == [
        transport_module.LocalAiMetrics(
            wall_duration_ms=metrics[0].wall_duration_ms,
            prompt_tokens=12,
            output_tokens=4,
            ollama_total_duration_ms=2_500,
            ollama_load_duration_ms=500,
            output_tokens_per_second=2.0,
        )
    ]


def test_chat_request_keeps_its_existing_payload_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            stream=_PeriodicStream((b'{"message":{"content":"ok"},"done":true}\n',)),
        )

    with httpx.Client(timeout=httpx.Timeout(120), transport=httpx.MockTransport(handler)) as client:
        result = transport_module.request_local_ai(
            client,
            "parsezen-local",
            8_192,
            "Return the content.",
            "Private content",
            None,
            image=b"\x00\xff",
            json_response=True,
        )

    assert result == "ok"
    assert len(requests) == 1
    assert requests[0].url.path == "/api/chat"
    request_json = json.loads(requests[0].read())
    assert request_json["model"] == "parsezen-local"
    assert request_json["messages"] == [
        {"role": "system", "content": "Return the content."},
        {"role": "user", "content": "Private content", "images": ["AP8="]},
    ]
    assert request_json["stream"] is True
    assert request_json["think"] is False
    assert request_json["format"] == "json"
    assert "raw" not in request_json
    assert "keep_alive" not in request_json


def test_raw_generation_uses_the_generate_contract_and_reports_metrics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            stream=_PeriodicStream(
                (
                    b'{"response":"respuesta ","done":false}\n',
                    b'{"response":"raw","done":true,"prompt_eval_count":12,'
                    b'"eval_count":4,"total_duration":2500000000,"load_duration":500000000,'
                    b'"eval_duration":2000000000}\n',
                )
            ),
        )

    metrics = []
    private_prompt = "PRIVATE RAW PROMPT"
    with httpx.Client(timeout=httpx.Timeout(120), transport=httpx.MockTransport(handler)) as client:
        with capture_batch_telemetry() as telemetry:
            result = transport_module.request_local_ai_raw(
                client,
                "specialized-translator:latest",
                8_192,
                private_prompt,
                None,
                on_metrics=metrics.append,
                operation="translation_raw",
                temperature=0.7,
                top_p=0.6,
                top_k=20,
            )

    assert result == "respuesta raw"
    assert len(requests) == 1
    assert requests[0].url.path == "/api/generate"
    request_json = json.loads(requests[0].read())
    assert request_json["model"] == "specialized-translator:latest"
    assert request_json["prompt"] == private_prompt
    assert request_json["stream"] is True
    assert request_json["raw"] is True
    assert request_json["keep_alive"] == 300
    assert request_json["options"]["num_ctx"] == 8_192
    assert request_json["options"]["temperature"] == 0.7
    assert request_json["options"]["top_p"] == 0.6
    assert request_json["options"]["top_k"] == 20
    assert metrics[0].prompt_tokens == 12
    assert metrics[0].output_tokens == 4
    assert telemetry.snapshot().input_characters == len(private_prompt)
    assert private_prompt not in caplog.text


def test_raw_generation_honors_cancellation_between_stream_chunks() -> None:
    cancellation = CancellationToken()

    class _CancellingStream(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b'{"response":"partial","done":false}\n'
            cancellation.cancel()
            yield b'{"response":"never published","done":true}\n'

    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, stream=_CancellingStream())
    )
    with httpx.Client(timeout=httpx.Timeout(120), transport=transport) as client:
        with pytest.raises(ProcessingCancelledError, match="canceló"):
            transport_module.request_local_ai_raw(
                client,
                "specialized-translator:latest",
                8_192,
                "Prompt",
                cancellation,
            )


def test_raw_generation_rejects_an_oversized_streamed_response() -> None:
    oversized = "x" * (MAX_LOCAL_AI_OUTPUT_CHARACTERS + 1)
    transport = _transport_with_periodic_chunks(
        (f'{{"response":{json.dumps(oversized)},"done":false}}\n').encode()
    )
    with httpx.Client(timeout=httpx.Timeout(120), transport=transport) as client:
        with pytest.raises(ImprovementError, match="tamaño permitido"):
            transport_module.request_local_ai_raw(
                client,
                "specialized-translator:latest",
                8_192,
                "Prompt",
                None,
            )


def test_release_model_is_explicit_and_does_not_delete_weights() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"model": "specialized-translator:latest", "done": True})

    with httpx.Client(timeout=httpx.Timeout(120), transport=httpx.MockTransport(handler)) as client:
        transport_module.release_local_ai_model(client, "specialized-translator:latest")

    assert len(requests) == 1
    assert requests[0].url.path == "/api/generate"
    request_json = json.loads(requests[0].read())
    assert request_json == {
        "model": "specialized-translator:latest",
        "prompt": "",
        "stream": False,
        "keep_alive": 0,
    }
