"""Replay small, anonymized model behaviours that previously threatened a workflow."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from parsezen.improvement import ImprovementMode, improve_markdown
from parsezen.settings import AppSettings

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ai_regressions.json"
_SETTINGS = AppSettings(model="regression-model", context_window=4_096)


def _load_cases() -> tuple[dict[str, object], ...]:
    payload = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    cases = payload["cases"]
    assert isinstance(cases, list) and cases
    return tuple(cases)


@pytest.mark.parametrize("case", _load_cases(), ids=lambda case: str(case["name"]))
def test_anonymized_model_response_regressions(case: dict[str, object]) -> None:
    source = case["source"]
    expected = case["expected"]
    raw_responses = case["responses"]
    assert isinstance(source, str)
    assert isinstance(expected, str)
    assert isinstance(raw_responses, list) and all(
        isinstance(response, str) for response in raw_responses
    )
    responses = iter(raw_responses)
    request_count = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        try:
            content = next(responses)
        except StopIteration:
            pytest.fail("The regression made more model requests than its fixture allows.")
        return httpx.Response(200, json={"message": {"content": content}})

    result = improve_markdown(
        source,
        ImprovementMode(str(case["mode"])),
        _SETTINGS,
        transport=httpx.MockTransport(respond),
    )

    assert result == expected
    assert request_count == len(raw_responses)
