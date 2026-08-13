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


def test_active_stream_can_outlive_the_configured_idle_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_total_deadline() -> float:
        pytest.fail("El timeout de lectura no debe convertirse en un deadline total")

    monkeypatch.setattr(transport_module, "monotonic", unexpected_total_deadline)
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
