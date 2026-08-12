from __future__ import annotations

from dataclasses import dataclass

import pytest

import parsezen.offline_translation as translation_module
import parsezen.offline_translation_executor as translation_executor
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


def test_preserves_roman_index_references_without_sending_them_to_argos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "SCORPIO III 186\nSAGITTARIUS I 192\n"
    requests: list[str] = []

    def translate(text: str) -> str:
        requests.append(text)
        return {
            "SCORPIO": "ESCORPIO",
            "SAGITTARIUS": "SAGITARIO",
        }.get(text, text)

    monkeypatch.setattr(translation_module, "detect_source_language", lambda _text: "en")
    monkeypatch.setattr(
        translation_module,
        "_get_or_install_translator",
        lambda _source, _target: translate,
    )

    translated = translate_markdown_offline(source, "Español")

    assert translated == "ESCORPIO III 186\nSAGITARIO I 192\n"
    assert all("III" not in request and request != "I" for request in requests)


def test_preserves_roman_part_number_in_a_heading_without_protecting_prose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "## Part II: The 36 Faces\n\nI describe the second part.\n"
    requests: list[str] = []

    def translate(text: str) -> str:
        requests.append(text)
        return (
            text.replace("Part", "Parte")
            .replace("The 36 Faces", "Las 36 caras")
            .replace(
                "I describe the second part.",
                "Describo la segunda parte.",
            )
        )

    monkeypatch.setattr(translation_module, "detect_source_language", lambda _text: "en")
    monkeypatch.setattr(
        translation_module,
        "_get_or_install_translator",
        lambda _source, _target: translate,
    )

    translated = translate_markdown_offline(source, "es")

    assert translated == "## Parte II: Las 36 caras\n\nDescribo la segunda parte.\n"
    assert all(request != "II" for request in requests)
    assert any(request.startswith("I describe") for request in requests)


def test_keeps_trailing_list_folios_outside_argos_and_in_their_original_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "- Appendices 258\n- [Tables of Correspondence 266](#tables)\n"
    requests: list[str] = []

    def translate(text: str) -> str:
        requests.append(text)
        return {
            "Appendices": "Apéndices",
            "Tables of Correspondence": "Tablas de correspondencia",
        }.get(text, text)

    monkeypatch.setattr(translation_module, "detect_source_language", lambda _text: "en")
    monkeypatch.setattr(
        translation_module,
        "_get_or_install_translator",
        lambda _source, _target: translate,
    )

    translated = translate_markdown_offline(source, "es")

    assert translated == ("- Apéndices 258\n- [Tablas de correspondencia 266](#tables)\n")
    assert all("258" not in request and "266" not in request for request in requests)


def test_protects_an_angle_wrapped_relative_destination_after_empty_image_alt() -> None:
    destination = "../private-assets/page-cover.png"
    source = f"![](<{destination}>)"

    parts = translation_module._plan_markdown_parts(source)

    assert "".join(part.text for part in parts) == source
    assert destination not in "".join(part.text for part in parts if part.should_translate)
    assert any(destination in part.text and not part.should_translate for part in parts)


def test_normalizes_a_lowercase_prose_start_before_the_first_translation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "lowercase prose must remain a paragraph instead of becoming a numbered list."
    requests: list[str] = []

    def translate(text: str) -> str:
        requests.append(text)
        if text.startswith("Lowercase"):
            return "La prosa en minúscula debe seguir siendo un párrafo y no una lista numerada."
        return "1. Resultado estructuralmente incorrecto."

    monkeypatch.setattr(
        translation_module,
        "_get_or_install_translator",
        lambda _source, _target: translate,
    )

    translated = translate_markdown_offline(source, "es", source_language_code="en")

    assert translated.startswith("La prosa")
    assert requests[0].startswith("Lowercase")
    assert not translation_module._starts_with_lowercase_prose("Ordinary prose starts normally.")


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


def test_large_offline_translation_resumes_only_validated_work_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "\n\n".join(
        f"English paragraph {index} contains enough local words for a bounded work item."
        for index in range(1, 13)
    )
    cache: dict[str, str] = {}
    calls: list[str] = []
    ready: list[bool] = []
    monkeypatch.setattr(
        translation_module,
        "MAX_OFFLINE_TRANSLATION_WORK_ITEM_CHARACTERS",
        180,
    )
    monkeypatch.setattr(translation_module, "detect_source_language", lambda _text: "en")
    monkeypatch.setattr(
        translation_module,
        "validate_translation_quality",
        lambda *_args, **_kwargs: None,
    )

    def translate(markdown: str, _language: str, **kwargs) -> str:
        calls.append(markdown)
        kwargs["on_engine_ready"]()
        return markdown.replace("English paragraph", "PÃ¡rrafo en espaÃ±ol")

    monkeypatch.setattr(translation_executor, "translate_markdown_in_worker", translate)

    first = translation_module.translate_markdown_offline(
        source,
        "es",
        on_engine_ready=lambda: ready.append(True),
        load_checkpoint=cache.get,
        save_checkpoint=lambda key, value: not cache.__setitem__(key, value),
    )
    first_calls = len(calls)
    calls.clear()
    second = translation_module.translate_markdown_offline(
        source,
        "es",
        load_checkpoint=cache.get,
        save_checkpoint=lambda _key, _value: pytest.fail("No debe reescribir checkpoints"),
    )

    assert first == second == source.replace("English paragraph", "PÃ¡rrafo en espaÃ±ol")
    assert first_calls > 1
    assert calls == []
    assert ready == [True]
    assert len(cache) == first_calls


def test_offline_work_items_never_split_a_fenced_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (
        "Introductory prose before the example.\n\n"
        "```text\n"
        f"{'untranslated code payload ' * 20}\n"
        "```\n\n"
        "Closing prose after the example.\n"
    )
    monkeypatch.setattr(
        translation_module,
        "MAX_OFFLINE_TRANSLATION_WORK_ITEM_CHARACTERS",
        80,
    )

    work_items = tuple(translation_module._offline_translation_work_items(source))

    assert "".join(work_items) == source
    assert all(item.count("```") in {0, 2} for item in work_items)


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


def test_retries_one_long_failed_part_in_smaller_safe_units() -> None:
    source = " ".join(
        "This resilient sentence keeps 12 numbered observations intact." for _index in range(48)
    )
    requests: list[int] = []

    def translate(text: str) -> str:
        requests.append(len(text))
        if len(text) > translation_module.MAX_TRANSLATION_RETRY_PART_CHARACTERS:
            raise TranslationError("The local engine rejected an oversized prose span.")
        return f"ES:{text}"

    translated = translation_module._translate_value_safely(translate, source)

    assert translated.replace("ES:", "") == source
    assert translated.count("ES:") > 1
    assert translated.count("12") == source.count("12")
    assert requests[0] == len(source)
    assert max(requests[1:]) <= translation_module.MAX_TRANSLATION_RETRY_PART_CHARACTERS


def test_retries_only_the_still_failing_subparts_at_smaller_levels() -> None:
    source = " ".join(
        "This resilient sentence keeps 24 numbered observations intact." for _index in range(36)
    )
    requests: list[int] = []

    def translate(text: str) -> str:
        requests.append(len(text))
        if len(text) > 24:
            raise TranslationError("The local engine needs a smaller guarded span.")
        return f"ES:{text}"

    translated = translation_module._translate_value_safely(translate, source)

    assert translated.replace("ES:", "") == source
    assert translated.count("24") == source.count("24")
    assert any(length > 700 for length in requests)
    assert any(350 < length <= 700 for length in requests)
    assert any(175 < length <= 350 for length in requests)
    assert any(90 < length <= 175 for length in requests)
    assert any(45 < length <= 90 for length in requests)
    assert any(24 < length <= 45 for length in requests)
    assert max(length for length in requests if length <= 24) <= 24


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
        first = translation_module._number_placeholder(0)
        second = translation_module._number_placeholder(1)
        if first in text:
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


def test_preserves_an_unchanged_title_after_the_single_local_retry() -> None:
    source = "## PLANETARY CONDITION"

    result = translation_module._retry_unchanged_titles(
        source,
        source,
        "en",
        lambda text: text,
    )

    assert result == source


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


def test_sentence_retry_never_sends_inline_epub_guards_to_argos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_guard = "`PZDOC_EPUB_XML_A_A_XZQ`"
    last_guard = "`PZDOC_EPUB_XML_A_B_XZQ`"
    source = f"{first_guard}Modern EPUB content.{last_guard}"
    attempts = 0

    def translate(text: str) -> str:
        nonlocal attempts
        assert "PZDOC_EPUB_XML" not in text
        if text == "Modern EPUB content.":
            attempts += 1
            return text if attempts == 1 else "Contenido EPUB moderno."
        return text

    monkeypatch.setattr(
        translation_module,
        "_get_or_install_translator",
        lambda _source, _target: translate,
    )

    result = translate_markdown_offline(source, "es", source_language_code="en")

    assert result == f"{first_guard}Contenido EPUB moderno.{last_guard}"


def test_title_retry_never_sends_comment_epub_guards_to_argos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_guard = "<!-- PZDOC_EPUB_XML_A_A_XZQ -->"
    last_guard = "<!-- PZDOC_EPUB_XML_A_B_XZQ -->"
    source = f"{first_guard}CHAPTER{last_guard}"

    def translate(text: str) -> str:
        assert "PZDOC_EPUB_XML" not in text
        if text == "chapter":
            return "capítulo"
        return text

    monkeypatch.setattr(
        translation_module,
        "_get_or_install_translator",
        lambda _source, _target: translate,
    )

    result = translate_markdown_offline(source, "es", source_language_code="en")

    assert result == f"{first_guard}CAPÍTULO{last_guard}"


def test_sentence_retry_normalizes_case_after_an_unchanged_argos_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "This ordinary source sentence remains untranslated by the first attempt."

    def translate(text: str) -> str:
        if text == source.casefold():
            return "esta frase ordinaria queda traducida tras el segundo intento."
        return text

    monkeypatch.setattr(
        translation_module,
        "_get_or_install_translator",
        lambda _source, _target: translate,
    )

    result = translate_markdown_offline(source, "es", source_language_code="en")

    assert result == "Esta frase ordinaria queda traducida tras el segundo intento."


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
