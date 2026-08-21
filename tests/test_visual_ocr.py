from __future__ import annotations

import json

import httpx
import pytest

import parsezen.visual_ocr as visual_ocr_module
from parsezen.local_models import OllamaModel
from parsezen.visual_ocr import LocalVisualTextArbiter, build_local_visual_text_arbiter


def test_selects_the_smallest_installed_vision_model_after_tags_discovery() -> None:
    models = (
        OllamaModel("text:4b", "Text 4B", size_bytes=2_500_000_000),
        OllamaModel("vision:4b", "Vision 4B", size_bytes=4_900_000_000),
        OllamaModel("vision:12b", "Vision 12B", size_bytes=8_500_000_000),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        capabilities = ["completion", "vision"] if model.startswith("vision") else ["completion"]
        return httpx.Response(200, json={"capabilities": capabilities})

    arbiter = build_local_visual_text_arbiter(
        "text:4b",
        90,
        model_loader=lambda: models,
        transport=httpx.MockTransport(handler),
    )

    assert arbiter is not None
    assert arbiter.model == "vision:4b"


def test_visual_arbiter_sends_only_the_bounded_crop_and_returns_literal_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def request(*_args: object, **kwargs: object) -> str:
        seen.update(kwargs)
        return '{"text":"The Horimæa 1135"}'

    monkeypatch.setattr(visual_ocr_module, "request_local_ai", request)
    arbiter = LocalVisualTextArbiter("vision:4b", 30)
    image = b"\x89PNG\r\n\x1a\n" + b"local-crop"

    result = arbiter(image, "The Horimiea 1135", "The Horim\ufffda 1135", None)

    assert result == "The Horimæa 1135"
    assert seen["image"] == image
    assert seen["json_response"] is True


def test_visual_arbiter_rejects_an_unbounded_or_non_image_payload() -> None:
    arbiter = LocalVisualTextArbiter("vision:4b", 30)

    with pytest.raises(Exception, match="recorte visual"):
        arbiter(b"not-an-image", "Native line", "OCR line", None)
