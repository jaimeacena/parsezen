from __future__ import annotations

from typing import Any

import httpx

import parsezen.local_ai_adapters as adapters


def test_product_lfm_alias_uses_raw_chatml_and_strips_completed_reasoning(
    monkeypatch: Any,
) -> None:
    captured: dict[str, object] = {}

    def raw(
        _client: httpx.Client,
        model: str,
        context_window: int,
        prompt: str,
        _cancellation: object,
        **options: object,
    ) -> str:
        captured.update(
            model=model,
            context_window=context_window,
            prompt=prompt,
            options=options,
        )
        return "<think>extra private reasoning</think>Final text."

    monkeypatch.setattr(adapters, "request_local_ai_raw", raw)
    client = httpx.Client()
    try:
        result = adapters.request_adapted_local_ai(
            client,
            "parsezen/lfm-review:Q6_K",
            8192,
            "Transform exactly.",
            "Synthetic fragment 51.",
            None,
            prediction_characters=90,
            operation="review_content",
        )
    finally:
        client.close()

    assert result == "Final text."
    assert captured["model"] == "parsezen/lfm-review:Q6_K"
    assert captured["context_window"] == 8192
    prompt = captured["prompt"]
    assert isinstance(prompt, str)
    assert prompt.startswith("<|startoftext|><|im_start|>system\n")
    assert "Synthetic fragment 51.<|im_end|>" in prompt
    assert prompt.endswith("<|im_start|>assistant\n")
    assert "<think>" not in prompt
    options = captured["options"]
    assert isinstance(options, dict)
    assert options["prediction_characters"] > 90


def test_unknown_model_keeps_established_chat_transport(monkeypatch: Any) -> None:
    captured: dict[str, object] = {}

    def chat(*args: object, **options: object) -> str:
        captured["args"] = args
        captured["options"] = options
        return "unchanged"

    monkeypatch.setattr(adapters, "request_local_ai", chat)
    client = httpx.Client()
    try:
        result = adapters.request_adapted_local_ai(
            client,
            "qwen3:4b-instruct",
            4096,
            "Instructions",
            "Fragment",
            None,
            json_response=True,
            operation="translation",
        )
    finally:
        client.close()

    assert result == "unchanged"
    assert captured["args"] == (
        client,
        "qwen3:4b-instruct",
        4096,
        "Instructions",
        "Fragment",
        None,
    )
    options = captured["options"]
    assert isinstance(options, dict)
    assert options["json_response"] is True


def test_only_product_specialized_alias_is_explicitly_released(monkeypatch: Any) -> None:
    released: list[str] = []
    monkeypatch.setattr(
        adapters,
        "release_local_ai_model",
        lambda _client, model: released.append(model),
    )
    client = httpx.Client()
    try:
        assert adapters.release_adapted_local_ai_model(client, "qwen3.5:9b") is False
        assert (
            adapters.release_adapted_local_ai_model(
                client,
                "parsezen/lfm-review:Q6_K",
            )
            is True
        )
        assert adapters.release_adapted_local_ai_model(client, "parsezen/lfm-review:Q8_0") is False
        assert (
            adapters.release_adapted_local_ai_model(
                client,
                "parsezen/hymt-translation:Q4_K_M",
            )
            is True
        )
    finally:
        client.close()

    assert released == [
        "parsezen/lfm-review:Q6_K",
        "parsezen/hymt-translation:Q4_K_M",
    ]


def test_unapproved_specialized_alias_keeps_established_chat_transport(
    monkeypatch: Any,
) -> None:
    captured: dict[str, object] = {}

    def chat(*args: object, **options: object) -> str:
        captured["args"] = args
        captured["options"] = options
        return "unchanged"

    monkeypatch.setattr(adapters, "request_local_ai", chat)
    client = httpx.Client()
    try:
        result = adapters.request_adapted_local_ai(
            client,
            "parsezen/hymt-translation:tampered",
            8192,
            "These generic instructions are deliberately ignored.",
            "The report is ready.",
            None,
            source_language_code="en",
            target_language_code="es",
            prediction_characters=64,
            operation="translate",
        )
    finally:
        client.close()

    assert result == "unchanged"
    assert captured["args"][1] == "parsezen/hymt-translation:tampered"
    options = captured["options"]
    assert isinstance(options, dict)
    assert options["prediction_characters"] == 64


def test_product_hymt_alias_uses_official_raw_translation_prompt(monkeypatch: Any) -> None:
    captured: dict[str, object] = {}

    def raw(
        _client: httpx.Client,
        model: str,
        context_window: int,
        prompt: str,
        _cancellation: object,
        **options: object,
    ) -> str:
        captured.update(model=model, context_window=context_window, prompt=prompt, options=options)
        return "El informe está listo."

    monkeypatch.setattr(adapters, "request_local_ai_raw", raw)
    client = httpx.Client()
    try:
        result = adapters.request_adapted_local_ai(
            client,
            "parsezen/hymt-translation:Q4_K_M",
            8192,
            "These generic instructions are deliberately ignored.",
            "The report is ready.",
            None,
            source_language_code="en",
            target_language_code="es",
            prediction_characters=64,
            operation="translate",
        )
    finally:
        client.close()

    assert result == "El informe está listo."
    assert captured["prompt"] == (
        "<|startoftext|>Translate the following text into Spanish. Note that you should only "
        "output the translated result without any additional explanation:\nThe report is ready."
        "<|extra_0|>"
    )
    options = captured["options"]
    assert isinstance(options, dict)
    assert options["temperature"] == 0.7
    assert options["top_p"] == 0.6
    assert options["top_k"] == 20


def test_lfm_adapter_removes_only_added_heading_before_line_marker(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(
        adapters,
        "request_local_ai_raw",
        lambda *_args, **_kwargs: "# ZPZDOCAXZQ HEADING\n\nKeep # ZPZDOCBXZQ inline.",
    )
    client = httpx.Client()
    try:
        result = adapters.request_adapted_local_ai(
            client,
            "parsezen/lfm-review:Q6_K",
            8192,
            "Review.",
            "ZPZDOCAXZQ HEADING\n\nKeep ZPZDOCBXZQ inline.",
            None,
        )
    finally:
        client.close()

    assert result == "ZPZDOCAXZQ HEADING\n\nKeep # ZPZDOCBXZQ inline."


def test_lfm_adapter_discards_reasoning_and_one_outer_markdown_fence(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(
        adapters,
        "request_local_ai_raw",
        lambda *_args, **_kwargs: (
            "<think>synthetic reasoning</think>```markdown\nTexto corregido.\n```"
        ),
    )
    client = httpx.Client()
    try:
        result = adapters.request_adapted_local_ai(
            client,
            "parsezen/lfm-review:Q6_K",
            8192,
            "Review.",
            "Texto corregido.",
            None,
        )
    finally:
        client.close()

    assert result == "Texto corregido."


def test_lfm_adapter_extracts_complete_final_json_value(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        adapters,
        "request_local_ai_raw",
        lambda *_args, **_kwargs: (
            "<think>synthetic reasoning</think>La respuesta final es:\n"
            '[{"old":"cerrado","new":"listo"}]\nFin.'
        ),
    )
    client = httpx.Client()
    try:
        result = adapters.request_adapted_local_ai(
            client,
            "parsezen/lfm-review:Q6_K",
            8192,
            "Review bilingual content.",
            "Synthetic fragment.",
            None,
            operation="translation_review",
        )
    finally:
        client.close()

    assert result == '[{"old":"cerrado","new":"listo"}]'
