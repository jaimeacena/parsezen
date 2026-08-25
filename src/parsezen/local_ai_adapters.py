"""Small model-family adapters over Parsezen's fixed local Ollama transport."""

from __future__ import annotations

import json
import re
from collections.abc import Callable

import httpx

from parsezen.cancellation import CancellationToken
from parsezen.component_catalog import REVIEW_MODEL_NAME, TRANSLATION_MODEL_NAME
from parsezen.errors import ImprovementError
from parsezen.local_ai_transport import (
    LocalAiMetrics,
    release_local_ai_model,
    request_local_ai,
    request_local_ai_raw,
)

_LFM_REASONING_OVERHEAD_CHARACTERS = 3_072
_LFM_THINKING_PATTERN = re.compile(r"\A\s*<think>[\s\S]*?</think>\s*", re.IGNORECASE)
_PROTECTED_LINE_TOKEN_PATTERN = re.compile(r"^(Z*PZDOC[A-Z]+XZQ)(?:\s|$)")
_MILMMT_LANGUAGE_NAMES = {
    "de": "German",
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "it": "Italian",
    "pt": "Portuguese",
    "zh": "Chinese (Simplified)",
    "zt": "Chinese (Traditional)",
}


def request_adapted_local_ai(
    client: httpx.Client,
    model: str,
    context_window: int,
    instructions: str,
    document_fragment: str,
    cancellation: CancellationToken | None,
    *,
    prediction_characters: int | None = None,
    max_generation_seconds: float | None = None,
    image: bytes | None = None,
    json_response: bool = False,
    operation: str = "other",
    source_language_code: str | None = None,
    target_language_code: str | None = None,
    on_metrics: Callable[[LocalAiMetrics], None] | None = None,
) -> str:
    """Use an exact adapter only for product-owned specialized model aliases.

    LFM2.5 is a reasoning model. Its normal chat endpoint separates or truncates
    that reasoning too early for short transformations. Parsezen therefore owns
    the complete raw ChatML prompt, reserves bounded reasoning headroom and then
    discards the completed ``<think>`` prefix before deterministic validation.
    Unknown models retain the established Ollama chat contract unchanged.
    """

    normalized_model = model.casefold()
    if normalized_model == TRANSLATION_MODEL_NAME.casefold() and image is None:
        target_name = _MILMMT_LANGUAGE_NAMES.get((target_language_code or "").casefold())
        if target_name is None:
            raise ImprovementError("Hy-MT2 necesita un idioma de destino compatible.")
        if json_response:
            raise ImprovementError("Hy-MT2 no admite respuestas JSON agrupadas.")
        response = request_local_ai_raw(
            client,
            model,
            context_window,
            _hymt_translation_prompt(target_name, document_fragment),
            cancellation,
            prediction_characters=prediction_characters,
            max_generation_seconds=max_generation_seconds,
            operation=operation,
            on_metrics=on_metrics,
            temperature=0.7,
            top_p=0.6,
            top_k=20,
        )
        return _strip_outer_markdown_fence(response).strip()
    if normalized_model == REVIEW_MODEL_NAME.casefold() and image is None:
        expected_characters = (
            len(document_fragment) if prediction_characters is None else prediction_characters
        )
        response = request_local_ai_raw(
            client,
            model,
            context_window,
            _lfm_review_prompt(instructions, document_fragment),
            cancellation,
            prediction_characters=expected_characters + _LFM_REASONING_OVERHEAD_CHARACTERS,
            max_generation_seconds=max_generation_seconds,
            operation=operation,
            on_metrics=on_metrics,
        )
        normalized = _reconcile_lfm_protected_line_prefixes(
            document_fragment,
            _strip_outer_markdown_fence(_strip_lfm_reasoning(response)),
        )
        needs_json = json_response or operation in {"translation_review", "translation_repair"}
        return _extract_json_response(normalized) if needs_json else normalized
    return request_local_ai(
        client,
        model,
        context_window,
        instructions,
        document_fragment,
        cancellation,
        prediction_characters=prediction_characters,
        max_generation_seconds=max_generation_seconds,
        image=image,
        json_response=json_response,
        operation=operation,
        on_metrics=on_metrics,
    )


def release_adapted_local_ai_model(client: httpx.Client, model: str) -> bool:
    """Unload a specialized phase model; generic models keep their legacy lifecycle."""

    normalized_model = model.casefold()
    if normalized_model not in {
        REVIEW_MODEL_NAME.casefold(),
        TRANSLATION_MODEL_NAME.casefold(),
    }:
        return False
    release_local_ai_model(client, model)
    return True


def _lfm_review_prompt(instructions: str, document_fragment: str) -> str:
    return (
        "<|startoftext|><|im_start|>system\n"
        f"{instructions}<|im_end|>\n"
        "<|im_start|>user\n"
        f"{document_fragment}<|im_end|>\n"
        "<|im_start|>assistant\n"
    )


def _hymt_translation_prompt(target_name: str, document_fragment: str) -> str:
    return (
        f"<|startoftext|>Translate the following text into {target_name}. "
        "Note that you should only output "
        "the translated result without any additional explanation:\n"
        f"{document_fragment}<|extra_0|>"
    )


def _strip_lfm_reasoning(response: str) -> str:
    """Discard a defensive reasoning prefix if the model emits another one."""

    return _LFM_THINKING_PATTERN.sub("", response, count=1)


def _strip_outer_markdown_fence(response: str) -> str:
    match = re.fullmatch(
        r"\s*```(?:markdown|md)?[ \t]*\r?\n([\s\S]*?)\r?\n```\s*",
        response,
        flags=re.IGNORECASE,
    )
    return match.group(1) if match is not None else response


def _extract_json_response(response: str) -> str:
    """Keep one complete final JSON value outside model commentary or fences."""

    decoder = json.JSONDecoder()
    candidates: list[tuple[int, int]] = []
    for index, character in enumerate(response):
        if character not in "[{":
            continue
        try:
            value, end = decoder.raw_decode(response, index)
        except json.JSONDecodeError:
            continue
        if isinstance(value, (list, dict)):
            candidates.append((index, end))
    if not candidates:
        return response
    start, end = max(candidates, key=lambda bounds: (bounds[1] - bounds[0], -bounds[0]))
    return response[start:end]


def _reconcile_lfm_protected_line_prefixes(source: str, response: str) -> str:
    """Remove a Markdown prefix LFM adds before an opaque line-leading marker."""

    line_tokens = {
        match.group(1)
        for line in source.splitlines()
        if (match := _PROTECTED_LINE_TOKEN_PATTERN.match(line)) is not None
    }
    reconciled = response
    for token in line_tokens:
        reconciled = re.sub(
            rf"(?m)^(?:[ \t]{{0,3}}#{{1,6}}[ \t]+){re.escape(token)}(?=\s|$)",
            token,
            reconciled,
        )
    return reconciled
