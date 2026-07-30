from __future__ import annotations

import re
from dataclasses import dataclass

import pytest

import parsezen.offline_translation as translation_module
from parsezen.errors import TranslationError
from parsezen.offline_translation import (
    _translate_markdown_offline_in_process as translate_markdown_offline,
)
from parsezen.offline_translation import (
    detect_source_language,
    resolve_target_language,
)


def test_translates_only_text_and_preserves_markdown_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (
        "# Hello 2026\n\n"
        "Read [the guide](https://example.com/docs/10) and keep `value = 3`.\n\n"
        "| Name | Value |\n"
        "| --- | ---: |\n"
        "| Parsezen | 42 |\n\n"
        "```python\nprint('Hello 99')\n```\n"
    )
    monkeypatch.setattr(translation_module, "detect_source_language", lambda _text: "en")
    monkeypatch.setattr(
        translation_module,
        "_get_or_install_translator",
        lambda _source, _target: lambda text: f"ES:{text}",
    )

    translated = translate_markdown_offline(source, "Español")

    assert translated == (
        "# ES:Hello 2026\n\n"
        "ES:Read [the guide](https://example.com/docs/10) and keep `value = 3`.\n\n"
        "| ES:Name | Value |\n"
        "| --- | ---: |\n"
        "| ES:Parsezen | 42 |\n\n"
        "```python\nprint('Hello 99')\n```\n"
    )


def test_reports_translation_progress_without_exposing_document_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress: list[tuple[int, int]] = []
    translations = {
        "First paragraph with enough text.": "Primer párrafo con suficiente texto.",
        "Second paragraph with more text.": "Segundo párrafo con más texto.",
    }
    monkeypatch.setattr(translation_module, "detect_source_language", lambda _text: "en")
    monkeypatch.setattr(
        translation_module,
        "_get_or_install_translator",
        lambda _source, _target: translations.__getitem__,
    )

    result = translate_markdown_offline(
        "First paragraph with enough text.\n\nSecond paragraph with more text.",
        "Español",
        on_progress=lambda current, total: progress.append((current, total)),
    )

    assert result == "Primer párrafo con suficiente texto.\n\nSegundo párrafo con más texto."
    assert progress == [(1, 2), (2, 2)]


def test_skips_translation_when_document_is_already_in_target_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(translation_module, "detect_source_language", lambda _text: "es")
    monkeypatch.setattr(
        translation_module,
        "_get_or_install_translator",
        lambda *_args: pytest.fail("No language package should be loaded"),
    )

    source = "Este documento ya está escrito completamente en español."
    assert translate_markdown_offline(source, "Español") == source


@pytest.mark.parametrize(
    ("language", "code"),
    [("Español", "es"), ("INGLÉS", "en"), ("pt", "pt")],
)
def test_resolves_supported_languages(language: str, code: str) -> None:
    assert resolve_target_language(language) == code


def test_detects_common_document_languages_deterministically() -> None:
    assert (
        detect_source_language(
            "This is a sufficiently long English document about books, tables and careful editing."
        )
        == "en"
    )
    assert (
        detect_source_language(
            "Este es un documento suficientemente largo en español sobre libros y traducción."
        )
        == "es"
    )


def test_rejects_too_little_text_for_automatic_detection() -> None:
    with pytest.raises(TranslationError, match="suficiente texto"):
        detect_source_language("Hello")


def test_explains_translation_engine_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(translation_module, "detect_source_language", lambda _text: "en")

    def fail(_text: str) -> str:
        raise RuntimeError("engine failure")

    monkeypatch.setattr(
        translation_module,
        "_get_or_install_translator",
        lambda _source, _target: fail,
    )

    with pytest.raises(TranslationError, match="no pudo traducir"):
        translate_markdown_offline(
            "This document contains enough English text for language detection.",
            "Español",
        )


def test_decodes_html_entities_introduced_only_by_the_translation_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "This sufficiently long sentence uses an ampersand & a quoted balance sheet."
    monkeypatch.setattr(translation_module, "detect_source_language", lambda _text: "en")
    monkeypatch.setattr(
        translation_module,
        "_get_or_install_translator",
        lambda _source, _target: lambda _text: "Una hoja de &quot;balance&quot; &amp; gastos.",
    )

    translated = translate_markdown_offline(source, "Español")

    assert translated == 'Una hoja de "balance" & gastos.'


def test_translates_one_markdown_line_with_shared_sentence_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str] = []
    marker = translation_module.TRANSLATION_SEGMENT_MARKER

    def translate(text: str) -> str:
        requests.append(text)
        return f"El 14 de marzo de 2026, Elena pagó $1,250.50 para {marker} tres servicios"

    monkeypatch.setattr(translation_module, "detect_source_language", lambda _text: "en")
    monkeypatch.setattr(
        translation_module,
        "_get_or_install_translator",
        lambda _source, _target: translate,
    )

    result = translate_markdown_offline(
        "On March 14, 2026, Elena paid $1,250.50 for **three services**.\n",
        "es",
    )

    assert result == "El 14 de marzo de 2026, Elena pagó $1,250.50 para **tres servicios**.\n"
    assert len(requests) == 1
    assert "14, 2026" in requests[0]
    assert requests[0].count(marker) == 1


def test_falls_back_safely_when_an_argos_package_changes_the_group_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str] = []

    def translate(text: str) -> str:
        requests.append(text)
        if translation_module.TRANSLATION_SEGMENT_MARKER in text:
            return text.replace(translation_module.TRANSLATION_SEGMENT_MARKER, "MARCADOR_CAMBIADO")
        return {"Read": "Lee", "the guide": "la guía"}.get(text, text)

    monkeypatch.setattr(translation_module, "detect_source_language", lambda _text: "en")
    monkeypatch.setattr(
        translation_module,
        "_get_or_install_translator",
        lambda _source, _target: translate,
    )

    result = translate_markdown_offline("Read [the guide](https://example.com).", "es")

    assert result == "Lee [la guía](https://example.com)."
    assert len(requests) == 3


def test_falls_back_safely_when_argos_moves_the_group_marker_to_an_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str] = []
    marker = translation_module.TRANSLATION_SEGMENT_MARKER

    def translate(text: str) -> str:
        requests.append(text)
        if marker in text:
            return f"Lee el capítulo siguiente de {marker}"
        return {"Read the": "Lee el", "next chapter": "capítulo siguiente"}.get(text, text)

    monkeypatch.setattr(translation_module, "detect_source_language", lambda _text: "en")
    monkeypatch.setattr(
        translation_module,
        "_get_or_install_translator",
        lambda _source, _target: translate,
    )

    result = translate_markdown_offline("Read the [next chapter](chapter2.xhtml).", "es")

    assert result == "Lee el [capítulo siguiente](chapter2.xhtml)."
    assert len(requests) == 3


def test_retries_only_the_affected_line_when_grouping_drops_a_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = translation_module.TRANSLATION_SEGMENT_MARKER
    requests: list[str] = []

    def translate(text: str) -> str:
        requests.append(text)
        if marker in text:
            return f"Cómo vive {marker} tu nombre {marker} cada día?"
        return {
            "How does 1,000,000 per year": "Cómo es 1,000,000 al año",
            "your name": "tu nombre",
            "think each day?": "piensa cada día?",
        }[text]

    monkeypatch.setattr(translation_module, "detect_source_language", lambda _text: "en")
    monkeypatch.setattr(
        translation_module,
        "_get_or_install_translator",
        lambda _source, _target: translate,
    )

    result = translate_markdown_offline(
        "How does 1,000,000 per year {your name} think each day?\n",
        "es",
    )

    assert result == "Cómo es 1,000,000 al año {tu nombre} piensa cada día?\n"
    assert len(requests) == 4


def test_retries_with_identity_markers_when_argos_reorders_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str] = []

    def translate(text: str) -> str:
        requests.append(text)
        if "PZNUMBERTOKEN" in text:
            first, second = re.findall(r"PZNUMBERTOKEN[A-Z]+ENDPZ", text)
            return f"Bob tiene {second} peras. Alice tiene {first} manzanas."
        return "Bob tiene 20 peras. Alice tiene 10 manzanas."

    monkeypatch.setattr(translation_module, "detect_source_language", lambda _text: "en")
    monkeypatch.setattr(
        translation_module,
        "_get_or_install_translator",
        lambda _source, _target: translate,
    )

    result = translate_markdown_offline(
        "Alice has 10 apples. Bob has 20 pears.",
        "es",
    )

    assert result == "Bob tiene 20 peras. Alice tiene 10 manzanas."
    assert len(requests) == 2


def test_rejects_an_offline_result_left_in_the_source_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (
        "This deliberately long English paragraph contains enough natural language to verify "
        "that a failed translation is never published as if it had been translated correctly. "
        "Every sentence remains in English and therefore must be rejected by the final guard."
    )
    monkeypatch.setattr(translation_module, "detect_source_language", lambda _text: "en")
    monkeypatch.setattr(
        translation_module,
        "_get_or_install_translator",
        lambda _source, _target: lambda text: text,
    )

    with pytest.raises(TranslationError, match="idioma solicitado"):
        translate_markdown_offline(source, "es")


@pytest.mark.parametrize(
    ("source", "engine_output", "expected"),
    [
        ("### Journal Prompts:", "indicaciones del diario:", "### Indicaciones del diario:"),
        ("PROMISE TO YO' SELF", "prometerte a ti mismo", "PROMETERTE A TI MISMO"),
    ],
)
def test_retries_unchanged_titles_with_normalized_case(
    source: str,
    engine_output: str,
    expected: str,
) -> None:
    requests: list[str] = []

    def translate(text: str) -> str:
        requests.append(text)
        return engine_output

    result = translation_module._translate_title_case_normalized(source, translate)

    assert result == expected
    assert requests == [source.lstrip("# ").casefold()]


def test_retries_a_partially_translated_title_from_its_complete_source() -> None:
    result = translation_module._retry_unchanged_titles(
        "30 DAY MILLIONAIRE CHALLENGE",
        "30 días MILLIONAIRE CHALLENGE",
        "en",
        lambda text: "30 días reto millonario" if text == "30 day millionaire challenge" else text,
    )

    assert result == "30 DÍAS RETO MILLONARIO"


def test_explicit_source_language_allows_a_short_focused_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        translation_module,
        "detect_source_language",
        lambda _text: pytest.fail("El idioma original ya se conoce."),
    )
    monkeypatch.setattr(
        translation_module,
        "_get_or_install_translator",
        lambda source, target: (
            (lambda text: "Párrafo original 30." if text == "Original paragraph 30." else text)
            if (source, target) == ("en", "es")
            else pytest.fail("Par de idiomas inesperado.")
        ),
    )

    result = translate_markdown_offline(
        "Original paragraph 30.",
        "es",
        source_language_code="en",
    )

    assert result == "Párrafo original 30."


def test_retries_an_unchanged_numbered_sentence_around_its_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def translate(text: str) -> str:
        if text == "Original paragraph 30.":
            return text
        if text == "Original paragraph":
            return "Párrafo original"
        return text

    monkeypatch.setattr(
        translation_module,
        "_get_or_install_translator",
        lambda _source, _target: translate,
    )

    result = translate_markdown_offline(
        "Original paragraph 30.",
        "es",
        source_language_code="en",
    )

    assert result == "Párrafo original 30."


@dataclass
class _Package:
    from_code: str
    to_code: str


def test_uses_a_direct_package_when_available() -> None:
    packages = [_Package("fr", "es"), _Package("fr", "en"), _Package("en", "es")]

    assert translation_module._required_package_pairs("fr", "es", packages) == (("fr", "es"),)


def test_uses_english_as_a_pivot_when_no_direct_package_exists() -> None:
    packages = [_Package("fr", "en"), _Package("en", "es")]

    assert translation_module._required_package_pairs("fr", "es", packages) == (
        ("fr", "en"),
        ("en", "es"),
    )


def test_forces_the_minisbd_segmenter_before_loading_argos() -> None:
    translation_module._configure_safe_argos()

    import argostranslate.settings as argos_settings

    assert argos_settings.chunk_type is argos_settings.ChunkType.MINISBD
