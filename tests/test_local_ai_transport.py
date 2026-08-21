from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

import parsezen.local_ai_transport as transport_module
from parsezen.errors import ImprovementError


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
