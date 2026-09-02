from __future__ import annotations

import hashlib
import json
import re

import httpx
import pytest

import parsezen.ai_markdown_safety as markdown_safety_module
import parsezen.improvement as improvement_module
import parsezen.local_ai_transport as transport_module
from parsezen.cancellation import CancellationToken
from parsezen.errors import (
    ImprovementError,
    LocalModelUnavailableError,
    ProcessingCancelledError,
    SettingsError,
)
from parsezen.improvement import (
    MAX_CONSERVED_VALUES_PER_CHUNK,
    MAX_DOCUMENT_CHARACTERS,
    MAX_INPUT_CHARACTERS,
    MAX_TRANSLATION_CHUNK_CHARACTERS,
    MAX_TRANSLATION_PROTECTED_VALUES_PER_CHUNK,
    NUMBER_PATTERN,
    ImprovementMode,
    build_instructions,
    improve_markdown,
    review_translation_markdown,
)
from parsezen.settings import AppSettings
from parsezen.translation_quality import (
    TranslationIssueKind,
    TranslationQualityIssue,
    TranslationQualityReport,
    build_translation_quality_report,
)

LOCAL_SETTINGS = AppSettings(
    model="parsezen-local",
    context_window=8_192,
    timeout_seconds=30,
)


def test_translation_localizes_copied_english_ordinal_and_era() -> None:
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )

    translated = markdown_safety_module._localize_copied_english_conventions(
        "Part II. The tomb dates to the early 3rd millennium BC.",
        "PART Ii. La tumba data de principios del 3rd milenio BC.",
        context,
    )

    assert translated == "PARTE II. La tumba data de principios del 3.º milenio a. C."


def test_translation_does_not_localize_ordinals_inside_urls_or_code() -> None:
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )
    source = "Use `3rd` and https://example.test/3rd before the 3rd attempt."

    translated = markdown_safety_module._localize_copied_english_conventions(
        source,
        "Usa `3rd` y https://example.test/3rd antes del 3rd intento.",
        context,
    )

    assert translated == "Usa `3rd` y https://example.test/3rd antes del 3.º intento."


def test_translation_localizes_copied_astrological_series_labels() -> None:
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )
    source = "Compare *Venus in Aries I* with *Sun in Taurus II* before continuing."
    proposed = "Compara *Venus in Aries I* con *Sun in Taurus II* antes de continuar."

    translated = markdown_safety_module._localize_copied_english_conventions(
        source,
        proposed,
        context,
    )

    assert translated == "Compara *Venus en Aries I* con *Sol en Tauro II* antes de continuar."


def test_translation_finishes_partially_localized_astrological_series_labels() -> None:
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )
    source = "Compare *Jupiter in Gemini III* with *Mars in Capricorn I*."
    proposed = "Compara *Jupiter in Géminis III* con *Mars in Capricornio I*."

    translated = markdown_safety_module._localize_copied_english_conventions(
        source,
        proposed,
        context,
    )

    assert translated == "Compara *Júpiter en Géminis III* con *Marte en Capricornio I*."


def test_translation_restores_a_split_emphasis_astrological_series_label() -> None:
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )
    source = "The paired image is *Sun in Aries II* and remains part of the same series."
    proposed = "La imagen emparejada es *Sun in Aries* II y sigue formando parte de la misma serie."

    translated = markdown_safety_module._localize_copied_english_conventions(
        source,
        proposed,
        context,
    )

    assert translated == (
        "La imagen emparejada es *Sol en Aries II* y sigue formando parte de la misma serie."
    )


def test_translation_treats_a_following_roman_as_part_of_an_astrological_series_label() -> None:
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )
    source = "The paired image is *Sun in Aries* II and remains part of the same series."
    proposed = "La imagen emparejada es *Sun in Aries* II y sigue formando parte de la misma serie."

    translated = markdown_safety_module._localize_copied_english_conventions(
        source,
        proposed,
        context,
    )

    assert translated == (
        "La imagen emparejada es *Sol en Aries II* y sigue formando parte de la misma serie."
    )


def test_translation_localizes_an_astrological_series_with_separate_roman_emphasis() -> None:
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )
    source = "<!-- PZDOC PDF PAGE 65 --> *Sun in Aries* **III** The Sun is strong here."

    translated = markdown_safety_module._localize_copied_english_conventions(
        source,
        "<!-- PZDOC PDF PAGE 65 --> *Sun in Aries* **III** El Sol es fuerte aquí.",
        context,
    )

    assert translated == (
        "<!-- PZDOC PDF PAGE 65 --> *Sol en Aries* **III** El Sol es fuerte aquí."
    )


def test_translation_localizes_a_leading_emphasized_astrological_placement() -> None:
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )
    source = "<!-- PZDOC PDF PAGE 152 --> *Venus in Virgo* n Venus is in her fall."

    translated = markdown_safety_module._localize_copied_english_conventions(
        source,
        "<!-- PZDOC PDF PAGE 152 --> *Venus in Virgo* en Venus está en caída.",
        context,
    )

    assert translated == ("<!-- PZDOC PDF PAGE 152 --> *Venus en Virgo* en Venus está en caída.")


def test_translation_keeps_unsequenced_astrological_or_literal_labels() -> None:
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )
    source = "Compare *Sun in Aries* with `Venus in Aries I` and the linked reference."
    proposed = (
        "Compara *Sun in Aries* con `Venus in Aries I` y https://example.test/Sun-in-Aries-II."
    )

    translated = markdown_safety_module._localize_copied_english_conventions(
        source,
        proposed,
        context,
    )

    assert translated == proposed


def test_translation_postprocessing_preserves_a_bare_email_address() -> None:
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )
    source = "Email mainstream@example.com when you need careful assistance."
    response = "Escribe a mainstream@example.com cuando necesites ayuda especializada."

    translated = markdown_safety_module._prepare_and_validate_response(
        source,
        response,
        context,
        ImprovementMode.TRANSLATE,
    )

    assert translated == response


def test_translation_validation_rejects_a_mutated_bare_email_address() -> None:
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )

    with pytest.raises(ImprovementError, match="correos electrónicos"):
        markdown_safety_module._prepare_and_validate_response(
            "Email author@example.com when you need careful assistance.",
            "Escribe a hacker@example.net cuando necesites ayuda especializada.",
            context,
            ImprovementMode.TRANSLATE,
        )


def test_translation_protects_a_complete_english_ordinal_before_localizing_it() -> None:
    source = "Plan your 13th trip."
    protected = improvement_module._protect_translation_values(
        source,
        protect_headings=False,
        protect_paragraphs=False,
    )

    assert {value.value for value in protected.values} == {"13th"}
    assert "13" not in protected.text
    assert "th" not in protected.text

    restored = improvement_module._restore_protected_values(
        protected.text.replace("Plan your", "Planifica tu").replace("trip", "viaje"),
        protected.values,
    )
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )

    assert (
        markdown_safety_module._localize_copied_english_conventions(
            source,
            restored,
            context,
        )
        == "Planifica tu 13.º viaje."
    )


def test_translation_preserves_braced_template_fields_exactly() -> None:
    source = "Write {your name} and {{account_id}} here."

    protected = improvement_module._protect_translation_values(
        source,
        protect_headings=False,
        protect_paragraphs=False,
    )

    assert {value.value for value in protected.values} == {
        "{your name}",
        "{{account_id}}",
    }
    assert improvement_module._restore_protected_values(protected.text, protected.values) == source


@pytest.mark.parametrize("paragraph", (False, True))
def test_restores_backslashes_in_protected_values_as_literal_text(paragraph: bool) -> None:
    token = "PZDOCSAFEVALUEXZQ"
    value = "C:\\Archive\\"
    protected = improvement_module._ProtectedValue(token, value, paragraph=paragraph)
    response = f"\n{token}\n" if paragraph else f"<!-- {token} -->"

    assert improvement_module._restore_protected_values(response, (protected,)) == value


def test_translation_protects_currency_without_mistaking_prose_for_a_formula() -> None:
    source = "Create a $50k/year salary and unlimited money $$$."

    protected = improvement_module._protect_translation_values(
        source,
        protect_headings=False,
        protect_paragraphs=False,
    )

    assert {value.value for value in protected.values} == {"$", "50", "$$$"}
    assert "salary and unlimited money" in protected.text
    assert improvement_module._restore_protected_values(protected.text, protected.values) == source


def test_translation_still_protects_inline_math_next_to_currency() -> None:
    source = "Use $x = 2$ before paying $50."

    protected = improvement_module._protect_translation_values(
        source,
        protect_headings=False,
        protect_paragraphs=False,
    )

    assert {value.value for value in protected.values} == {"$x = 2$", "$", "50"}
    assert improvement_module._restore_protected_values(protected.text, protected.values) == source


def test_translation_localizes_copied_established_terms_but_not_literal_values() -> None:
    source = (
        "PartOne says Taurus and Gemini remained important in the mainstream. "
        "See https://example.test/Taurus and `Gemini`."
    )
    proposed = (
        "ParteUno dice que Taurus y Gemini siguieron siendo importantes en el mainstream. "
        "Véase https://example.test/Taurus y `Gemini`."
    )
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )

    translated = markdown_safety_module._localize_copied_english_conventions(
        source,
        proposed,
        context,
    )

    assert translated == (
        "Parte uno dice que Tauro y Géminis siguieron siendo importantes en el ámbito general. "
        "Véase https://example.test/Taurus y `Gemini`."
    )


def test_quality_guided_review_selects_risky_neighbors_and_a_distributed_clean_sample() -> None:
    parts = tuple(
        improvement_module._TranslationReviewPart(
            f"Source paragraph identity{chr(97 + index)} with enough distinct context.",
            f"Párrafo traducido identidad{chr(97 + index)} con contexto suficientemente distinto.",
        )
        for index in range(21)
    )
    issue = TranslationQualityIssue(
        11,
        TranslationIssueKind.FIDELITY,
        "Revisar",
        parts[10].source,
        parts[10].translated,
        "risky-part",
    )
    report = TranslationQualityReport("en", "es", "es", 21, 1_000, 1_000, 1, (issue,))

    selected = improvement_module._quality_guided_translation_review_indexes(parts, report)

    assert selected == (0, 9, 10, 11, 20)


def test_quality_guided_review_falls_back_to_every_part_for_an_unanchored_issue() -> None:
    parts = tuple(
        improvement_module._TranslationReviewPart(
            f"Source {index} with complete context.",
            f"Destino {index} con contexto completo.",
        )
        for index in range(5)
    )
    issue = TranslationQualityIssue(
        1,
        TranslationIssueKind.SOURCE_TEXT,
        "Revisar",
        "Excerpt absent from every aligned part.",
        "Fragmento ausente de todas las partes alineadas.",
        "missing-part",
    )
    report = TranslationQualityReport("en", "es", "es", 5, 100, 100, 1, (issue,))

    assert improvement_module._quality_guided_translation_review_indexes(parts, report) == tuple(
        range(5)
    )


def test_quality_guided_review_prefers_stable_segments_over_a_stale_excerpt() -> None:
    parts = tuple(
        improvement_module._TranslationReviewPart(
            f"Source {index} with complete context.",
            f"Destino {index} con contexto completo.",
            frozenset({index + 1}),
        )
        for index in range(8)
    )
    issue = TranslationQualityIssue(
        1,
        TranslationIssueKind.SOURCE_TEXT,
        "Revisar",
        "Excerpt changed by a guarded repair.",
        "Extracto ya sustituido por una reparación segura.",
        "stale-excerpt",
    )
    report = TranslationQualityReport(
        "en",
        "es",
        "es",
        8,
        100,
        100,
        1,
        (issue,),
        review_segment_numbers=(4,),
    )

    assert improvement_module._quality_guided_translation_review_indexes(parts, report) == (
        0,
        2,
        3,
        4,
    )


def test_quality_guided_review_keeps_every_risky_pdf_segment_beyond_excerpt_cap() -> None:
    source = "\n\n".join(
        f"<!-- PZDOC PDF PAGE {page} -->\n\n"
        f"This original page {page} contains substantial English prose that remains untranslated."
        for page in range(1, 26)
    )
    report = build_translation_quality_report(
        source,
        source,
        source_language="en",
        target_language="es",
    )
    parts = improvement_module._plan_translation_review_parts(source, source)

    assert len(report.issues) == 20
    assert report.total_issues > len(report.issues)
    assert report.review_segment_numbers == tuple(range(1, 26))
    assert not report.requires_full_review
    assert improvement_module._quality_guided_translation_review_indexes(parts, report) == tuple(
        range(len(parts))
    )


def test_quality_guided_review_expands_when_a_hidden_issue_has_no_safe_position() -> None:
    parts = tuple(
        improvement_module._TranslationReviewPart(
            f"Source {index} with complete context.",
            f"Destino {index} con contexto completo.",
        )
        for index in range(4)
    )
    report = TranslationQualityReport(
        "en",
        "es",
        "es",
        4,
        100,
        100,
        21,
        (),
        requires_full_review=True,
    )

    assert improvement_module._quality_guided_translation_review_indexes(parts, report) == (
        0,
        1,
        2,
        3,
    )


def test_quality_guided_review_uses_prose_sample_instead_of_markdown_table() -> None:
    table = "| Topic | Meaning |\n| --- | --- |\n| House | Interpretation |\n"
    parts = (
        improvement_module._TranslationReviewPart(table, table, frozenset({1})),
        improvement_module._TranslationReviewPart(
            "A complete prose paragraph for comparison.\n",
            "Un párrafo completo para comparar.\n",
            frozenset({2}),
        ),
    )
    issue = TranslationQualityIssue(
        1,
        TranslationIssueKind.FIDELITY,
        "Revisar",
        "Topic Meaning House Interpretation",
        "Topic Meaning House Interpretation",
        "table-risk",
    )
    report = TranslationQualityReport(
        "en",
        "es",
        "es",
        2,
        100,
        100,
        1,
        (issue,),
        review_segment_numbers=(1,),
    )

    assert improvement_module._quality_guided_translation_review_indexes(parts, report) == (1,)


def test_translation_chunks_receive_a_bounded_hierarchical_book_context() -> None:
    parts = (
        improvement_module._MarkdownPart("# Book title\n", True),
        improvement_module._MarkdownPart("First paragraph.\n", True),
        improvement_module._MarkdownPart("## Inner section\n", True),
        improvement_module._MarkdownPart("Second paragraph.\n", True),
        improvement_module._MarkdownPart("# Appendix\n", True),
    )

    contexts = improvement_module._hierarchical_translation_contexts(parts)

    assert contexts == (
        "Book title",
        "Book title",
        "Book title > Inner section",
        "Book title > Inner section",
        "Appendix",
    )
    contextualized = improvement_module._instructions_with_hierarchical_context(
        "Translate faithfully.",
        contexts[3],
    )
    assert "solo para coherencia" in contextualized
    assert "Book title > Inner section" in contextualized


def test_bilingual_review_corrects_a_mistranslated_title_from_its_source() -> None:
    source = "# The History, Astrology and Magic of the Decans\n\nBy\n\nAustin Coppock\n"
    translated = (
        "# Historia del Ajedrez; Astrología y Magia de los Decanos\n\nKor\n\nAustin Coppock\n"
    )
    corrected = "# Historia, Astrología y Magia de los Decanos\n\nPor\n\nAustin Coppock\n"

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert source.strip() not in payload["messages"][0]["content"]
        assert source.strip() in payload["messages"][1]["content"]
        assert translated.splitlines()[0] in payload["messages"][1]["content"]
        corrections = [
            {
                "old": "Historia del Ajedrez; Astrología y Magia de los Decanos",
                "new": "Historia, Astrología y Magia de los Decanos",
            },
            {"old": "Kor", "new": "Por"},
        ]
        return httpx.Response(200, json={"message": {"content": json.dumps(corrections)}})

    result = review_translation_markdown(
        source,
        translated,
        LOCAL_SETTINGS,
        "es",
        transport=httpx.MockTransport(respond),
    )

    assert result == corrected


def test_bilingual_review_uses_only_closed_semantic_focus_instructions() -> None:
    source = "You should preserve the force of every claim.\n"
    translated = "Debe conservar la fuerza de cada afirmación.\n"

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        instructions = payload["messages"][0]["content"]
        assert "falsos sentidos" in instructions
        assert "grado formal, coloquial, vulgar" in instructions
        assert "calcos inequívocos" not in instructions
        return httpx.Response(200, json={"message": {"content": "[]"}})

    result = review_translation_markdown(
        source,
        translated,
        LOCAL_SETTINGS,
        "es",
        transport=httpx.MockTransport(respond),
        review_focus=frozenset(
            {
                improvement_module.TranslationReviewFocus.MEANING,
                improvement_module.TranslationReviewFocus.REGISTER,
            }
        ),
    )

    assert result == translated


def test_bilingual_review_rejects_free_text_as_semantic_focus() -> None:
    with pytest.raises(ImprovementError, match="foco de revisión semántica"):
        review_translation_markdown(
            "Source statement.\n",
            "Afirmación traducida.\n",
            LOCAL_SETTINGS,
            "es",
            review_focus=frozenset({"meaning from document"}),  # type: ignore[arg-type]
        )


def test_focused_bilingual_review_has_an_isolated_checkpoint_identity() -> None:
    part = improvement_module._TranslationReviewPart(
        "Source statement.",
        "Afirmación traducida.",
    )
    expected_default = hashlib.sha256(
        b"ollama-translation-review-v3\nSource statement.\n\0\nAfirmaci\xc3\xb3n traducida."
    ).hexdigest()

    default_key = improvement_module._translation_review_checkpoint_key(part)
    meaning_key = improvement_module._translation_review_checkpoint_key(
        part,
        review_focus=frozenset({improvement_module.TranslationReviewFocus.MEANING}),
    )
    register_key = improvement_module._translation_review_checkpoint_key(
        part,
        review_focus=frozenset({improvement_module.TranslationReviewFocus.REGISTER}),
    )

    assert default_key == expected_default
    assert len({default_key, meaning_key, register_key}) == 3


def test_bilingual_review_minimizes_verbose_patch_and_preserves_byline_layout() -> None:
    source = "By\nAustin Coppock\n"
    translated = "Kor\nAustin Coppock\n"

    def respond(_request: httpx.Request) -> httpx.Response:
        corrections = [
            {
                "old": "Kor\nAustin Coppock",
                "new": "Por Austin Coppock",
            }
        ]
        return httpx.Response(200, json={"message": {"content": json.dumps(corrections)}})

    result = review_translation_markdown(
        source,
        translated,
        LOCAL_SETTINGS,
        "es",
        transport=httpx.MockTransport(respond),
    )

    assert result == "Por\nAustin Coppock\n"


def test_bilingual_review_retranslates_a_title_after_a_partial_patch_duplicates_it() -> None:
    source = "# The History, Astrology and Magic of the Decans\n"
    translated = "# Historia del Ajedrez; Astrología y Magia de los Decanos\n"
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            content = json.dumps([{"old": "del Ajedrez", "new": "de los Decanos"}])
        else:
            content = "Historia, Astrología y Magia de los Decanos"
        return httpx.Response(200, json={"message": {"content": content}})

    result = review_translation_markdown(
        source,
        translated,
        LOCAL_SETTINGS,
        "es",
        transport=httpx.MockTransport(respond),
        priority_block_count=1,
    )

    assert result == "# Historia, Astrología y Magia de los Decanos\n"
    assert calls == 2


def test_bilingual_review_rechecks_unchanged_front_matter_in_a_focused_group() -> None:
    source = "# The History, Astrology and Magic of the Decans\n\nBy\nAustin Coppock\n"
    translated = (
        "# Historia del Ajedrez; Astrología y Magia de los Decanos\n\nKor\nAustin Coppock\n"
    )
    corrected = "# Historia, Astrología y Magia de los Decanos\n\nPor\nAustin Coppock\n"
    calls = 0
    progress: list[tuple[int, int]] = []

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        corrections: list[dict[str, str]] = []
        if calls == 2:
            corrections = [
                {
                    "old": "Historia del Ajedrez; Astrología y Magia de los Decanos",
                    "new": "Historia, Astrología y Magia de los Decanos",
                }
            ]
        elif calls == 3:
            corrections = [{"old": "Kor", "new": "Por"}]
        return httpx.Response(200, json={"message": {"content": json.dumps(corrections)}})

    result = review_translation_markdown(
        source,
        translated,
        LOCAL_SETTINGS,
        "es",
        transport=httpx.MockTransport(respond),
        priority_block_count=3,
        on_progress=lambda current, total: progress.append((current, total)),
    )

    assert result == corrected
    assert calls == 3
    assert progress == [(1, 4), (2, 4), (3, 4), (4, 4)]


def test_bilingual_review_runs_a_focused_second_pass_for_embedded_source_words() -> None:
    source = "The decans preserve a body of scholarship that remains important to this chapter.\n"
    translated = (
        "Los decans conservan un cuerpo de scholarship que sigue siendo importante "
        "para este capÃ­tulo.\n"
    )
    calls = 0
    progress: list[tuple[int, int]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        if calls == 1:
            content = "[]"
        else:
            assert "lista FOCUS" in payload["messages"][0]["content"]
            assert 'FOCUS=["decans", "scholarship"]' in payload["messages"][1]["content"]
            content = json.dumps(
                [
                    {"old": "decans", "new": "decanos"},
                    {"old": "scholarship", "new": "tradiciÃ³n acadÃ©mica"},
                ]
            )
        return httpx.Response(200, json={"message": {"content": content}})

    result = review_translation_markdown(
        source,
        translated,
        LOCAL_SETTINGS,
        "es",
        transport=httpx.MockTransport(respond),
        on_progress=lambda current, total: progress.append((current, total)),
    )

    assert "Los decanos" in result
    assert "tradiciÃ³n acadÃ©mica" in result
    assert calls == 2
    assert progress == [(1, 2), (2, 2)]


def test_residual_review_sends_only_each_aligned_line_with_residue() -> None:
    source = (
        "The decans preserve the first tradition.\nThe gaggles preserve a body of scholarship.\n"
    )
    translated = (
        "Los decans conservan la primera tradición.\n"
        "Los gaggles conservan un cuerpo de scholarship.\n"
    )
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        prompt = payload["messages"][1]["content"]
        if calls == 1:
            content = "[]"
        elif calls == 2:
            assert "decans" in prompt
            assert "gaggles" not in prompt
            assert 'FOCUS=["decans"]' in prompt
            content = json.dumps(
                [
                    {"old": "decans", "new": "decanos"},
                    {"old": "primera", "new": "inicial"},
                    {"old": "Los decans", "new": "Los decans tradicionales"},
                ]
            )
        elif calls == 3:
            assert "gaggles" in prompt
            assert "decans" not in prompt
            assert 'FOCUS=["gaggles", "scholarship"]' in prompt
            content = json.dumps(
                [
                    {"old": "gaggles", "new": "grupos"},
                    {"old": "scholarship", "new": "tradición académica"},
                ]
            )
        return httpx.Response(200, json={"message": {"content": content}})

    result = review_translation_markdown(
        source,
        translated,
        LOCAL_SETTINGS,
        "es",
        transport=httpx.MockTransport(respond),
    )

    assert result == (
        "Los decanos conservan la primera tradición.\n"
        "Los grupos conservan un cuerpo de tradición académica.\n"
    )
    assert calls == 3


def test_residual_review_prioritizes_a_rare_one_edit_variant() -> None:
    part = improvement_module._TranslationReviewPart(
        "Gods and Spirits of the 36 Airs of the Zodiac.\n",
        "Dioses y espÃ­ritus de los 36 Airees del ZodÃ­aco.\n",
    )

    selected = improvement_module._plan_residual_translation_review_part_indexes(
        (part,),
        "Aires " * 12 + part.translated,
        source_language="en",
        target_language="es",
    )

    assert selected == (0,)


def test_residual_review_prioritizes_the_final_report_source_text_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    noisy = improvement_module._TranslationReviewPart(
        "The decans scholarship grimoires gaggles minutiae traditions symbols planets spirits "
        "zodiac remain relevant.\n",
        "En la tradición siguen presentes zodiac spirits planets symbols traditions minutiae "
        "gaggles grimoires scholarship y decans.\n",
    )
    untranslated = improvement_module._TranslationReviewPart(
        "A short English sentence remains entirely untranslated.\n",
        "A short English sentence remains entirely untranslated.\n",
    )
    translated = ("decans scholarship grimoires gaggles minutiae " * 12) + noisy.translated
    monkeypatch.setattr(improvement_module, "MAX_RESIDUAL_TRANSLATION_REVIEW_PARTS", 1)

    selected = improvement_module._plan_residual_translation_review_part_indexes(
        (noisy, untranslated),
        translated,
        source_language="en",
        target_language="es",
    )

    assert selected == (1,)


def test_residual_review_uses_only_reported_source_text_prose_segments() -> None:
    table = "| Topic | Meaning |\n| --- | --- |\n| House | Interpretation |\n"
    prose = "A short English sentence remains entirely untranslated.\n"
    parts = (
        improvement_module._TranslationReviewPart(table, table, frozenset({1})),
        improvement_module._TranslationReviewPart(prose, prose, frozenset({2})),
    )
    issues = (
        TranslationQualityIssue(
            1,
            TranslationIssueKind.SOURCE_TEXT,
            "Revisar",
            "Topic Meaning House Interpretation",
            "Topic Meaning House Interpretation",
            "table-source-text",
        ),
        TranslationQualityIssue(
            2,
            TranslationIssueKind.SOURCE_TEXT,
            "Revisar",
            prose,
            prose,
            "prose-source-text",
        ),
    )
    report = TranslationQualityReport(
        "en",
        "es",
        "en",
        2,
        100,
        100,
        2,
        issues,
        review_segment_numbers=(1, 2),
    )

    selected = improvement_module._plan_residual_translation_review_part_indexes(
        parts,
        table + prose,
        source_language="en",
        target_language="es",
        quality_report=report,
    )

    assert selected == (1,)


def test_residual_review_does_not_chase_non_source_text_report_warnings() -> None:
    part = improvement_module._TranslationReviewPart(
        "An original sentence with a specialist term.\n",
        "Una frase traducida con specialist como término técnico.\n",
        frozenset({1}),
    )
    issue = TranslationQualityIssue(
        1,
        TranslationIssueKind.FIDELITY,
        "Revisar",
        part.source,
        part.translated,
        "fidelity-only",
    )
    report = TranslationQualityReport("en", "es", "es", 1, 50, 60, 1, (issue,))

    assert (
        improvement_module._plan_residual_translation_review_part_indexes(
            (part,),
            part.translated,
            source_language="en",
            target_language="es",
            quality_report=report,
        )
        == ()
    )


def test_residual_review_does_not_retranslate_a_reference_catalogue() -> None:
    catalogue = (
        "BIBLIOGRAPHY. Ada Author. The Complete Book of Stars, translated by Bea Editor, "
        "University Press. Carla Writer. Ancient Astronomy, edited by Dan Scholar, London "
        "Academic Press. Eva Researcher. The Planetary Journal, revised edition, Cambridge "
        "University Press."
    )
    part = improvement_module._TranslationReviewPart(catalogue, catalogue)

    units = improvement_module._exact_source_text_review_units(
        part,
        source_language="en",
    )

    assert units == ()


def test_residual_review_keeps_prose_repairable_after_an_index_heading() -> None:
    residual = "The following section explains how planetary conditions shape daily decisions."
    source = f"INDEX\n\n{residual}\n"
    part = improvement_module._TranslationReviewPart(source, source)

    units = improvement_module._exact_source_text_review_units(
        part,
        source_language="en",
    )

    assert len(units) == 1
    assert residual in units[0].source


def test_exact_retranslation_requires_positive_target_language_evidence() -> None:
    improvement_module._validate_exact_retranslation_target_language(
        "Esta frase completa está escrita claramente en español.",
        "es",
    )

    with pytest.raises(ImprovementError, match="idioma solicitado"):
        improvement_module._validate_exact_retranslation_target_language(
            "Zorble quaxen mivra plestun drovaki senfar ulmato krivens.",
            "es",
        )


def test_exact_retranslation_rejects_a_fluent_third_language_response() -> None:
    source = "This complete English sentence remains entirely untranslated."
    part = improvement_module._TranslationReviewPart(source, source)
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": json.dumps(
                        {
                            "translation": (
                                "Cette phrase anglaise complète reste entièrement non traduite."
                            )
                        }
                    )
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        candidate, cacheable = improvement_module._retranslate_exact_source_text_unit(
            client,
            "local-model",
            4096,
            part,
            source_language="en",
            target_language="es",
            translated_markdown=source,
            cancellation=None,
        )

    assert candidate == source
    assert cacheable
    assert calls == 2


def test_exact_source_units_include_a_long_heading_on_a_mixed_page() -> None:
    heading = (
        "THE COMPLETE PRACTICAL GUIDE TO PLANETARY CONDITIONS AND THEIR MANY EFFECTS ON "
        "EVERY IMPORTANT DECISION THROUGHOUT ORDINARY DAILY LIFE AND WORK"
    )
    part = improvement_module._TranslationReviewPart(
        f"{heading}\n\nThis paragraph explains the first consequence.",
        f"{heading}\n\nEste párrafo explica la primera consecuencia.",
    )

    units = improvement_module._exact_source_text_review_units(
        part,
        source_language="en",
    )

    assert len(units) == 1
    assert units[0].source == heading


def test_exact_source_offsets_survive_expansive_unicode_casefolding() -> None:
    residual = "Dieser vollständige deutsche Satz bleibt unverändert erhalten."
    prefix = "La Straße es larga. "
    source = f"{prefix}{residual} Fin."
    part = improvement_module._TranslationReviewPart(source, source)

    units = improvement_module._exact_source_text_review_units(
        part,
        source_language="de",
    )

    assert len(units) == 1
    assert units[0].source == residual
    assert units[0].translated == residual
    assert units[0].translated_start == len(prefix)
    assert source[: units[0].translated_start] == prefix


def test_residual_source_text_review_retranslates_the_complete_focused_unit() -> None:
    source_residue = "This complete English sentence remains entirely untranslated."
    source = (
        "A carefully translated Spanish sentence provides enough surrounding context.\n"
        f"{source_residue}\n"
    )
    translated = (
        "Una frase traducida cuidadosamente al español aporta suficiente contexto alrededor.\n"
        f"{source_residue}\n"
    )
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        if calls == 1:
            content = "[]"
        else:
            assert "una unica frase o titulo" in payload["messages"][0]["content"]
            assert "FOCUS=" not in payload["messages"][1]["content"]
            assert "suficiente contexto alrededor" not in payload["messages"][1]["content"]
            content = json.dumps(
                {"translation": "Esta frase completa en inglés seguía enteramente sin traducir."}
            )
        return httpx.Response(200, json={"message": {"content": content}})

    result = review_translation_markdown(
        source,
        translated,
        LOCAL_SETTINGS,
        "es",
        transport=httpx.MockTransport(respond),
    )

    assert result == (
        "Una frase traducida cuidadosamente al español aporta suficiente contexto alrededor.\n"
        "Esta frase completa en inglés seguía enteramente sin traducir.\n"
    )
    assert calls == 2


def test_residual_source_text_candidate_must_clear_the_report_signal() -> None:
    source = "This complete English sentence remains entirely untranslated.\n"
    part = improvement_module._TranslationReviewPart(source, source)
    candidate = "That complete English sentence remains entirely untranslated.\n"

    with pytest.raises(ImprovementError, match="texto detectado"):
        improvement_module._validate_residual_translation_review_candidate(
            part,
            candidate,
            source,
            source_language="en",
            target_language="es",
        )


def test_residual_review_rejects_a_new_source_word_even_when_other_residue_decreases() -> None:
    part = improvement_module._TranslationReviewPart(
        "The decans and gaggles preserve a body of scholarship.\n",
        "Los decans y gaggles conservan un cuerpo académico.\n",
    )
    candidate = "Los decanos y grupos conservan un cuerpo de scholarship.\n"

    with pytest.raises(ImprovementError, match="introdujo texto"):
        improvement_module._validate_residual_translation_review_candidate(
            part,
            candidate,
            part.translated,
            source_language="en",
            target_language="es",
        )


def test_residual_review_accepts_a_candidate_that_only_removes_source_residue() -> None:
    part = improvement_module._TranslationReviewPart(
        "The decans and gaggles preserve a body of scholarship.\n",
        "Los decans y gaggles conservan un cuerpo académico.\n",
    )
    candidate = "Los decanos y grupos conservan un cuerpo académico.\n"

    improvement_module._validate_residual_translation_review_candidate(
        part,
        candidate,
        part.translated,
        source_language="en",
        target_language="es",
    )


def test_residual_micro_candidate_defers_language_detection_to_the_full_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    part = improvement_module._TranslationReviewPart(
        "The decans preserve a tradition.\n",
        "Los decans conservan una tradición.\n",
    )
    target_languages: list[str | None] = []

    def validate(*_args: object, target_language: str | None, **_kwargs: object) -> None:
        target_languages.append(target_language)

    monkeypatch.setattr(improvement_module, "validate_translation_quality", validate)

    improvement_module._validate_translation_review_candidate(
        part,
        "Los decanos conservan una tradición.\n",
        source_language="en",
        target_language="es",
        require_target_language=False,
    )

    assert target_languages == [None]


def test_bilingual_review_candidate_cannot_add_a_lowercase_source_word() -> None:
    part = improvement_module._TranslationReviewPart(
        "The chapter preserves a careful body of scholarship.\n",
        "El capítulo conserva un cuerpo académico cuidadoso.\n",
    )
    candidate = "El capítulo conserva un cuerpo de scholarship cuidadoso.\n"

    with pytest.raises(ImprovementError, match="introdujo texto"):
        improvement_module._validate_translation_review_candidate(
            part,
            candidate,
            source_language="en",
            target_language="es",
        )


def test_bilingual_review_candidate_cannot_restore_an_uppercase_source_heading() -> None:
    part = improvement_module._TranslationReviewPart(
        "# TABLES OF CORRESPONDENCE\n",
        "# TABLAS DE CORRESPONDENCIA\n",
    )
    candidate = "# TABLAS DE CORRESPONDENCIA TABLES\n"

    with pytest.raises(ImprovementError, match="introdujo texto"):
        improvement_module._validate_translation_review_candidate(
            part,
            candidate,
            source_language="en",
            target_language="es",
        )


def test_bilingual_title_review_can_separate_an_ocr_joined_proper_name() -> None:
    part = improvement_module._TranslationReviewPart(
        "CENTRO DE TRANSURFING 437",
        "CENTRODETRANSURFING 437",
    )

    improvement_module._validate_translation_review_candidate(
        part,
        "TRANSURFING CENTER 437",
        source_language="es",
        target_language="en",
        allow_ocr_word_separation=True,
    )


def test_title_prompt_separates_multiple_joined_source_language_words() -> None:
    assert (
        improvement_module._separate_ocr_joined_title_words(
            "APÉNDICE 5: EDICIONESDELUJO 427",
            "es",
        )
        == "APÉNDICE 5: EDICIONES DE LUJO 427"
    )
    assert (
        improvement_module._separate_ocr_joined_title_words(
            "CENTRODETRANSURFING 437",
            "es",
        )
        == "CENTRO DE TRANSURFING 437"
    )


def test_title_prompt_does_not_split_a_single_language_hint() -> None:
    assert (
        improvement_module._separate_ocr_joined_title_words(
            "REALIDAD",
            "es",
        )
        == "REALIDAD"
    )


def test_regular_bilingual_review_cannot_use_ocr_separation_exception() -> None:
    part = improvement_module._TranslationReviewPart(
        "CENTRO DE TRANSURFING 437",
        "CENTRODETRANSURFING 437",
    )

    with pytest.raises(ImprovementError, match="introdujo texto"):
        improvement_module._validate_translation_review_candidate(
            part,
            "TRANSURFING CENTER 437",
            source_language="es",
            target_language="en",
        )


def test_document_review_reverts_only_a_cumulatively_unsafe_block() -> None:
    original = "Uno dos tres cuatro cinco seis.\n\nEste bloque conserva una errrata menor.\n"
    candidate = "Siete ocho nueve diez once doce.\n\nEste bloque conserva una errata menor.\n"

    repaired, preserved = improvement_module._preserve_unsafe_review_content_blocks(
        original,
        candidate,
    )

    assert preserved == 1
    assert repaired == (
        "Uno dos tres cuatro cinco seis.\n\nEste bloque conserva una errata menor.\n"
    )
    improvement_module._validate_mode_output(
        original,
        repaired,
        ImprovementMode.REVIEW_CONTENT,
    )


def test_bilingual_review_resumes_an_unchanged_validated_chunk() -> None:
    source = "A careful translation keeps every statement and number 42.\n"
    translated = "Una traducción cuidadosa conserva cada afirmación y el número 42.\n"
    cache: dict[str, str] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": "[]"}})

    def save_checkpoint(key: str, value: str) -> bool:
        cache[key] = value
        return True

    first = review_translation_markdown(
        source,
        translated,
        LOCAL_SETTINGS,
        "es",
        transport=httpx.MockTransport(respond),
        load_checkpoint=cache.get,
        save_checkpoint=save_checkpoint,
    )

    def reject_network(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("Una revisión bilingüe validada debe reanudarse desde caché.")

    resumed = review_translation_markdown(
        source,
        translated,
        LOCAL_SETTINGS,
        "es",
        transport=httpx.MockTransport(reject_network),
        load_checkpoint=cache.get,
    )

    assert first == resumed == translated
    assert cache


def test_bilingual_review_does_not_count_or_cache_two_unsafe_responses() -> None:
    source = "The document keeps two complete paragraphs.\n"
    translated = "El documento conserva dos párrafos completos.\n"
    cache: dict[str, str] = {}
    reviewed_segments: list[int] = []
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"message": {"content": "Texto inventado."}})

    def save_checkpoint(key: str, value: str) -> bool:
        cache[key] = value
        return True

    first = review_translation_markdown(
        source,
        translated,
        LOCAL_SETTINGS,
        "es",
        transport=httpx.MockTransport(respond),
        load_checkpoint=cache.get,
        save_checkpoint=save_checkpoint,
        on_reviewed_segments=reviewed_segments.append,
    )

    assert first == translated
    assert calls == 2
    assert cache == {}
    assert reviewed_segments == [0]


def test_bilingual_review_stops_after_three_consecutive_invalid_json_contracts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = "\n\n".join(
        f"Source paragraph {index} explains one complete technical point in sufficient detail."
        for index in range(1, 81)
    )
    translated = "\n\n".join(
        f"El párrafo {index} explica un punto técnico completo con suficiente detalle."
        for index in range(1, 81)
    )
    calls = 0
    reviewed_segments: list[int] = []

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"message": {"content": "respuesta no JSON"}})

    result = review_translation_markdown(
        source,
        translated,
        LOCAL_SETTINGS,
        "es",
        transport=httpx.MockTransport(respond),
        on_reviewed_segments=reviewed_segments.append,
    )

    assert result == translated
    assert calls == 6
    assert reviewed_segments == [0]
    assert "translation_review_stopped consecutive_invalid_contracts=3" in caplog.text


def test_bilingual_review_recognizes_an_explicit_cached_preservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "The source-language fragment remains intentionally unchanged.\n"
    translated = "El fragmento conservado permanece intencionadamente sin cambios.\n"
    part = improvement_module._TranslationReviewPart(source, translated)
    cache = {improvement_module._translation_review_checkpoint_key(part): translated}
    reviewed_segments: list[int] = []

    monkeypatch.setattr(
        improvement_module,
        "_validate_translation_review_candidate",
        lambda *_args, **_kwargs: pytest.fail(
            "Una preservación explícita no debe reinterpretarse como propuesta del modelo."
        ),
    )

    result = review_translation_markdown(
        source,
        translated,
        LOCAL_SETTINGS,
        "es",
        transport=httpx.MockTransport(
            lambda _request: pytest.fail("La preservación explícita debe reanudarse sin red.")
        ),
        load_checkpoint=cache.get,
        on_reviewed_segments=reviewed_segments.append,
    )

    assert result == translated
    assert reviewed_segments and reviewed_segments[0] > 0


def test_residual_review_caches_safe_preservation_after_rejected_cleanup() -> None:
    source = "The decans preserve a body of scholarship that remains important.\n"
    translated = "Los decans conservan un cuerpo de scholarship que sigue siendo importante.\n"
    cache: dict[str, str] = {}
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        content = "[]" if calls == 1 else "Texto inventado."
        return httpx.Response(200, json={"message": {"content": content}})

    def save_checkpoint(key: str, value: str) -> bool:
        cache[key] = value
        return True

    first = review_translation_markdown(
        source,
        translated,
        LOCAL_SETTINGS,
        "es",
        transport=httpx.MockTransport(respond),
        load_checkpoint=cache.get,
        save_checkpoint=save_checkpoint,
    )
    resumed = review_translation_markdown(
        source,
        translated,
        LOCAL_SETTINGS,
        "es",
        transport=httpx.MockTransport(
            lambda _request: pytest.fail("La preservación residual debe reanudarse desde caché.")
        ),
        load_checkpoint=cache.get,
    )

    assert first == resumed == translated
    assert calls == 2
    assert len(cache) == 2


def test_bilingual_review_logs_a_private_document_validation_reason(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = "Private source sentence that must never be logged.\n"
    translated = "Frase privada traducida que nunca debe registrarse.\n"

    def reject_quality(*_args: object, **_kwargs: object) -> None:
        raise improvement_module.TranslationQualityError(
            "La traducción cambió la separación de párrafos."
        )

    monkeypatch.setattr(improvement_module, "validate_translation_quality", reject_quality)

    result = review_translation_markdown(
        source,
        translated,
        LOCAL_SETTINGS,
        "es",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"message": {"content": "[]"}})
        ),
    )

    assert result == translated
    assert "error_type=TranslationQualityError" in caplog.text
    assert "reason=La traducción cambió la separación de párrafos." in caplog.text
    assert source.strip() not in caplog.text
    assert translated.strip() not in caplog.text


def test_bilingual_review_safely_skips_unaligned_source_and_translation(caplog) -> None:
    translated = "Primero y segundo en un solo bloque.\n"
    reviewed_segments: list[int] = []

    result = review_translation_markdown(
        "First.\n\nSecond.\n",
        translated,
        LOCAL_SETTINGS,
        "es",
        transport=httpx.MockTransport(lambda _request: pytest.fail("Unexpected request")),
        on_reviewed_segments=reviewed_segments.append,
    )

    assert result == translated
    assert reviewed_segments == [0]
    assert "translation_review_skipped alignment_unavailable=true" in caplog.text


def test_bilingual_review_reports_only_the_segments_selected_by_its_adaptive_plan() -> None:
    source_blocks = [
        (
            f"This source paragraph {index} explains one complete point in enough detail to "
            "exercise the adaptive bilingual review without introducing a quality warning."
        )
        for index in range(1, 21)
    ]
    translated_blocks = [
        (
            f"Este párrafo traducido {index} explica un punto completo con suficiente detalle "
            "para ejercitar la revisión bilingüe adaptativa sin introducir una incidencia."
        )
        for index in range(1, 21)
    ]
    source = "\n\n".join(source_blocks)
    translated = "\n\n".join(translated_blocks)
    report = TranslationQualityReport(
        "en",
        "es",
        "es",
        checked_segments=20,
        source_characters=len(source),
        translated_characters=len(translated),
        total_issues=0,
        issues=(),
        source_blocks=20,
        translated_blocks=20,
    )
    reviewed_segments: list[int] = []

    result = review_translation_markdown(
        source,
        translated,
        LOCAL_SETTINGS,
        "es",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"message": {"content": "[]"}})
        ),
        priority_block_count=0,
        quality_report=report,
        on_reviewed_segments=reviewed_segments.append,
    )

    assert result == translated
    assert len(reviewed_segments) == 1
    assert 0 < reviewed_segments[0] < report.translated_blocks


def test_bilingual_review_splits_aligned_long_line_sequences_without_loss() -> None:
    source = "".join(
        f"Source row {index} preserves its aligned meaning and structure.\n" for index in range(80)
    )
    translated = "".join(
        f"La fila {index} conserva su significado y su estructura alineados.\n"
        for index in range(80)
    )

    parts = improvement_module._plan_translation_review_parts(source, translated)

    assert len(parts) > 1
    assert "".join(part.source for part in parts) == source
    assert "".join(part.translated for part in parts) == translated
    assert max(len(part.translated) for part in parts) <= 1_400
    assert max(len(part.source) + len(part.translated) for part in parts) <= 2_800


def test_bilingual_review_keeps_safe_patches_when_another_patch_changes_a_number() -> None:
    source = "The careful report keeps number 42 and uses an accurate title.\n"
    translated = "El informe cuidadoso conserva el número 42 y usa un título inexacto.\n"
    patches = [
        {"old": "42", "new": "43"},
        {"old": "título inexacto", "new": "título exacto"},
    ]

    result = review_translation_markdown(
        source,
        translated,
        LOCAL_SETTINGS,
        "es",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"message": {"content": f"```json\n{json.dumps(patches)}\n```"}},
            )
        ),
    )

    assert result == translated.replace("inexacto", "exacto")
    assert "42" in result
    assert "43" not in result


def test_bilingual_review_rejects_email_and_url_mutations_but_keeps_a_safe_patch() -> None:
    source = "Write to author@example.com and read https://example.com for the accurate title.\n"
    translated = (
        "Escribe a author@example.com y consulta https://example.com para ver el título inexacto.\n"
    )
    patches = [
        {"old": "author@example.com", "new": "hacker@example.net"},
        {"old": "https://example.com", "new": "https://evil.example"},
        {"old": "título inexacto", "new": "título exacto"},
    ]

    result = review_translation_markdown(
        source,
        translated,
        LOCAL_SETTINGS,
        "es",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"message": {"content": json.dumps(patches)}},
            )
        ),
    )

    assert result == translated.replace("inexacto", "exacto")
    assert "author@example.com" in result
    assert "https://example.com" in result
    assert "hacker@example.net" not in result
    assert "https://evil.example" not in result


def test_bilingual_review_rejects_a_moved_toc_folio_but_keeps_safe_patches() -> None:
    source = "- Appendices 258\n\nThe careful report uses an accurate title.\n"
    translated = "- Apéndices 258\n\nEl informe cuidadoso usa un título inexacto.\n"
    patches = [
        {"old": "Apéndices 258", "new": "258 Apéndices"},
        {"old": "título inexacto", "new": "título exacto"},
    ]

    result = review_translation_markdown(
        source,
        translated,
        LOCAL_SETTINGS,
        "es",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"message": {"content": json.dumps(patches)}},
            )
        ),
    )

    assert result == translated.replace("inexacto", "exacto")
    assert "Apéndices 258" in result
    assert "258 Apéndices" not in result


def test_bilingual_review_recovers_complete_patches_from_a_truncated_array() -> None:
    source = "The careful report uses an accurate title.\n"
    translated = "El informe cuidadoso usa un título inexacto.\n"
    truncated = '[{"old":"título inexacto","new":"título exacto"},{"old":"unfinished","new":"cor'

    result = review_translation_markdown(
        source,
        translated,
        LOCAL_SETTINGS,
        "es",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"message": {"content": truncated}})
        ),
    )

    assert result == translated.replace("inexacto", "exacto")


def test_validated_chunks_resume_without_calling_ollama_again() -> None:
    source = "# Original\n\nThis paragraph keeps the same Markdown structure and number 42."
    cache: dict[str, str] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        content = payload["messages"][1]["content"].replace("Original", "Improved")
        return httpx.Response(200, json={"message": {"content": content}})

    def save_checkpoint(key: str, value: str) -> bool:
        cache[key] = value
        return True

    first = improve_markdown(
        source,
        ImprovementMode.CLEAN,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
        load_checkpoint=cache.get,
        save_checkpoint=save_checkpoint,
    )

    def reject_network(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("A validated checkpoint must avoid a repeated Ollama request.")

    resumed = improve_markdown(
        source,
        ImprovementMode.CLEAN,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(reject_network),
        load_checkpoint=cache.get,
    )

    assert first == resumed
    assert cache


def test_legacy_position_based_checkpoint_is_validated_and_migrated() -> None:
    source = "This translated paragraph contains enough meaningful natural language."
    translated = "Este párrafo traducido contiene suficiente lenguaje natural significativo."
    legacy_key = improvement_module._legacy_chunk_checkpoint_key(
        ImprovementMode.TRANSLATE,
        1,
        source,
    )
    cache = {legacy_key: translated}

    def reject_network(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("Un checkpoint antiguo válido debe evitar una petición repetida.")

    def save_checkpoint(key: str, value: str) -> bool:
        cache[key] = value
        return True

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(reject_network),
        load_checkpoint=cache.get,
        save_checkpoint=save_checkpoint,
    )

    assert result == translated
    assert cache[improvement_module._chunk_checkpoint_key(ImprovementMode.TRANSLATE, source)] == (
        translated
    )


def test_validated_unchanged_optional_review_resumes_without_calling_ollama_again() -> None:
    source = "This paragraph is already correct and should remain unchanged."
    cache: dict[str, str] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={"message": {"content": payload["messages"][1]["content"]}},
        )

    def save_checkpoint(key: str, value: str) -> bool:
        cache[key] = value
        return True

    first = improve_markdown(
        source,
        ImprovementMode.REVIEW_CONTENT,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
        load_checkpoint=cache.get,
        save_checkpoint=save_checkpoint,
    )

    def reject_network(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("Un resultado sin cambios ya validado debe poder reanudarse.")

    resumed = improve_markdown(
        source,
        ImprovementMode.REVIEW_CONTENT,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(reject_network),
        load_checkpoint=cache.get,
    )

    assert first == resumed == source
    assert cache


def test_optional_review_preserved_after_validation_failure_is_not_cached() -> None:
    source = "Chapter title\n\nA complete paragraph that must remain unchanged."
    cache: dict[str, str] = {}

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": "not-a-directive"}})

    def save_checkpoint(key: str, value: str) -> bool:
        cache[key] = value
        return True

    result = improve_markdown(
        source,
        ImprovementMode.REVIEW_STRUCTURE,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
        load_checkpoint=cache.get,
        save_checkpoint=save_checkpoint,
    )

    assert result == source
    assert cache == {}


def test_plain_text_instructions_do_not_request_markdown_structure() -> None:
    instructions = build_instructions(
        ImprovementMode.CLEAN,
        plain_text=True,
    )

    assert "texto sin formato" in instructions
    assert "No añadas sintaxis Markdown" in instructions


def test_validation_requires_internal_structure_markers() -> None:
    marker = "<!-- PZDOCDOCX000001XZQ -->"

    with pytest.raises(ImprovementError, match="marcadores internos"):
        improvement_module._validate_output(f"Antes\n{marker}\nDespués", "Antes\nDespués")


def test_validation_allows_translation_around_an_inline_private_marker() -> None:
    marker = "`PZDOC_EPUB_XML_RETRY_A_A_XZQ`"

    improvement_module._validate_internal_markers(
        f"{marker}Opening chapter{marker}",
        f"{marker}Capítulo inicial{marker}",
    )


def test_validation_rejects_a_changed_inline_private_marker() -> None:
    source = "`PZDOC_EPUB_XML_RETRY_A_A_XZQ` Opening chapter"
    changed = "`PZDOC_EPUB_XML_RETRY_A_B_XZQ` Capítulo inicial"

    with pytest.raises(ImprovementError, match="comentario interno"):
        improvement_module._validate_internal_markers(source, changed)


def test_validation_rejects_an_unquoted_private_marker_leak() -> None:
    with pytest.raises(ImprovementError, match="comentario interno"):
        improvement_module._validate_internal_markers(
            "Opening chapter",
            "Capítulo inicial PZDOC_LEAK_XZQ",
        )


def test_validation_locks_private_phrases_without_locking_their_whole_line() -> None:
    improvement_module._validate_internal_markers(
        "An internal comentario interno remains protected.",
        "Un comentario interno permanece protegido.",
    )


@pytest.mark.parametrize(
    ("mode", "language", "expected"),
    [
        (ImprovementMode.CLEAN, None, "No traduzcas"),
        (ImprovementMode.TRANSLATE, "Español", "idioma Español"),
        (ImprovementMode.CLEAN_AND_TRANSLATE, "Francés", "única transformación"),
    ],
)
def test_prompts_are_conservative_for_every_mode(
    mode: ImprovementMode,
    language: str | None,
    expected: str,
) -> None:
    instructions = build_instructions(mode, language)

    assert expected in instructions
    assert "No resumas" in instructions
    assert "No añadas explicaciones" in instructions
    assert "números, fechas, destinos de enlaces" in instructions
    assert "Devuelve únicamente el Markdown" in instructions
    assert "PZDOC" not in instructions


def test_review_prompts_are_scoped_and_structure_requires_markdown() -> None:
    content = build_instructions(ImprovementMode.REVIEW_CONTENT)
    structure = build_instructions(ImprovementMode.REVIEW_STRUCTURE)

    assert "ruido inequívoco" in content
    assert "no cambies la jerarquía" in content
    assert "No resumas" in content
    assert "No añadas explicaciones" in content
    assert "Conserva exactamente" in structure
    assert "no corrijas, traduzcas, elimines ni añadas" in structure
    assert "No resumas" in structure
    with pytest.raises(ImprovementError, match="formato Markdown"):
        build_instructions(ImprovementMode.REVIEW_STRUCTURE, plain_text=True)


def test_optional_review_preserves_the_document_if_final_reassembly_is_unsafe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "# Chapter\n\nThe complete document must remain recoverable."
    original_validate = improvement_module._validate_mode_output

    def validate_except_final(
        original: str,
        improved: str,
        mode: ImprovementMode,
        *,
        max_characters: int = improvement_module.MAX_OUTPUT_CHARACTERS,
    ) -> None:
        if max_characters == improvement_module.MAX_DOCUMENT_OUTPUT_CHARACTERS:
            raise ImprovementError("final reassembly mismatch")
        original_validate(original, improved, mode, max_characters=max_characters)

    monkeypatch.setattr(improvement_module, "_validate_mode_output", validate_except_final)

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": payload["messages"][1]["content"]}})

    assert (
        improve_markdown(
            source,
            ImprovementMode.REVIEW_STRUCTURE,
            LOCAL_SETTINGS,
            transport=httpx.MockTransport(respond),
        )
        == source
    )


def test_optional_review_keeps_safe_chunks_when_only_their_combination_is_unsafe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (
        "First paragraph contains teh first bounded correction and enough words to split.\n\n"
        "Second paragraph must recieve another bounded correction without losing content."
    )
    monkeypatch.setattr(improvement_module, "MAX_INPUT_CHARACTERS", 90)
    original_validate = improvement_module._validate_mode_output

    def reject_only_combination(
        original: str,
        improved: str,
        mode: ImprovementMode,
        *,
        max_characters: int = improvement_module.MAX_OUTPUT_CHARACTERS,
    ) -> None:
        original_validate(original, improved, mode, max_characters=max_characters)
        if (
            max_characters == improvement_module.MAX_DOCUMENT_OUTPUT_CHARACTERS
            and "the first bounded" in improved
            and "receive another" in improved
        ):
            raise ImprovementError("combined final mismatch")

    monkeypatch.setattr(
        improvement_module,
        "_validate_mode_output",
        reject_only_combination,
    )

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        proposal = (
            payload["messages"][1]["content"]
            .replace("teh", "the")
            .replace(
                "recieve",
                "receive",
            )
        )
        return httpx.Response(200, json={"message": {"content": proposal}})

    reviewed = improve_markdown(
        source,
        ImprovementMode.REVIEW_CONTENT,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
    )

    assert "the first bounded correction" in reviewed
    assert "must recieve another bounded correction" in reviewed
    assert reviewed != source


def test_translation_recovers_safe_chunks_when_final_structure_is_unsafe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (
        "# A careful first chapter guides every reader through a practical and complete "
        "exercise\n\n"
        "# A thoughtful second chapter explains every relevant decision with enough detail"
    )
    first_translation = (
        "Un primer capítulo cuidadoso guía a cada lector por un ejercicio práctico y completo"
    )
    second_translation = (
        "Un segundo capítulo reflexivo explica cada decisión relevante con suficiente detalle"
    )
    original_validate = improvement_module._validate_mode_output

    def reject_second_chunk_in_the_full_document(
        original: str,
        improved: str,
        mode: ImprovementMode,
        *,
        max_characters: int = improvement_module.MAX_OUTPUT_CHARACTERS,
    ) -> None:
        original_validate(original, improved, mode, max_characters=max_characters)
        if (
            max_characters == improvement_module.MAX_DOCUMENT_OUTPUT_CHARACTERS
            and second_translation in improved
        ):
            raise ImprovementError("El modelo cambió u omitió marcadores internos del documento.")

    monkeypatch.setattr(
        improvement_module,
        "_validate_mode_output",
        reject_second_chunk_in_the_full_document,
    )

    def respond(request: httpx.Request) -> httpx.Response:
        fragment = json.loads(request.content)["messages"][1]["content"]
        translated = first_translation if "careful first" in fragment else second_translation
        return httpx.Response(200, json={"message": {"content": translated}})

    preserved: list[tuple[int, int]] = []
    translated = improve_markdown(
        source,
        ImprovementMode.CLEAN_AND_TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
        on_translation_preserved=lambda current, total: preserved.append((current, total)),
    )

    assert translated == (
        f"# {first_translation}\n\n"
        "# A thoughtful second chapter explains every relevant decision with enough detail"
    )
    assert preserved == [(2, 2)]


def test_unsafe_translation_chunk_is_preserved_and_reported_for_manual_review() -> None:
    source = "This complete paragraph must remain available when a translation is unsafe."
    preserved: list[tuple[int, int]] = []

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"message": {"content": "Texto traducido.\n\nPárrafo añadido."}},
        )

    translated = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
        on_translation_preserved=lambda current, total: preserved.append((current, total)),
    )

    assert translated == source
    assert preserved == [(1, 1)]


@pytest.mark.parametrize(
    "mode",
    [ImprovementMode.TRANSLATE, ImprovementMode.CLEAN_AND_TRANSLATE],
)
def test_translation_modes_require_a_language(mode: ImprovementMode) -> None:
    with pytest.raises(ImprovementError, match="idioma"):
        build_instructions(mode, " ")


def test_translation_prompt_requires_every_markdown_text_block() -> None:
    instructions = build_instructions(ImprovementMode.TRANSLATE, "Español")

    assert "TODO" in instructions
    assert "título, párrafo, elemento de lista y celda de tabla" in instructions
    assert "no dejes pasajes" in instructions
    assert "redacción natural e idiomática" in instructions
    assert "orden propio del idioma de destino" in instructions
    assert "conserva ese tratamiento" in instructions


def test_local_ai_translation_preserves_an_all_caps_heading() -> None:
    source = "## CREATE YOUR VISION"

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": "Crea tu visión"}})

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
        source_language_code="en",
    )

    assert result == "## CREA TU VISIÓN"


def test_translation_resolves_an_emphasized_established_label_without_a_model() -> None:
    result = improve_markdown(
        "**TAURUS**",
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(lambda _request: pytest.fail("Unexpected request")),
        source_language_code="en",
    )

    assert result == "**TAURO**"


def test_locked_value_fallback_translates_a_short_numeric_label() -> None:
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        content = json.loads(request.content)["messages"][1]["content"]
        requests.append(content)
        return httpx.Response(200, json={"message": {"content": "Tabla"}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        translated = improvement_module._improve_translation_with_locked_values(
            client,
            "parsezen-local",
            8_192,
            build_instructions(ImprovementMode.TRANSLATE, "Español"),
            "Table 1 / 2 / 3",
            improvement_module._TranslationContext("en", "es", True),
            None,
        )

    assert requests == ["Table"]
    assert translated == "Tabla 1 / 2 / 3"


def test_short_pdf_headings_are_translated_in_one_aligned_batch() -> None:
    headings = (
        ("BUILD YOUR VISION", "CONSTRUYE TU VISIÓN"),
        ("DEFINE YOUR GOALS", "DEFINE TUS METAS"),
        ("CHOOSE YOUR FUTURE", "ELIGE TU FUTURO"),
        ("CREATE YOUR PLAN", "CREA TU PLAN"),
        ("REVIEW YOUR PROGRESS", "REVISA TU PROGRESO"),
        ("CELEBRATE YOUR SUCCESS", "CELEBRA TU ÉXITO"),
    )
    source = "\n\n".join(
        f"<!-- PZDOC PDF PAGE {index} -->\n\n## {heading}"
        for index, (heading, _translation) in enumerate(headings, 1)
    )
    translations = dict(headings)
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        request_payload = json.loads(request.content)
        assert request_payload["format"] == "json"
        batch = json.loads(request_payload["messages"][1]["content"])
        response_items = [
            {"id": item["id"], "text": translations[item["text"]]} for item in batch["items"]
        ]
        return httpx.Response(
            200,
            json={"message": {"content": json.dumps({"items": response_items})}},
        )

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
        source_language_code="en",
    )

    assert calls == 1
    assert all(translation in result for _source, translation in headings)
    assert all(source_heading not in result for source_heading, _translation in headings)
    assert result.count("<!-- PZDOC PDF PAGE") == len(headings)


def test_segmented_list_fallback_reuses_aligned_batching() -> None:
    translations = {
        "Build a practical vision.": "Construye una visión práctica.",
        "Define every relevant goal.": "Define cada meta relevante.",
        "Choose a realistic future.": "Elige un futuro realista.",
        "Create a careful plan.": "Crea un plan cuidadoso.",
        "Review measurable progress.": "Revisa el progreso medible.",
        "Celebrate meaningful success.": "Celebra un éxito significativo.",
    }
    source = "\n".join(f"- {text}" for text in translations)
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        request_payload = json.loads(request.content)
        batch = json.loads(request_payload["messages"][1]["content"])
        response_items = [
            {"id": item["id"], "text": translations[item["text"]]} for item in batch["items"]
        ]
        return httpx.Response(
            200,
            json={"message": {"content": json.dumps({"items": response_items})}},
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = improvement_module._improve_translation_segments(
            client,
            "parsezen-local",
            8_192,
            build_instructions(ImprovementMode.TRANSLATE, "Español"),
            source,
            improvement_module._TranslationContext("en", "es", True),
            None,
        )

    assert calls == 1
    assert result == "\n".join(f"- {text}" for text in translations.values())


def test_segmented_short_titles_receive_bounded_neighbor_context() -> None:
    source = "CHOOSE YO'\nCHARACTER"
    requests: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        request_payload = json.loads(request.content)
        system_prompt = request_payload["messages"][0]["content"]
        content = request_payload["messages"][1]["content"]
        requests.append((system_prompt, content))
        translated = "ELIGE TU" if content == "CHOOSE YO'" else "PERSONAJE"
        return httpx.Response(
            200,
            json={"message": {"content": translated}},
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = improvement_module._improve_translation_segments(
            client,
            "parsezen-local",
            8_192,
            build_instructions(ImprovementMode.TRANSLATE, "Español"),
            source,
            improvement_module._TranslationContext("en", "es", True),
            None,
        )

    assert result == "ELIGE TU\nPERSONAJE"
    assert requests[0][1] == "CHOOSE YO'"
    assert "CHARACTER" in requests[0][0]
    assert requests[1][1] == "CHARACTER"
    assert "CHOOSE YO'" in requests[1][0]


def test_translation_discards_an_echoed_list_marker_before_restoring_the_source_prefix() -> None:
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        content = json.loads(request.content)["messages"][1]["content"]
        requests.append(content)
        return httpx.Response(200, json={"message": {"content": "- Lee la guía completa."}})

    result = improve_markdown(
        "- Read the complete guide.",
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
        source_language_code="en",
    )

    assert requests == ["Read the complete guide."]
    assert result == "- Lee la guía completa."


def test_translation_discards_a_list_marker_added_to_an_unmarked_title() -> None:
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        content = json.loads(request.content)["messages"][1]["content"]
        requests.append(content)
        return httpx.Response(
            200,
            json={"message": {"content": "- Interpretación de la condición planetaria"}},
        )

    result = improve_markdown(
        "Interpreting planetary condition",
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
        source_language_code="en",
    )

    assert requests == ["Interpreting planetary condition"]
    assert result == "Interpretación de la condición planetaria"


def test_translation_expands_unambiguous_english_yo_possessive_only_for_the_model() -> None:
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        content = json.loads(request.content)["messages"][1]["content"]
        requests.append(content)
        return httpx.Response(200, json={"message": {"content": "ELIGE TU PERSONAJE"}})

    result = improve_markdown(
        "## CHOOSE YO' CHARACTER",
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
        source_language_code="en",
    )

    assert requests == ["CHOOSE YOUR CHARACTER"]
    assert result == "## ELIGE TU PERSONAJE"


def test_empty_private_image_reference_never_reaches_the_translation_model() -> None:
    source = (
        "This complete paragraph needs a faithful translation.\n\n"
        "![](<__parsezen_resources__/pdf/page-0001-image-01.jpg>)"
    )
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"message": {"content": "Este párrafo completo necesita una traducción fiel."}},
        )

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
        source_language_code="en",
    )

    assert calls == 1
    assert "Este párrafo completo" in result
    assert "![](<__parsezen_resources__/pdf/page-0001-image-01.jpg>)" in result


def test_uppercase_normalization_does_not_change_protected_link_destinations() -> None:
    source = "# VISIT OUR SITE"
    translated = "# Visita [nuestro sitio](https://Example.com/Path)"
    context = improvement_module._TranslationContext("en", "es", True)

    result = improvement_module._preserve_translation_uppercase(source, translated, context)

    assert result == "# VISITA [NUESTRO SITIO](https://Example.com/Path)"


def test_focused_title_prompt_translates_names_of_works_but_preserves_people() -> None:
    assert "títulos" in improvement_module.FOCUSED_TITLE_INSTRUCTION
    assert "canciones, libros u otras obras" in improvement_module.FOCUSED_TITLE_INSTRUCTION
    assert "artistas y nombres propios" in improvement_module.FOCUSED_TITLE_INSTRUCTION
    assert "signos" in improvement_module.FOCUSED_TITLE_INSTRUCTION
    assert "zodiacales" in improvement_module.FOCUSED_TITLE_INSTRUCTION
    assert "signos zodiacales" in improvement_module.FOCUSED_BILINGUAL_TITLE_INSTRUCTION
    assert "nombre convencional" in improvement_module.FOCUSED_TITLE_INSTRUCTION
    assert "del signo" in improvement_module.FOCUSED_TITLE_INSTRUCTION
    assert "comprobación léxica silenciosa" in improvement_module.BASE_INSTRUCTIONS


def test_initial_translation_request_receives_source_derived_lexical_attention() -> None:
    source = "His scholarship brought recognition to his work."
    prompts: list[str] = []
    fragments: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        prompt = payload["messages"][0]["content"]
        fragment = payload["messages"][1]["content"]
        prompts.append(prompt)
        fragments.append(fragment)
        if "lexicógrafo" in prompt:
            return httpx.Response(200, json={"message": {"content": "labor académica"}})
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": fragment.replace(
                        "His ",
                        "Su ",
                    ).replace(
                        " brought recognition to his work.",
                        " dio reconocimiento a su obra.",
                    )
                }
            },
        )

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
        source_language_code="en",
    )

    assert result == "Su labor académica dio reconocimiento a su obra."
    assert len(prompts) == 2
    assert "lexicógrafo" in prompts[0]
    assert "scholarship" in fragments[0]
    assert "scholarship" not in fragments[1]


def test_initial_translation_request_omits_lexical_attention_without_candidates() -> None:
    prompts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        prompts.append(payload["messages"][0]["content"])
        return httpx.Response(200, json={"message": {"content": "Un gráfico sencillo."}})

    result = improve_markdown(
        "A simple chart.",
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
        source_language_code="en",
    )

    assert result == "Un gráfico sencillo."
    assert "Condición léxica de aceptación" not in prompts[0]


def test_regular_prose_does_not_lock_a_common_established_term_before_translation() -> None:
    source = (
        "Each pair provides equal amounts of day and night, and rises and sets from the same "
        "part of the horizon."
    )
    fragments: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        fragment = payload["messages"][1]["content"]
        fragments.append(fragment)
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": (
                        "Cada par proporciona cantidades iguales de día y noche, y sale y se pone "
                        "desde la misma parte del horizonte."
                    )
                }
            },
        )

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
        source_language_code="en",
    )

    assert result.startswith("Cada par proporciona")
    assert fragments == [source]
    assert "PZDOCLEX" not in fragments[0]


def test_focused_source_repair_explicitly_forbids_copying_residual_prose() -> None:
    prompts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        prompts.append(payload["messages"][0]["content"])
        return httpx.Response(
            200,
            json={"message": {"content": "El horizonte separa ambos hemisferios."}},
        )

    result = improve_markdown(
        "The horizon separates both hemispheres.",
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
        source_language_code="en",
        focused_source_repair=True,
    )

    assert result == "El horizonte separa ambos hemisferios."
    assert "conservó prosa en el idioma de origen" in prompts[0]
    assert "No copies ninguna oración" in prompts[0]


def test_established_term_is_grounded_without_asking_model_to_classify_it() -> None:
    source = "Rulership defines an essential planetary dignity."
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        system_prompt = payload["messages"][0]["content"]
        fragment = payload["messages"][1]["content"]
        requests.append(system_prompt)
        translated = fragment.replace(
            " defines an essential planetary dignity.",
            " define una dignidad planetaria esencial.",
        )
        return httpx.Response(200, json={"message": {"content": translated}})

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
        source_language_code="en",
    )

    assert result == "Regencia define una dignidad planetaria esencial."
    assert len(requests) == 1
    assert "lexicógrafo" not in requests[0]


def test_short_index_term_uses_the_document_source_language_without_a_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fragments: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        fragment = payload["messages"][1]["content"]
        fragments.append(fragment)
        return httpx.Response(200, json={"message": {"content": fragment}})

    monkeypatch.setattr(improvement_module, "detect_language_code", lambda _value: "de")
    result = improve_markdown(
        "54. SUMMARY AND SOURCE READINGS 525",
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
        source_language_code="en",
    )

    assert result == "54. RESUMEN Y LECTURAS DE FUENTES 525"
    assert fragments == []


def test_fully_grounded_structural_title_skips_the_model_request() -> None:
    fragments: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        fragment = payload["messages"][1]["content"]
        fragments.append(fragment)
        return httpx.Response(200, json={"message": {"content": fragment}})

    result = improve_markdown(
        "PART SIX: THE ART OF JUDGMENT 531",
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
        source_language_code="en",
    )

    assert result == "PARTE SEIS: EL ARTE DEL JUICIO 531"
    assert fragments == []


def test_bilingual_review_cannot_degrade_an_established_equivalent() -> None:
    part = improvement_module._TranslationReviewPart(
        "SECTION SIX: THE SCIENCE OF JUDGMENT 531",
        "SECCIÓN SEIS: LA CIENCIA DEL JUICIO 531",
    )

    with pytest.raises(
        ImprovementError,
        match="equivalencia terminológica establecida",
    ):
        improvement_module._validate_translation_review_candidate(
            part,
            "SECCIÓN SEIS: LA CIENCIA DE LA JUICIO 531",
            source_language="en",
            target_language="es",
        )


def test_long_translation_with_risky_word_is_segmented_before_initial_generation() -> None:
    sequence = (
        "This opening sentence provides enough context for the complete paragraph. "
        "The author's scholarship brought recognition to the careful historical study. "
        "A final sentence preserves the conclusion and the original order."
    )
    source = " ".join(sequence for _index in range(4))
    requested_fragments: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        prompt = payload["messages"][0]["content"]
        fragment = payload["messages"][1]["content"]
        if "lexicógrafo" in prompt:
            return httpx.Response(200, json={"message": {"content": "labor académica"}})
        requested_fragments.append(fragment)
        translated = (
            fragment.replace(
                "This opening sentence provides enough context for the complete paragraph.",
                "Esta frase inicial aporta contexto suficiente para el párrafo completo.",
            )
            .replace(
                "The author's ",
                "La ",
            )
            .replace(
                " brought recognition to the careful historical study.",
                " del autor dio reconocimiento al cuidadoso estudio histórico.",
            )
            .replace(
                "A final sentence preserves the conclusion and the original order.",
                "Una frase final conserva la conclusión y el orden original.",
            )
        )
        return httpx.Response(200, json={"message": {"content": translated}})

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
        source_language_code="en",
    )

    assert "scholarship" not in result
    assert len(requested_fragments) > 1
    assert all(
        all(
            len(item["text"]) <= improvement_module.MAX_FOCUSED_LEXICAL_TRANSLATION_CHARACTERS
            for item in json.loads(fragment)["items"]
        )
        if fragment.startswith('{"items":')
        else len(fragment) <= improvement_module.MAX_FOCUSED_LEXICAL_TRANSLATION_CHARACTERS
        for fragment in requested_fragments
    )


def test_translation_restores_exact_paragraph_spacing_around_internal_markers() -> None:
    source = "First paragraph.\n\nSecond paragraph."

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        protected = payload["messages"][1]["content"]
        translated = protected.replace("First paragraph", "Primer párrafo").replace(
            "Second paragraph",
            "Segundo párrafo",
        )
        translated = translated.replace("<!-- PZDOC", "  <!-- PZDOC").replace(
            "XZQ -->",
            "XZQ -->  ",
        )
        return httpx.Response(200, json={"message": {"content": translated}})

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == "Primer párrafo.\n\nSegundo párrafo."


def test_translation_falls_back_to_individual_paragraphs_after_marker_loss() -> None:
    source = (
        "This first English paragraph contains enough meaningful text for translation.\n\n"
        "This second English paragraph also contains meaningful text for translation."
    )
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        protected = payload["messages"][1]["content"]
        if "<!--" in protected:
            translated = re.sub(r"\n<!-- PZDOCP[A-Z]+XZQ -->\n", "\n\n", protected)
        else:
            translated = protected.replace(
                "This first English paragraph contains enough meaningful text for translation.",
                "Este primer párrafo contiene suficiente texto con significado para traducir.",
            ).replace(
                "This second English paragraph also contains meaningful text for translation.",
                "Este segundo párrafo también contiene texto con significado para traducir.",
            )
        return httpx.Response(200, json={"message": {"content": translated}})

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == (
        "Este primer párrafo contiene suficiente texto con significado para traducir.\n\n"
        "Este segundo párrafo también contiene texto con significado para traducir."
    )
    assert calls == 4


def test_combined_mode_falls_back_to_safe_segments_after_marker_loss() -> None:
    source = (
        "This first English paragraph contains enough meaningful text for translation.\n\n"
        "This second English paragraph also contains meaningful text for translation."
    )
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        protected = payload["messages"][1]["content"]
        if "<!--" in protected:
            translated = re.sub(r"\n<!-- PZDOCP[A-Z]+XZQ -->\n", "\n\n", protected)
        else:
            translated = protected.replace(
                "This first English paragraph contains enough meaningful text for translation.",
                "Este primer párrafo contiene suficiente texto con significado para traducir.",
            ).replace(
                "This second English paragraph also contains meaningful text for translation.",
                "Este segundo párrafo también contiene texto con significado para traducir.",
            )
        return httpx.Response(200, json={"message": {"content": translated}})

    result = improve_markdown(
        source,
        ImprovementMode.CLEAN_AND_TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == (
        "Este primer párrafo contiene suficiente texto con significado para traducir.\n\n"
        "Este segundo párrafo también contiene texto con significado para traducir."
    )
    assert calls == 4


def test_translation_locks_numeric_values_outside_the_final_fallback_requests() -> None:
    source = (
        "This single sentence contains 36 carefully counted houses and enough English words "
        "for reliable local validation."
    )
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        protected = payload["messages"][1]["content"]
        if "PZDOC" in protected:
            translated = re.sub(r"PZDOC[A-Z]+XZQ", "", protected)
        else:
            translated = protected.replace(
                "This single sentence contains",
                "Esta oración contiene",
            ).replace(
                "carefully counted houses and enough English words for reliable local validation.",
                "casas contadas cuidadosamente y suficientes palabras para una validación local "
                "fiable.",
            )
        return httpx.Response(200, json={"message": {"content": translated}})

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == (
        "Esta oración contiene 36 casas contadas cuidadosamente y suficientes palabras para "
        "una validación local fiable."
    )
    assert calls == 4


def test_translation_falls_back_to_sentences_inside_one_paragraph() -> None:
    first = (
        "This first English sentence contains enough natural language to validate a complete "
        "translation safely."
    )
    second = (
        "This second English sentence also contains enough meaningful prose to verify the "
        "sentence fallback."
    )
    source = f"{first} {second}"
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        content = payload["messages"][1]["content"]
        translated = (
            content.replace(
                first,
                "Esta primera oración contiene suficiente lenguaje natural para validar con "
                "seguridad una traducción completa.",
            ).replace(
                second,
                "Esta segunda oración también contiene suficiente prosa significativa para "
                "comprobar el respaldo por oraciones.",
            )
            if content in {first, second}
            else content
        )
        return httpx.Response(200, json={"message": {"content": translated}})

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result.startswith("Esta primera oración")
    assert "Esta segunda oración" in result
    assert calls == 4


def test_translation_segment_fallback_preserves_list_prefixes_locally() -> None:
    source = "- Read the complete guide.\n- Keep every useful detail."
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        fragment = payload["messages"][1]["content"]
        if "\n" in fragment:
            translated = "- Lee la guía completa.\n\n- Conserva cada detalle útil."
        elif "Read" in fragment:
            translated = "Lee la guía completa."
        else:
            translated = "Conserva cada detalle útil."
        return httpx.Response(200, json={"message": {"content": translated}})

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == "- Lee la guía completa.\n- Conserva cada detalle útil."
    assert calls == 4


def test_translation_segment_fallback_recurses_into_a_list_paragraph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    introduction = "This introduction explains the complete exercise."
    list_block = "- Read the complete guide.\n- Keep every useful detail."
    source = f"{introduction}\n\n{list_block}"
    context = improvement_module._TranslationContext("en", "es", True)
    calls: list[str] = []
    translations = {
        introduction: "Esta introducción explica el ejercicio completo.",
        "- Read the complete guide.": "- Lee la guía completa.",
        "- Keep every useful detail.": "- Conserva cada detalle útil.",
    }

    monkeypatch.setattr(
        improvement_module,
        "_aligned_translation_batch_item",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        improvement_module,
        "_prepare_and_validate_response",
        lambda _source, response, *_args, **_kwargs: response,
    )

    def translate_part(
        _client: httpx.Client,
        _model: str,
        _context_window: int,
        _instructions: str,
        markdown: str,
        **_kwargs: object,
    ) -> str:
        calls.append(markdown)
        if markdown == list_block:
            raise ImprovementError("La traducción cambió la estructura de las listas.")
        return translations[markdown]

    monkeypatch.setattr(improvement_module, "_improve_part", translate_part)
    with httpx.Client() as client:
        translated = improvement_module._improve_translation_segments(
            client,
            "parsezen-local",
            8_192,
            "Translate",
            source,
            context,
            None,
        )

    assert translated == (
        "Esta introducción explica el ejercicio completo.\n\n"
        "- Lee la guía completa.\n- Conserva cada detalle útil."
    )
    assert calls == [
        introduction,
        list_block,
        "- Read the complete guide.",
        "- Keep every useful detail.",
    ]
    assert context.preserved_segments == []


def test_translation_segment_fallback_keeps_each_toc_folio_attached() -> None:
    source = "- FIRST HOUSE 10\n- SECOND HOUSE 20"
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        fragment = payload["messages"][1]["content"]
        tokens = re.findall(r"PZDOC[A-Z]+XZQ", fragment)
        if "\n" in fragment:
            translated = f"- PRIMERA CASA {tokens[1]}\n- SEGUNDA CASA {tokens[0]}"
        else:
            translated = fragment.replace("FIRST HOUSE", "PRIMERA CASA").replace(
                "SECOND HOUSE",
                "SEGUNDA CASA",
            )
        return httpx.Response(200, json={"message": {"content": translated}})

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == "- PRIMERA CASA 10\n- SEGUNDA CASA 20"
    assert calls == 4


def test_translation_segment_fallback_preserves_only_the_sentence_that_still_fails() -> None:
    source_sentences = (
        "This first English sentence contains enough natural language for a safe translation.",
        "This second English sentence also provides meaningful prose for the local validator.",
        "This third English sentence confirms that most of the paragraph can remain translated.",
        "This final English sentence is deliberately preserved for a focused repair.",
    )
    translations = {
        source_sentences[0]: (
            "Esta primera oración contiene suficiente lenguaje natural para una traducción segura."
        ),
        source_sentences[1]: (
            "Esta segunda oración también aporta prosa significativa para el validador local."
        ),
        source_sentences[2]: (
            "Esta tercera oración confirma que la mayor parte del párrafo puede quedar traducida."
        ),
    }
    source = " ".join(source_sentences)
    calls = 0
    preserved: list[tuple[int, int]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        content = payload["messages"][1]["content"]
        return httpx.Response(
            200,
            json={"message": {"content": translations.get(content, content)}},
        )

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
        on_translation_preserved=lambda current, total: preserved.append((current, total)),
    )

    assert result.startswith("Esta primera oración")
    assert "Esta tercera oración" in result
    assert result.endswith(source_sentences[3])
    assert calls == 3 + len(source_sentences)
    assert preserved == [(1, 1)]


def test_translation_segment_fallback_keeps_valid_paragraphs_when_residue_is_large() -> None:
    source_paragraphs = (
        "This first English paragraph contains enough natural language for safe translation.",
        "This second English paragraph also provides meaningful prose for the validator.",
        "This third English paragraph is deliberately preserved for a focused repair.",
        "This fourth English paragraph is also deliberately preserved for manual review.",
    )
    translations = {
        source_paragraphs[0]: (
            "Este primer párrafo contiene suficiente lenguaje natural para una traducción segura."
        ),
        source_paragraphs[1]: (
            "Este segundo párrafo también aporta prosa significativa para el validador."
        ),
    }
    source = "\n\n".join(source_paragraphs)
    preserved: list[tuple[int, int]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        content = payload["messages"][1]["content"]
        return httpx.Response(
            200,
            json={"message": {"content": translations.get(content, content)}},
        )

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
        on_translation_preserved=lambda current, total: preserved.append((current, total)),
    )

    assert result.startswith("Este primer párrafo")
    assert "Este segundo párrafo" in result
    assert source_paragraphs[2] in result
    assert result.endswith(source_paragraphs[3])
    assert preserved == [(1, 1)]


def test_translation_repairs_individual_lines_in_a_dense_index() -> None:
    source_lines = (
        "FIRST ASTROLOGICAL HOUSE 1",
        "SECOND ASTROLOGICAL HOUSE 2",
        "THIRD ASTROLOGICAL HOUSE 3",
        "FOURTH ASTROLOGICAL HOUSE 4",
        "FIFTH ASTROLOGICAL HOUSE 5",
        "SIXTH ASTROLOGICAL HOUSE 6",
    )
    translated_lines = (
        "PRIMERA CASA ASTROLÓGICA 1",
        "SEGUNDA CASA ASTROLÓGICA 2",
        "TERCERA CASA ASTROLÓGICA 3",
        "CUARTA CASA ASTROLÓGICA 4",
        "QUINTA CASA ASTROLÓGICA 5",
        "SEXTA CASA ASTROLÓGICA 6",
    )
    source = "\n".join(source_lines)
    translations = dict(zip(source_lines, translated_lines, strict=True))
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        content = payload["messages"][1]["content"]
        if content.startswith('{"items":'):
            batch = json.loads(content)
            response_items = []
            for item, translated_line in zip(batch["items"], translated_lines, strict=True):
                protected_number = item["text"].rsplit(" ", 1)[1]
                translated_text = f"{translated_line.rsplit(' ', 1)[0]} {protected_number}"
                response_items.append({"id": item["id"], "text": translated_text})
            return httpx.Response(
                200,
                json={"message": {"content": json.dumps({"items": response_items})}},
            )
        translated = translations.get(content, content)
        return httpx.Response(200, json={"message": {"content": translated}})

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == "\n".join(translated_lines)
    assert calls == 2


def test_translation_repairs_a_partially_translated_line_in_a_dense_index() -> None:
    index = "\n".join(
        (
            "SCORPIO I 174",
            "SAGITTARIUS I 192",
            "CAPRICORN I 210",
        )
    )
    paragraph = "This English paragraph establishes the source language for the complete document."
    translated_paragraph = "Este parrafo establece el idioma de origen para el documento completo."
    source = f"{index}\n\n{paragraph}"
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        content = payload["messages"][1]["content"]
        requests.append(content)
        translated = (
            f"ESCORPIO{content[len('SCORPIO') :]}"
            if "\n" not in content and content.startswith("SCORPIO")
            else content.replace("SCORPIO", "SCORPION")
            .replace("SAGITTARIUS", "SAGITARIO")
            .replace("CAPRICORN", "CAPRICORNO")
            .replace(paragraph, translated_paragraph)
        )
        return httpx.Response(200, json={"message": {"content": translated}})

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == "\n".join(
        (
            "ESCORPIO I 174",
            "SAGITARIO I 192",
            "CAPRICORNIO I 210",
            "",
            translated_paragraph,
        )
    )
    focused_requests = [
        request for request in requests if "\n" not in request and request.startswith("SCORPIO")
    ]
    assert focused_requests == []


def test_translation_repairs_an_unchanged_title_with_one_focused_request() -> None:
    source = (
        "30 DAY MILLIONAIRE CHALLENGE\n\n"
        "This deliberately long English paragraph contains enough natural language to verify "
        "that the main passage reaches Spanish while the unchanged title is repaired separately."
    )
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        protected = payload["messages"][1]["content"]
        if "DAY MILLIONAIRE CHALLENGE" not in protected:
            translated = protected.replace(
                "This deliberately long English paragraph contains enough natural language to "
                "verify that the main passage reaches Spanish while the unchanged title is "
                "repaired separately.",
                "Este párrafo deliberadamente largo contiene suficiente lenguaje natural para "
                "comprobar que el pasaje principal llega al español mientras el título sin "
                "traducir se repara por separado.",
            )
        elif calls == 1:
            translated = protected
        else:
            number_marker = protected.split()[0]
            translated = f"DESAFÍO DEL MILLONARIO EN {number_marker} DÍAS"
        return httpx.Response(200, json={"message": {"content": translated}})

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result.startswith("DESAFÍO DEL MILLONARIO EN 30 DÍAS\n\nEste párrafo")
    assert calls == 3


def test_bilingual_title_retranslation_allows_a_complete_short_label_translation() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": "Imágenes del Picatrix",
                }
            },
        )

    result = improvement_module.retranslate_residual_title(
        "9. Images from the Picatrix",
        "9. Images from the Picatrix",
        LOCAL_SETTINGS,
        "Español",
        source_language_code="en",
        transport=httpx.MockTransport(respond),
    )

    assert result == "9. Imágenes del Picatrix"


def test_bilingual_title_retranslation_protects_written_cardinals_semantically() -> None:
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        protected_title = payload["messages"][1]["content"]
        requests.append(protected_title)
        marker_match = re.search(r"PZDOCCARD[A-Z]+XZQ", protected_title)
        assert marker_match is not None
        marker = marker_match.group(0)
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": f"PARTE {marker}: CON LOS PIES EN LA TIERRA",
                }
            },
        )

    result = improvement_module.retranslate_residual_title(
        "PART SEVEN: DOWN TO EARTH",
        "PARTE SIETE: DOWN TOEARTH",
        LOCAL_SETTINGS,
        "Español",
        source_language_code="en",
        transport=httpx.MockTransport(respond),
    )

    assert result == "PARTE SIETE: CON LOS PIES EN LA TIERRA"
    assert len(requests) == 1
    assert "SEVEN" not in requests[0]


def test_bilingual_title_retranslation_accepts_one_soft_wrapped_title() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)["messages"][1]["content"]
        delimiter = re.search(r"<<<PZDOC_TITLE_[A-F0-9]+>>>", payload)
        assert delimiter is not None
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": (
                        f"{delimiter.group(0)}\nEL MAPA DEL TESORO:\n"
                        f"UN PROGRAMA DE TRANSFORMACIÓN DE 30 DÍAS\n{delimiter.group(0)}"
                    ),
                }
            },
        )

    result = improvement_module.retranslate_residual_title(
        "THE TREASURE MAP: A 30 DAY TRANSFORMATION PROGRAM",
        "THE TREASURE MAP: A 30 DAY TRANSFORMATION PROGRAM",
        LOCAL_SETTINGS,
        "Español",
        source_language_code="en",
        transport=httpx.MockTransport(respond),
    )

    assert result == "EL MAPA DEL TESORO: UN PROGRAMA DE TRANSFORMACIÓN DE 30 DÍAS"


def test_bilingual_title_retranslation_preserves_combined_emphasis() -> None:
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)["messages"][1]["content"]
        requests.append(payload)
        return httpx.Response(200, json={"message": {"content": "EL MAPA DEL TESORO"}})

    result = improvement_module.retranslate_residual_title(
        "***THE TREASURE MAP***",
        "***THE TREASURE MAP***",
        LOCAL_SETTINGS,
        "Español",
        source_language_code="en",
        transport=httpx.MockTransport(respond),
    )

    assert result == "***EL MAPA DEL TESORO***"
    assert all("***" not in request for request in requests)


def test_bilingual_title_retranslation_rejects_an_explanatory_second_line() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": "EL MAPA DEL TESORO\nEsta es la traducción solicitada.",
                }
            },
        )

    source = "THE TREASURE MAP"
    result = improvement_module.retranslate_residual_title(
        source,
        source,
        LOCAL_SETTINGS,
        "Español",
        source_language_code="en",
        transport=httpx.MockTransport(respond),
    )

    assert result == source


def test_bilingual_title_retranslation_rejects_an_explanation_after_a_colon() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": "EL MAPA DEL TESORO:\nEsta es la traducción solicitada.",
                }
            },
        )

    source = "THE TREASURE MAP: A PRACTICAL GUIDE"
    result = improvement_module.retranslate_residual_title(
        source,
        source,
        LOCAL_SETTINGS,
        "Español",
        source_language_code="en",
        transport=httpx.MockTransport(respond),
    )

    assert result == source


def test_focused_title_repair_detaches_a_list_prefix_and_protects_its_folio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str] = []

    def request(
        _client: object,
        _model: str,
        _context_window: int,
        _instructions: str,
        markdown: str,
        _cancellation: object,
        **_kwargs: object,
    ) -> str:
        requests.append(markdown)
        return markdown.replace("APPENDIX CONTENTS", "CONTENIDO DEL APÉNDICE")

    monkeypatch.setattr(improvement_module, "_request_improvement", request)
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )

    result = improvement_module._repair_untranslated_titles(
        object(),
        "model",
        8192,
        "Translate.",
        "- APPENDIX CONTENTS 307",
        "- APPENDIX CONTENTS 307",
        context,
        None,
    )

    assert result == "- CONTENIDO DEL APÉNDICE 307"
    assert len(requests) == 1
    assert not requests[0].startswith("-")
    assert "307" not in requests[0]
    assert "PZDOC" in requests[0]


def test_focused_title_repair_translates_only_one_proven_multiword_residue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str] = []

    def request(
        _client: object,
        _model: str,
        _context_window: int,
        _instructions: str,
        markdown: str,
        _cancellation: object,
        **_kwargs: object,
    ) -> str:
        requests.append(markdown)
        return markdown

    monkeypatch.setattr(improvement_module, "_request_improvement", request)
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )

    result = improvement_module._repair_untranslated_titles(
        object(),
        "model",
        8192,
        "Translate.",
        "## A. CONJUNCTION (sunodos), LYING HIDDEN",
        "## A. CONJUNCIÓN (sunodos), LYING HIDDEN",
        context,
        None,
    )

    assert result == "## A. CONJUNCIÓN (sunodos), OCULTO"
    assert requests == []


def test_bilingual_title_retranslation_prefers_one_proven_multiword_residue() -> None:
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        content = payload["messages"][1]["content"]
        requests.append(content)
        return httpx.Response(200, json={"message": {"content": content}})

    result = improvement_module.retranslate_residual_title(
        "## A. CONJUNCTION (sunodos), LYING HIDDEN",
        "## A. CONJUNCIÓN (sunodos), LYING HIDDEN",
        LOCAL_SETTINGS,
        "Español",
        source_language_code="en",
        transport=httpx.MockTransport(respond),
    )

    assert result == "## A. CONJUNCIÓN (sunodos), OCULTO"
    assert requests == []


def test_dense_index_defers_multiple_title_repairs_to_line_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str] = []
    monkeypatch.setattr(
        improvement_module,
        "_request_improvement",
        lambda *_args, markdown, **_kwargs: requests.append(markdown) or markdown,
    )
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )
    source = "- TAURUS I 69\n- GEMINI I 86\n- SCORPIO I 174"

    with pytest.raises(ImprovementError, match="división por líneas"):
        improvement_module._repair_untranslated_titles(
            object(),
            "model",
            8192,
            "Translate.",
            source,
            source,
            context,
            None,
        )

    assert requests == []


def test_bilingual_residual_title_retry_preserves_list_prefix_and_numbers() -> None:
    requests: list[str] = []
    protected = improvement_module._protect_translation_values(
        "APÉNDICE 5: EDICIONES DE LUJO 427",
        protect_headings=False,
    )
    first_number, last_number = (value.token for value in protected.values)

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        content = payload["messages"][1]["content"]
        requests.append(content)
        return httpx.Response(
            200,
            json={
                "message": {"content": f"APPENDIX {first_number}: LUXURY EDITIONS {last_number}"}
            },
        )

    result = improvement_module.retranslate_residual_title(
        "- **APÉNDICE 5: EDICIONES DE LUJO 427**",
        "- **APPENDIX 5: EDITIONS OF LUJO 427**",
        LOCAL_SETTINGS,
        "Inglés",
        source_language_code="es",
        transport=httpx.MockTransport(respond),
    )

    assert result == "- **APPENDIX 5: LUXURY EDITIONS 427**"
    assert len(requests) == 1
    assert " 5" not in requests[0]
    assert "427" not in requests[0]


def test_translation_protects_formulas_and_reference_identifiers_before_the_request() -> None:
    source = (
        "Energy follows E = mc^2 according to [12-14], "
        "doi:10.1000/XYZ-123 and ISBN 978-1-4028-9462-6."
    )

    protected = improvement_module._protect_translation_values(
        source,
        protect_headings=False,
        protect_paragraphs=False,
    )
    restored = improvement_module._restore_protected_values(
        protected.text,
        protected.values,
    )

    assert restored == source
    assert "E = mc^2" not in protected.text
    assert "[12-14]" not in protected.text
    assert "doi:10.1000/XYZ-123" not in protected.text
    assert "978-1-4028-9462-6" not in protected.text


def test_translation_protects_complete_numeric_footnote_links() -> None:
    source = "The source explains the method.[6](<#page-36>) [7](<#page-36>)"

    protected = improvement_module._protect_translation_values(
        source,
        protect_headings=False,
        protect_paragraphs=False,
    )

    assert {value.value for value in protected.values} == {
        "[6](<#page-36>)",
        "[7](<#page-36>)",
    }
    assert "page-36" not in protected.text
    assert improvement_module._restore_protected_values(protected.text, protected.values) == source


def test_translation_protects_short_foreign_terms_inside_emphasis() -> None:
    source = (
        "The Greek verb *chrēmatizō*, the noun *oikonomia* and *Tetrabiblos* remain exact, "
        "while *business* is ordinary English prose."
    )

    protected = improvement_module._protect_translation_values(
        source,
        protect_headings=False,
        protect_paragraphs=False,
        foreign_emphasis_languages=("en", "es"),
    )
    protected_values = {value.value for value in protected.values}

    assert {"chrēmatizō", "oikonomia", "Tetrabiblos"} <= protected_values
    assert "business" not in protected_values
    assert improvement_module._restore_protected_values(protected.text, protected.values) == source


def test_translation_can_protect_simple_emphasis_delimiters_without_hiding_words() -> None:
    source = (
        "Keep *ordinary prose*, **important guidance**, ~~obsolete wording~~ and "
        "word_with_underscores visible. Do not alter `*literal code*`."
    )

    protected = improvement_module._protect_translation_values(
        source,
        protect_numbers=False,
        protect_headings=False,
        protect_paragraphs=False,
        protect_emphasis=True,
    )

    protected_values = [value.value for value in protected.values]
    assert protected_values.count("*") == 2
    assert protected_values.count("**") == 2
    assert protected_values.count("~~") == 2
    assert "ordinary prose" in protected.text
    assert "important guidance" in protected.text
    assert "obsolete wording" in protected.text
    assert "word_with_underscores" in protected.text
    assert "`*literal code*`" not in protected.text
    emphasis_tokens = [
        value.token for value in protected.values if value.value in {"*", "**", "~~"}
    ]
    assert emphasis_tokens[0].startswith("<PZDOCE")
    assert emphasis_tokens[1] == emphasis_tokens[0].replace("<", "</", 1)
    assert improvement_module._restore_protected_values(protected.text, protected.values) == source


def test_translation_protects_combined_bold_italic_delimiters_as_one_pair() -> None:
    source = "Keep ***important guidance*** and ___a second passage___ visible."

    protected = improvement_module._protect_translation_values(
        source,
        protect_numbers=False,
        protect_headings=False,
        protect_paragraphs=False,
        protect_emphasis=True,
    )

    protected_values = [value.value for value in protected.values]
    assert protected_values.count("***") == 2
    assert protected_values.count("___") == 2
    assert "important guidance" in protected.text
    assert "a second passage" in protected.text
    assert "***" not in protected.text
    assert "___" not in protected.text
    assert improvement_module._restore_protected_values(protected.text, protected.values) == source


def test_translation_protects_nested_mixed_emphasis_delimiters() -> None:
    source = "Keep **_important guidance_** and __*a second passage*__ visible."

    protected = improvement_module._protect_translation_values(
        source,
        protect_numbers=False,
        protect_headings=False,
        protect_paragraphs=False,
        protect_emphasis=True,
    )

    assert "important guidance" in protected.text
    assert "a second passage" in protected.text
    assert improvement_module._restore_protected_values(protected.text, protected.values) == source


def test_translation_protects_a_bare_email_address() -> None:
    source = "Write to **reader@example.com** for assistance."

    protected = improvement_module._protect_translation_values(
        source,
        protect_numbers=False,
        protect_headings=False,
        protect_paragraphs=False,
        protect_emphasis=True,
    )

    assert "reader@example.com" not in protected.text
    assert any(value.value == "reader@example.com" for value in protected.values)
    assert improvement_module._restore_protected_values(protected.text, protected.values) == source


def test_translation_protects_each_repeated_emphasis_span_independently() -> None:
    source = "Read *first work*, *second work* and *third work* in order."

    protected = improvement_module._protect_translation_values(
        source,
        protect_numbers=False,
        protect_headings=False,
        protect_paragraphs=False,
        protect_emphasis=True,
    )

    emphasis_values = [value for value in protected.values if value.value == "*"]
    assert len(emphasis_values) == 6
    assert len({value.token for value in emphasis_values}) == 6
    assert (
        markdown_safety_module.markdown_emphasis_structure(
            protected.text,
            preserve_inline_positions=False,
        )
        == ()
    )
    assert improvement_module._restore_protected_values(protected.text, protected.values) == source


def test_translation_removes_emphasis_added_around_opaque_source_markers() -> None:
    protected_source = "Keep PZDOCAXZQordinary prosePZDOCBXZQ visible."
    response = "Mantén *PZDOCAXZQprosa ordinariaPZDOCBXZQ* visible."

    assert (
        improvement_module._remove_added_simple_translation_emphasis(
            protected_source,
            response,
        )
        == "Mantén PZDOCAXZQprosa ordinariaPZDOCBXZQ visible."
    )


def test_translation_removes_only_spaces_added_inside_opaque_emphasis_markers() -> None:
    source = "Keep *ordinary prose* visible."
    protected = improvement_module._protect_translation_values(
        source,
        protect_numbers=False,
        protect_headings=False,
        protect_paragraphs=False,
        protect_emphasis=True,
    )
    opening, closing = (value.token for value in protected.values)
    response = f"Mantén{opening} prosa ordinaria {closing}visible."

    reconciled = improvement_module._reconcile_protected_emphasis_spacing(
        protected.text,
        response,
        protected.values,
    )

    assert (
        improvement_module._restore_protected_values(
            reconciled,
            protected.values,
        )
        == "Mantén *prosa ordinaria* visible."
    )


def test_translation_protects_macron_transliterations_without_pdf_inline_emphasis() -> None:
    source = "The terms chrēmatizō and chrēmatistikos remain exact in translated prose."

    protected = improvement_module._protect_translation_values(
        source,
        protect_headings=False,
        protect_paragraphs=False,
        foreign_emphasis_languages=("en", "es"),
    )

    assert {value.value for value in protected.values} == {
        "chrēmatizō",
        "chrēmatistikos",
    }
    assert improvement_module._restore_protected_values(protected.text, protected.values) == source


def test_translation_fallback_can_split_sentences_inside_whole_line_emphasis() -> None:
    parts = improvement_module._translation_fallback_parts(
        "*The first sentence needs translation. The second sentence needs translation.*"
    )

    assert "".join(part.text for part in parts) == (
        "*The first sentence needs translation. The second sentence needs translation.*"
    )
    assert [part.text for part in parts if part.should_improve] == [
        "The first sentence needs translation.",
        "The second sentence needs translation.",
    ]


def test_translation_fallback_never_splits_inside_inline_emphasis() -> None:
    source = (
        "Before *the first emphasized sentence. The second remains emphasized.* "
        "An outside sentence follows. A final sentence ends the paragraph."
    )

    parts = improvement_module._translation_fallback_parts(source)

    assert "".join(part.text for part in parts) == source
    assert all(part.text.count("*") % 2 == 0 for part in parts)
    inside_emphasis = source.index(". The second") + 1
    assert not improvement_module._is_safe_markdown_boundary(source, inside_emphasis)


def test_translation_fallback_can_split_between_sequential_emphasis_spans() -> None:
    source = (
        "Compare *the first complete phrase* with *the second complete phrase* and "
        "*the third complete phrase* before *the fourth complete phrase* and "
        "*the fifth complete phrase*"
    )

    parts = improvement_module._translation_fallback_parts(source)

    assert "".join(part.text for part in parts) == source
    assert len([part for part in parts if part.should_improve]) == 5
    assert all(part.text.count("*") == 2 for part in parts if part.should_improve)
    assert all(part.text.isspace() for part in parts if not part.should_improve)


def test_translation_fallback_splits_a_long_single_sentence_at_safe_clauses() -> None:
    source = (
        "In this system the first house begins at the degree of the Ascendant, "
        "each following house uses the same interval for its boundary, "
        "and the remaining divisions continue around the complete chart without omission"
    )

    parts = improvement_module._translation_fallback_parts(source)

    assert "".join(part.text for part in parts) == source
    assert len([part for part in parts if part.should_improve]) >= 2


def test_translation_fallback_recursively_splits_dense_protected_emphasis() -> None:
    dense_paragraph = (
        "Read *the first important passage* at 10 degrees, then compare "
        "*the second useful passage* and *the third complete passage*. "
        "Study *the fourth careful passage* before reviewing *the fifth final passage*."
    )
    source = f"{dense_paragraph}\n\nA short conclusion explains the method clearly."
    requests: list[str] = []
    word_translations = {
        "A": "Una",
        "Read": "Lee",
        "Study": "Estudia",
        "and": "y",
        "at": "a",
        "before": "antes",
        "careful": "cuidadoso",
        "clearly": "claramente",
        "compare": "compara",
        "complete": "completo",
        "conclusion": "conclusión",
        "degrees": "grados",
        "explains": "explica",
        "fifth": "quinto",
        "final": "final",
        "first": "primer",
        "fourth": "cuarto",
        "important": "importante",
        "method": "método",
        "of": "de",
        "passage": "pasaje",
        "reviewing": "revisar",
        "second": "segundo",
        "short": "breve",
        "the": "el",
        "then": "luego",
        "third": "tercer",
        "useful": "útil",
    }

    def respond(request: httpx.Request) -> httpx.Response:
        content = json.loads(request.content)["messages"][1]["content"]
        requests.append(content)
        protected_markers = re.findall(r"</?PZDOCE[A-Z]+XZQ>", content)
        if len(protected_markers) > MAX_TRANSLATION_PROTECTED_VALUES_PER_CHUNK:
            return httpx.Response(
                200,
                json={"message": {"content": content.replace(protected_markers[-1], "", 1)}},
            )
        translated = re.sub(
            r"[A-Za-z]+",
            lambda match: (
                match.group(0)
                if match.group(0).startswith("PZDOC")
                else word_translations[match.group(0)]
            ),
            content,
        )
        return httpx.Response(200, json={"message": {"content": translated}})

    context = improvement_module._TranslationContext("en", "es", True)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        translated = improvement_module._improve_translation_segments(
            client,
            "parsezen-local",
            8_192,
            build_instructions(ImprovementMode.TRANSLATE, "Español"),
            source,
            context,
            None,
        )

    assert translated.count("*") == source.count("*")
    assert "passage" not in translated
    assert context.preserved_segments == []
    assert any(
        len(re.findall(r"</?PZDOCE[A-Z]+XZQ>", content))
        > MAX_TRANSLATION_PROTECTED_VALUES_PER_CHUNK
        for content in requests
    )
    assert any(
        0
        < len(re.findall(r"</?PZDOCE[A-Z]+XZQ>", content))
        <= MAX_TRANSLATION_PROTECTED_VALUES_PER_CHUNK
        for content in requests
    )


def test_bilingual_residual_title_retries_once_when_source_words_remain() -> None:
    protected = improvement_module._protect_translation_values(
        "APÉNDICE 5: EDICIONES DE LUJO 427",
        protect_headings=False,
    )
    first_number, last_number = (value.token for value in protected.values)
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        title = (
            f"APPENDIX {first_number}: EDITIONS OF LUJO {last_number}"
            if calls == 1
            else "APPENDIX 5: LUXURY EDITIONS 427"
        )
        return httpx.Response(200, json={"message": {"content": title}})

    result = improvement_module.retranslate_residual_title(
        "- **APÉNDICE 5: EDICIONES DE LUJO 427**",
        "- **APPENDIX 5: EDITIONS OF LUJO 427**",
        LOCAL_SETTINGS,
        "Inglés",
        source_language_code="es",
        transport=httpx.MockTransport(respond),
    )

    assert result == "- **APPENDIX 5: LUXURY EDITIONS 427**"
    assert calls == 2


def test_translation_repairs_an_unchanged_markdown_heading_without_exposing_its_prefix() -> None:
    requests: list[str] = []
    paragraph = (
        "This English paragraph contains enough natural language to establish the source language."
    )

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        content = payload["messages"][1]["content"]
        requests.append(content)
        if content == "THE HOUSES OF ASTROLOGY" and requests.count(content) == 2:
            translated = "LAS CASAS DE LA ASTROLOGÍA"
        elif content == paragraph:
            translated = (
                "Este párrafo en inglés contiene suficiente lenguaje natural para establecer "
                "el idioma de origen."
            )
        else:
            translated = content
        return httpx.Response(200, json={"message": {"content": translated}})

    result = improve_markdown(
        f"# THE HOUSES OF ASTROLOGY\n\n{paragraph}",
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result.startswith("# LAS CASAS DE LA ASTROLOGÍA\n\nEste párrafo")
    assert requests[:2] == ["THE HOUSES OF ASTROLOGY", "THE HOUSES OF ASTROLOGY"]


def test_translation_planner_keeps_short_title_runs_focused() -> None:
    source = (
        "30 DAY MILLIONAIRE CHALLENGE\n\n# First chapter\n\nA normal paragraph follows the title."
    )

    parts = improvement_module._plan_markdown_parts(
        source,
        max_characters=MAX_TRANSLATION_CHUNK_CHARACTERS,
        protect_paragraphs=True,
    )

    assert [part.text for part in parts if part.should_improve] == [
        "30 DAY MILLIONAIRE CHALLENGE",
        "# First chapter",
        "A normal paragraph follows the title.",
    ]


def test_translation_planner_never_sends_standalone_html_comments_to_the_model() -> None:
    marker = "<!-- PZDOC PDF PAGE 1 -->"
    source = f"{marker}\n\n# First chapter\n\nA normal paragraph follows the title."

    parts = improvement_module._plan_markdown_parts(
        source,
        max_characters=MAX_TRANSLATION_CHUNK_CHARACTERS,
        protect_paragraphs=True,
    )

    assert [part.text for part in parts if not part.should_improve] == [marker]
    assert [part.text for part in parts if part.should_improve] == [
        "# First chapter",
        "A normal paragraph follows the title.",
    ]


def test_translation_planner_batches_a_long_title_run() -> None:
    source = "\n\n".join(
        (
            "# FIRST CHAPTER",
            "# SECOND CHAPTER",
            "# THIRD CHAPTER",
            "# FOURTH CHAPTER",
        )
    )

    parts = improvement_module._plan_markdown_parts(
        source,
        max_characters=MAX_TRANSLATION_CHUNK_CHARACTERS,
        protect_paragraphs=True,
    )

    assert [part.text for part in parts if part.should_improve] == [source]


def test_translation_preserves_uppercase_inside_a_batched_title_run() -> None:
    source = "# FIRST CHAPTER\n\n# SECOND CHAPTER\n\n# THIRD CHAPTER"

    def respond(request: httpx.Request) -> httpx.Response:
        content = json.loads(request.content)["messages"][1]["content"]
        translated = (
            content.replace("FIRST CHAPTER", "primer capítulo")
            .replace("SECOND CHAPTER", "segundo capítulo")
            .replace("THIRD CHAPTER", "tercer capítulo")
        )
        return httpx.Response(
            200,
            json={"message": {"content": translated}},
        )

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == "# PRIMER CAPÍTULO\n\n# SEGUNDO CAPÍTULO\n\n# TERCER CAPÍTULO"


def test_translation_planner_keeps_a_publisher_name_literal() -> None:
    source = "THREE HANDS PRESS\n\nA normal English paragraph follows the publisher name."

    parts = improvement_module._plan_markdown_parts(
        source,
        max_characters=MAX_TRANSLATION_CHUNK_CHARACTERS,
        protect_paragraphs=True,
    )

    assert [part.text for part in parts if not part.should_improve] == ["THREE HANDS PRESS"]
    assert [part.text for part in parts if part.should_improve] == [
        "A normal English paragraph follows the publisher name."
    ]


def test_translation_planner_keeps_repeated_uppercase_person_names_literal() -> None:
    source = (
        "# CHRIS BRENNAN\n\n"
        "A normal English paragraph follows the first byline.\n\n"
        "## CHRIS BRENNAN"
    )

    parts = improvement_module._plan_markdown_parts(
        source,
        max_characters=MAX_TRANSLATION_CHUNK_CHARACTERS,
        protect_paragraphs=True,
    )

    assert [part.text for part in parts if not part.should_improve] == [
        "# CHRIS BRENNAN",
        "## CHRIS BRENNAN",
    ]
    assert [part.text for part in parts if part.should_improve] == [
        "A normal English paragraph follows the first byline."
    ]


def test_translation_preserves_numbers_and_level_in_a_markdown_heading() -> None:
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        protected = payload["messages"][1]["content"]
        requests.append(protected)
        return httpx.Response(
            200,
            json={"message": {"content": protected.replace("DAYS", "DÍAS")}},
        )

    result = improve_markdown(
        "# DAYS6+7",
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == "# DÍAS6+7"
    assert len(requests) == 1
    assert not requests[0].startswith("#")
    assert "6" in requests[0]
    assert "7" in requests[0]


def test_translation_protects_the_folio_of_a_single_line_toc_entry() -> None:
    requests: list[str] = []
    instructions: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        protected = payload["messages"][1]["content"]
        requests.append(protected)
        instructions.append(payload["messages"][0]["content"])
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": protected.replace(
                        "LUXURY EDITIONS",
                        "EDICIONES DE LUJO",
                    )
                }
            },
        )

    result = improve_markdown(
        "- LUXURY EDITIONS 427\n",
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == "- EDICIONES DE LUJO 427"
    assert len(requests) == 1
    assert requests[0].startswith("LUXURY EDITIONS")
    assert "427" not in requests[0]
    assert "PZDOC" in requests[0]
    assert "signos" in instructions[0]
    assert "zodiacales" in instructions[0]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("- TAURUS I 69", "- TAURO I 69"),
        ("- PISCES III 258", "- PISCIS III 258"),
        ("- VIRGO II 156", "- VIRGO II 156"),
    ],
)
def test_translation_uses_established_index_classifications_without_model_risk(
    source: str,
    expected: str,
) -> None:
    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("The standard classification must not reach the model.")

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        source_language_code="en",
        transport=httpx.MockTransport(unexpected_request),
    )

    assert result == expected


def test_translation_restores_established_classifications_after_model_hallucination() -> None:
    source = "- VIRGO I 138\n- VIRGO II 144\n- VIRGO III 150"
    proposed = "- TÁRIGO I 138\n- TÁRIGO II 144\n- TURIA III 150"
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )

    restored = improvement_module._restore_established_index_classifications(
        source,
        proposed,
        context,
    )

    assert restored == source


def test_translation_localizes_an_established_term_copied_inside_a_toc_entry() -> None:
    source = "- 1. Main Planetary Rulership Schemes 274"
    proposed = "- 1. Esquemas de Rulership Planetaria Principal 274"
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )

    restored = improvement_module._restore_established_index_classifications(
        source,
        proposed,
        context,
    )

    assert restored == "- 1. Esquemas de regencia Planetaria Principal 274"


def test_translation_restores_an_exact_emphasized_running_title() -> None:
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )

    restored = improvement_module._restore_established_index_classifications(
        "*Part One*\n",
        "*ParteUno*\n",
        context,
    )

    assert restored == "*Parte uno*\n"


def test_translation_repairs_established_classifications_loaded_from_cache() -> None:
    source = "- VIRGO I 138\n- VIRGO II 144\n- VIRGO III 150"
    cached = "- TÁRIGO I 138\n- TÁRIGO II 144\n- TURIA III 150"
    saved: list[str] = []

    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("A repairable cache entry must not call the model.")

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        source_language_code="en",
        transport=httpx.MockTransport(unexpected_request),
        load_checkpoint=lambda _key: cached,
        save_checkpoint=lambda _key, value: saved.append(value) or True,
    )

    assert result == source
    assert saved == [source]


def test_translation_upgrades_copied_conventions_loaded_from_cache() -> None:
    source = "During the 3rd century BC, Taurus entered the mainstream of practice."
    cached = "Durante el siglo 3rd BC, Taurus entró en el mainstream de la práctica."
    saved: list[str] = []

    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("A repairable cache entry must not call the model.")

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        source_language_code="en",
        transport=httpx.MockTransport(unexpected_request),
        load_checkpoint=lambda _key: cached,
        save_checkpoint=lambda _key, value: saved.append(value) or True,
    )

    expected = "Durante el siglo 3.º a. C., Tauro entró en el ámbito general de la práctica."
    assert result == expected
    assert saved == [expected]


def test_translation_restores_a_plain_marker_wrapped_in_an_html_comment() -> None:
    source = "# DAYS6+7"
    protected = improvement_module._protect_translation_values(
        source,
        protect_headings=False,
    )
    response = protected.text
    for value in protected.values:
        response = response.replace(value.token, f"<!-- {value.token} -->")

    restored = improvement_module._restore_protected_values(response, protected.values)

    assert restored == source


def test_translation_protects_roman_numerals_used_in_index_references() -> None:
    source = "**LIBRA III 168**"
    protected = improvement_module._protect_translation_values(source)

    assert " III " not in protected.text
    assert any(value.value == "III" for value in protected.values)

    restored = improvement_module._restore_protected_values(
        protected.text.replace("LIBRA", "BALANZA"),
        protected.values,
    )

    assert restored == "**BALANZA III 168**"


def test_translation_repairs_one_unambiguous_internal_marker_typo() -> None:
    source = "Published in 1809-1822 and illustrated on page 23."
    protected = improvement_module._protect_translation_values(source)
    values = {value.value: value for value in protected.values}
    damaged = protected.text.replace(
        values["1809-1822"].token,
        f"{values['1809-1822'].token[:-1]}-",
    )

    restored = improvement_module._restore_protected_values(damaged, protected.values)

    assert restored == source


def test_translation_does_not_guess_an_ambiguous_internal_marker() -> None:
    source = "Values 10 and 20 remain exact."
    protected = improvement_module._protect_translation_values(source)
    first, second = protected.values
    ambiguous = protected.text.replace(first.token, second.token)

    with pytest.raises(ImprovementError, match="valor protegido"):
        improvement_module._restore_protected_values(ambiguous, protected.values)


def test_translation_removes_only_instruction_placeholder_comments_added_by_model() -> None:
    source = "Existing <!-- PZDOC... --> comment."
    response = "Existing <!-- PZDOC... --> comment.<!-- PZDOC... -->"

    repaired = improvement_module._remove_added_instruction_placeholders(source, response)

    assert repaired == source


@pytest.mark.parametrize(
    "added_placeholder",
    ("<PZDOC>", "</PZDOC>", "<PZDOC />", "&lt;PZDOC&gt;"),
)
def test_translation_removes_instruction_placeholder_tags_added_by_model(
    added_placeholder: str,
) -> None:
    source = "Texto sin etiquetas técnicas."
    response = f"{source}{added_placeholder}"

    repaired = improvement_module._remove_added_instruction_placeholders(source, response)

    assert repaired == source


def test_translation_preserves_only_the_instruction_placeholder_tags_from_source() -> None:
    source = "Existing <PZDOC> label."
    response = f"{source}<PZDOC>"

    repaired = improvement_module._remove_added_instruction_placeholders(source, response)

    assert repaired == source


def test_translation_validation_rejects_a_cached_instruction_placeholder_tag() -> None:
    with pytest.raises(ImprovementError, match="marcador técnico"):
        improvement_module._validate_mode_output(
            "Source paragraph.",
            "Párrafo traducido.<PZDOC>",
            ImprovementMode.TRANSLATE,
        )


def test_translation_allows_private_image_alt_text_to_be_translated() -> None:
    improvement_module._validate_mode_output(
        "![Cover](__parsezen_resources__/pdf/cover.jpg)",
        "![Portada](__parsezen_resources__/pdf/cover.jpg)",
        ImprovementMode.TRANSLATE,
    )


def test_translation_cannot_turn_a_private_image_into_a_link() -> None:
    with pytest.raises(ImprovementError, match="imagen privada"):
        improvement_module._validate_mode_output(
            "![Cover](__parsezen_resources__/pdf/cover.jpg)",
            "[Portada](__parsezen_resources__/pdf/cover.jpg)",
            ImprovementMode.TRANSLATE,
        )


def test_combined_mode_uses_one_operation_per_safe_fragment() -> None:
    source = "# Price\n\nValue 10 on 2026-07-17. [Site](https://example.com)"
    improved = "# Precio\n\nValor 10 el 2026-07-17. [Sitio](https://example.com)"
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        protected = payload["messages"][1]["content"]
        changed = (
            protected.replace("Price", "Precio")
            .replace("Value", "Valor")
            .replace(" on ", " el ")
            .replace("[Site]", "[Sitio]")
        )
        return httpx.Response(
            200,
            json={"message": {"content": changed}},
        )

    result = improve_markdown(
        source,
        ImprovementMode.CLEAN_AND_TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == improved
    assert len(requests) == 2
    assert all(request.url == "http://127.0.0.1:11434/api/chat" for request in requests)
    heading_payload = json.loads(requests[0].content)
    payload = json.loads(requests[1].content)
    assert payload["model"] == "parsezen-local"
    assert payload["stream"] is True
    assert payload["think"] is False
    assert payload["options"] == {
        "temperature": 0,
        "num_ctx": 8_192,
        "num_predict": transport_module.prediction_token_limit(
            len(source.split("\n\n", 1)[1]),
            8_192,
        ),
        "seed": 0,
    }
    assert payload["messages"][1]["role"] == "user"
    protected_markdown = payload["messages"][1]["content"]
    assert "Value" in protected_markdown
    assert "10" not in protected_markdown
    assert "2026-07-17" not in protected_markdown
    assert "https://example.com" not in protected_markdown
    assert protected_markdown.count("PZDOC") == 3
    assert heading_payload["messages"][1]["content"] == "Price"
    assert "exclusivamente un título" in heading_payload["messages"][0]["content"]
    assert all(
        "única transformación" in json.loads(request.content)["messages"][0]["content"]
        for request in requests
    )


def test_missing_model_is_rejected_before_any_request() -> None:
    called = False

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    with pytest.raises(SettingsError, match="Elige un modelo"):
        improve_markdown(
            "Content",
            ImprovementMode.CLEAN,
            AppSettings(),
            transport=httpx.MockTransport(respond),
        )

    assert not called


@pytest.mark.parametrize("model", ["glm-4.7:cloud", "gpt-oss:120b-cloud"])
def test_cloud_models_are_rejected_before_any_document_request(model: str) -> None:
    called = False

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    with pytest.raises(SettingsError, match="almacenados localmente"):
        improve_markdown(
            "Content",
            ImprovementMode.CLEAN,
            AppSettings(model=model),
            transport=httpx.MockTransport(respond),
        )

    assert not called


def test_redirect_to_a_remote_server_is_not_followed() -> None:
    requests = 0

    def redirect(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(307, headers={"Location": "https://example.com/steal"})

    with pytest.raises(ImprovementError, match="redirigir"):
        improve_markdown(
            "Content",
            ImprovementMode.CLEAN,
            LOCAL_SETTINGS,
            transport=httpx.MockTransport(redirect),
        )

    assert requests == 1


def test_unavailable_local_server_has_a_specific_error() -> None:
    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with pytest.raises(LocalModelUnavailableError, match="servidor esté iniciado"):
        improve_markdown(
            "Content",
            ImprovementMode.CLEAN,
            LOCAL_SETTINGS,
            transport=httpx.MockTransport(unavailable),
        )


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("empty", "vacía"),
        ("nul", "caracteres"),
    ],
)
def test_rejects_unsafe_model_output(mutation: str, expected_error: str) -> None:
    source = "Price 10 [Site](https://example.com)"

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        response_content = payload["messages"][1]["content"]
        if mutation == "empty":
            response_content = "   "
        else:
            response_content += "\0"
        return httpx.Response(
            200,
            json={"message": {"content": response_content}},
        )

    with pytest.raises(ImprovementError, match=expected_error):
        improve_markdown(
            source,
            ImprovementMode.CLEAN,
            LOCAL_SETTINGS,
            transport=httpx.MockTransport(respond),
        )


def test_preserves_the_original_chunk_when_cleaning_changes_protected_values() -> None:
    source = "Price 10 on 2026-07-17. [Site](https://example.com)"
    changed = "Precio 11 el 2027-08-18. [Sitio](https://changed.example.com)"
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"message": {"content": changed}})
    )

    result = improve_markdown(
        source,
        ImprovementMode.CLEAN,
        LOCAL_SETTINGS,
        transport=transport,
    )

    assert result == source


def test_translation_keeps_reordered_numbers_and_links_attached_to_their_markers() -> None:
    source = "[Alice](https://a.example) has 10 apples. [Bob](https://b.example) has 20 pears."

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        protected = payload["messages"][1]["content"]
        tokens = re.findall(r"PZDOC[A-Z]+XZQ", protected)
        translated = (
            f"[Bob]({tokens[2]}) tiene {tokens[3]} peras. "
            f"[Alice]({tokens[0]}) tiene {tokens[1]} manzanas."
        )
        return httpx.Response(200, json={"message": {"content": translated}})

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == (
        "[Bob](https://b.example) tiene 20 peras. [Alice](https://a.example) tiene 10 manzanas."
    )


def test_rejects_incompatible_ollama_json() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"result": "no"}))

    with pytest.raises(ImprovementError, match="incompatible"):
        improve_markdown(
            "Content",
            ImprovementMode.CLEAN,
            LOCAL_SETTINGS,
            transport=transport,
        )


def test_long_document_is_improved_in_ordered_structural_chunks() -> None:
    block = ("Texto conservador para comprobar la división estructural. " * 32).strip()
    source = "\n\n".join(f"## Sección\n\n{block}" for _ in range(7))
    requests: list[str] = []
    progress: list[tuple[int, int]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        chunk = payload["messages"][1]["content"]
        requests.append(chunk)
        return httpx.Response(200, json={"message": {"content": chunk}})

    result = improve_markdown(
        source,
        ImprovementMode.CLEAN,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
        on_progress=lambda current, total: progress.append((current, total)),
    )

    assert result == source
    assert len(requests) >= 2
    assert all(len(chunk) <= MAX_INPUT_CHARACTERS for chunk in requests)
    assert progress == [(index, len(requests)) for index in range(1, len(requests) + 1)]


def test_tables_are_never_split_between_model_requests() -> None:
    long_paragraph = "palabra " * 995
    table = "| Nombre | Estado |\n| --- | --- |\n| Parsezen | Listo |"
    source = f"{long_paragraph}\n\n{table}\n\nTexto posterior."
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        chunk = payload["messages"][1]["content"]
        requests.append(chunk)
        return httpx.Response(200, json={"message": {"content": chunk}})

    improve_markdown(
        source,
        ImprovementMode.CLEAN,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
    )

    assert len(requests) >= 2
    assert sum(table in chunk for chunk in requests) == 1


def test_structure_review_preserves_an_oversized_table_without_sending_it_to_model() -> None:
    rows = "\n".join(
        f"| Entry {index} | This complete indexed description remains unchanged. |"
        for index in range(180)
    )
    table = f"| Name | Description |\n| --- | --- |\n{rows}"
    source = f"Chapter One\n\n{table}\n\nChapter Two"
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        numbered = payload["messages"][1]["content"]
        requests.append(numbered)
        assert "| --- | --- |" not in numbered
        return httpx.Response(200, json={"message": {"content": "PZL1=1"}})

    result = improve_markdown(
        source,
        ImprovementMode.REVIEW_STRUCTURE,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
    )

    assert len(table) > MAX_INPUT_CHARACTERS
    assert len(requests) == 1
    assert "PZL1 CANDIDATA" in requests[0]
    assert "PZL186 CANDIDATA" in requests[0]
    assert "rol=front_matter" in requests[0]
    assert result == f"# Chapter One\n\n{table}\n\nChapter Two"


def test_structure_review_plans_from_toc_page_and_semantic_role_globally() -> None:
    source = (
        "<!-- PZDOC PDF PAGE 1 -->\n\n"
        "# Contents\n\n"
        "Chapter One .... 3\n\n"
        "<!-- PZDOC PDF PAGE 3 -->\n\n"
        "Chapter One\n\n"
        "This paragraph explains the chapter without changing its words.\n"
    )
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        inventory = payload["messages"][1]["content"]
        requests.append(inventory)
        body_record = next(
            line
            for line in inventory.splitlines()
            if "página=3" in line and line.endswith(": Chapter One")
        )
        assert "índice=sí" in body_record
        assert "rol=" in body_record
        assert "Chapter One .... 3" not in inventory
        line_number = re.match(r"PZL(\d+)", body_record)
        assert line_number is not None
        return httpx.Response(
            200,
            json={"message": {"content": f"PZL{line_number.group(1)}=1"}},
        )

    result = improve_markdown(
        source,
        ImprovementMode.REVIEW_STRUCTURE,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
    )

    assert len(requests) == 1
    assert "# Contents" not in requests[0]
    assert "Chapter One .... 3" not in requests[0]
    assert result == source.replace("\nChapter One\n\nThis", "\n# Chapter One\n\nThis")


def test_global_structure_inventory_prioritizes_and_bounds_large_outlines() -> None:
    source = "\n\n".join(f"# Heading {index}" for index in range(180))

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        inventory = payload["messages"][1]["content"]
        records = inventory.splitlines()
        assert len(records) == improvement_module.MAX_GLOBAL_STRUCTURE_CANDIDATES
        assert records[0].startswith("PZL1 CANDIDATA")
        assert records[-1].endswith(": # Heading 179")
        return httpx.Response(200, json={"message": {"content": "\n"}})

    result = improve_markdown(
        source,
        ImprovementMode.REVIEW_STRUCTURE,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
    )

    assert result == source


def test_global_structure_ignores_short_body_lines_without_outline_evidence() -> None:
    assert improvement_module._global_structure_candidates("Alpha\nBeta\nGamma") == ()


def test_global_structure_ignores_isolated_body_fragments_without_outline_evidence() -> None:
    source = (
        "La intención es una decisión y al mismo tiempo\n\n"
        "No te limites a visualizar la forma de pensamiento, sino\n\n"
        "Cuando te encuentres en una situación negativa, recuerda"
    )

    assert improvement_module._global_structure_candidates(source) == ()


@pytest.mark.parametrize("response", ("respuesta libre", "PZL999=1", "PZL1=3"))
def test_global_structure_preserves_document_on_invalid_or_unsafe_directives(
    response: str,
) -> None:
    source = "# Existing title\n\nBody paragraph."

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": response}})

    result = improve_markdown(
        source,
        ImprovementMode.REVIEW_STRUCTURE,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
    )

    assert result == source


def test_translation_changes_only_markdown_table_cells() -> None:
    source = (
        "| Topic | Description |\n"
        "| --- | --- |\n"
        "| First house | This section explains the first astrological house clearly. |\n"
        "| Second house | This section explains the second astrological house clearly. |"
    )

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        content = payload["messages"][1]["content"]
        assert "|" not in content
        translations = {
            "Topic": "Tema",
            "Description": "Descripción",
            "First house": "Primera casa",
            "This section explains the first astrological house clearly.": (
                "Esta sección explica claramente la primera casa astrológica."
            ),
            "Second house": "Segunda casa",
            "This section explains the second astrological house clearly.": (
                "Esta sección explica claramente la segunda casa astrológica."
            ),
        }
        items = json.loads(content)["items"]
        translated = {
            "items": [{"id": item["id"], "text": translations[item["text"]]} for item in items]
        }
        return httpx.Response(
            200,
            json={"message": {"content": json.dumps(translated)}},
        )

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == (
        "| Tema | Descripción |\n"
        "| --- | --- |\n"
        "| Primera casa | Esta sección explica claramente la primera casa astrológica. |\n"
        "| Segunda casa | Esta sección explica claramente la segunda casa astrológica. |"
    )


def test_fenced_code_is_preserved_without_sending_it_to_the_model() -> None:
    code = "```python\nvalue = 12\n\nprint(value)\n```"
    source = f"Texto anterior.\n\n{code}\n\nTexto posterior."
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        chunk = payload["messages"][1]["content"]
        requests.append(chunk)
        return httpx.Response(200, json={"message": {"content": chunk}})

    result = improve_markdown(
        source,
        ImprovementMode.CLEAN,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
    )

    assert code in result
    assert len(requests) == 2
    assert all("value = 12" not in chunk for chunk in requests)


def test_code_only_document_does_not_need_a_model_request() -> None:
    source = "```python\nprint('local')\n```"
    transport = httpx.MockTransport(lambda _request: pytest.fail("Unexpected request"))

    result = improve_markdown(
        source,
        ImprovementMode.CLEAN,
        LOCAL_SETTINGS,
        transport=transport,
    )

    assert result == source


def test_rejects_an_oversized_individual_block_before_any_request() -> None:
    called = False

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    with pytest.raises(ImprovementError, match="bloque Markdown"):
        improve_markdown(
            "x" * (MAX_INPUT_CHARACTERS + 1),
            ImprovementMode.CLEAN,
            LOCAL_SETTINGS,
            transport=httpx.MockTransport(respond),
        )

    assert not called


def test_splits_an_oversized_prose_block_at_safe_boundaries() -> None:
    source = ("Sentence with useful text. " * 700).strip()
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        chunk = payload["messages"][1]["content"]
        requests.append(chunk)
        return httpx.Response(200, json={"message": {"content": chunk}})

    result = improve_markdown(
        source,
        ImprovementMode.CLEAN,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
    )

    assert result == source
    assert len(requests) >= 2
    assert all(len(chunk) <= MAX_INPUT_CHARACTERS for chunk in requests)


def test_translation_planner_splits_long_prose_across_ordinary_parentheses() -> None:
    source = "(Editorial aside " + ("with natural language words " * 120).strip() + ")"

    parts = improvement_module._plan_markdown_parts(
        source,
        max_characters=MAX_TRANSLATION_CHUNK_CHARACTERS,
        protect_paragraphs=True,
    )

    assert len(parts) >= 2
    assert "".join(part.separator_before + part.text for part in parts) == source
    assert all(len(part.text) <= MAX_TRANSLATION_CHUNK_CHARACTERS for part in parts)


def test_translation_planner_splits_number_dense_text_with_unmatched_parenthesis() -> None:
    source = "(Chart row " + " ".join(f"Value {index}" for index in range(30))

    parts = improvement_module._plan_markdown_parts(
        source,
        max_characters=MAX_TRANSLATION_CHUNK_CHARACTERS,
        protect_paragraphs=True,
    )

    assert len(parts) >= 3
    assert "".join(part.separator_before + part.text for part in parts) == source
    assert all(
        len(NUMBER_PATTERN.findall(part.text)) <= MAX_TRANSLATION_PROTECTED_VALUES_PER_CHUNK
        for part in parts
    )


def test_translation_planner_keeps_large_markdown_table_as_one_safe_unit() -> None:
    rows = "\n".join(f"| {index} | The First House |" for index in range(60))
    source = f"| 1 | 2 |\n| --- | --- |\n{rows}"

    parts = improvement_module._plan_markdown_parts(
        source,
        max_characters=MAX_TRANSLATION_CHUNK_CHARACTERS,
        protect_paragraphs=True,
    )

    assert len(source) > MAX_TRANSLATION_CHUNK_CHARACTERS
    assert len(parts) == 1
    assert parts[0].text == source
    assert parts[0].should_improve is True


def test_translates_markdown_table_cells_without_changing_its_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "| Topic | Meaning |\n| :--- | ---: |\n| Bound rulerships | General description |"
    translations = {
        "Topic": "Tema",
        "Meaning": "Significado",
        "Bound rulerships": "Regencias vinculadas",
        "General description": "Descripción general",
    }

    def translate_cells(*args: object, **_kwargs: object) -> str:
        values = str(args[4]).split("\n\n")
        return "\n\n".join(translations[value] for value in values)

    monkeypatch.setattr(improvement_module, "_translate_table_text_batch", translate_cells)
    monkeypatch.setattr(
        improvement_module,
        "_validate_mode_output",
        lambda *_args, **_kwargs: pytest.fail(
            "Una tabla reconstruida por celdas no debe repetir la validación numérica global."
        ),
    )
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )
    with httpx.Client() as client:
        translated = improvement_module._translate_safe_markdown_table(
            client,
            "parsezen-local",
            8_192,
            "Translate",
            source,
            context,
            None,
        )

    assert translated == (
        "| Tema | Significado |\n| :--- | ---: |\n| regencias por término | Descripción general |"
    )
    assert context.preserved_segments == []


def test_splits_a_number_dense_index_without_changing_it() -> None:
    source = "\n".join(f"Chapter {index} {100 + index}" for index in range(60))
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        chunk = payload["messages"][1]["content"]
        requests.append(chunk)
        return httpx.Response(200, json={"message": {"content": chunk}})

    result = improve_markdown(
        source,
        ImprovementMode.CLEAN,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
    )

    assert result == source
    assert len(requests) >= 5
    assert all(
        len(NUMBER_PATTERN.findall(chunk)) <= MAX_CONSERVED_VALUES_PER_CHUNK for chunk in requests
    )


def test_translation_planner_limits_all_protected_values_including_paragraphs() -> None:
    source = "\n\n".join(
        f"Paragraph {index} contains enough natural language for a complete translation."
        for index in range(60)
    )

    parts = improvement_module._plan_markdown_parts(
        source,
        max_characters=MAX_TRANSLATION_CHUNK_CHARACTERS,
        protect_paragraphs=True,
    )
    translated_parts = [part for part in parts if part.should_improve]

    assert len(translated_parts) >= 4
    assert all(
        len(improvement_module._protect_translation_values(part.text).values)
        <= MAX_TRANSLATION_PROTECTED_VALUES_PER_CHUNK
        for part in translated_parts
    )


def test_repairs_changed_heading_levels_when_heading_count_is_preserved() -> None:
    source = "# Main title\n\n## Section\n\nText remains."

    def respond(_request: httpx.Request) -> httpx.Response:
        changed = "## Main Title\n\n### Section\n\nText remains."
        return httpx.Response(200, json={"message": {"content": changed}})

    result = improve_markdown(
        source,
        ImprovementMode.CLEAN,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
    )

    assert result == "# Main Title\n\n## Section\n\nText remains."


def test_removes_headings_invented_in_a_fragment_without_headings() -> None:
    source = "Estado de referencia 58\nCambiar el programa 63"
    changed = "### Estado de referencia 58\n### Cambiar el programa 63"
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"message": {"content": changed}})
    )

    result = improve_markdown(
        source,
        ImprovementMode.CLEAN,
        LOCAL_SETTINGS,
        transport=transport,
    )

    assert result == source


def test_retries_once_when_the_model_removes_a_heading() -> None:
    source = "# Main title\n\n## Section\n\nText remains."
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            changed = "# Main title\n\nSection\n\nText remains."
        else:
            payload = json.loads(request.content)
            changed = payload["messages"][1]["content"]
        return httpx.Response(200, json={"message": {"content": changed}})

    result = improve_markdown(
        source,
        ImprovementMode.CLEAN,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
    )

    assert result == source
    assert calls == 2


def test_rejects_a_document_above_the_global_limit_before_any_request() -> None:
    called = False

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    with pytest.raises(ImprovementError, match="límite"):
        improve_markdown(
            "x" * (MAX_DOCUMENT_CHARACTERS + 1),
            ImprovementMode.CLEAN,
            LOCAL_SETTINGS,
            transport=httpx.MockTransport(respond),
        )

    assert not called


def test_cleanup_preserves_a_failing_chunk_without_losing_content() -> None:
    source = f"{'texto ' * 1332}\n\nTotal 10."
    requests = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        payload = json.loads(request.content)
        chunk = payload["messages"][1]["content"]
        if requests >= 2:
            chunk = chunk.replace("10", "", 1)
        return httpx.Response(200, json={"message": {"content": chunk}})

    result = improve_markdown(
        source,
        ImprovementMode.CLEAN,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
    )

    assert result == source


def test_content_review_locks_internal_markers_and_link_destinations() -> None:
    marker = "<!-- PZDOC-PAGE: 1 -->"
    source = f"{marker}\n\n[Sitio](https://example.com) contiene un eror claro."
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        protected = payload["messages"][1]["content"]
        requests.append(protected)
        return httpx.Response(
            200,
            json={"message": {"content": protected.replace("un eror", "un error")}},
        )

    result = improve_markdown(
        source,
        ImprovementMode.REVIEW_CONTENT,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
    )

    assert result == source.replace("un eror", "un error")
    assert len(requests) == 1
    assert marker not in requests[0]
    assert "https://example.com" not in requests[0]


def test_content_review_skips_marker_blocks_and_reviews_each_text_segment() -> None:
    source = (
        "<!-- PZDOC-PAGE: 1 -->\n\n"
        "Este primer párrafo contiene un eror claro.\n\n"
        "<!-- PZDOC-PAGE: 2 -->\n\n"
        "Este segundo párrafo también contiene un eror claro."
    )
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        protected = payload["messages"][1]["content"]
        requests.append(protected)
        response = protected.replace("un eror", "un error")
        return httpx.Response(200, json={"message": {"content": response}})

    result = improve_markdown(
        source,
        ImprovementMode.REVIEW_CONTENT,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
    )

    assert result == source.replace("un eror", "un error")
    assert len(requests) == 2
    assert all("PZDOC-PAGE" not in request for request in requests)


def test_structure_review_accepts_formatting_changes_that_preserve_visible_text() -> None:
    source = "**Chapter One**\n\nText with *exactly* the same words."
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"message": {"content": "PZL1=1"}})
    )

    result = improve_markdown(
        source,
        ImprovementMode.REVIEW_STRUCTURE,
        LOCAL_SETTINGS,
        transport=transport,
    )

    assert result == "# **Chapter One**\n\nText with *exactly* the same words."


def test_structure_review_keeps_wording_while_accepting_an_aligned_heading_change() -> None:
    source = "## Chapter One\n\nThe original paragraph remains complete."
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"message": {"content": "PZL1=1"}})
    )

    result = improve_markdown(
        source,
        ImprovementMode.REVIEW_STRUCTURE,
        LOCAL_SETTINGS,
        transport=transport,
    )

    assert result == "# Chapter One\n\nThe original paragraph remains complete."


def test_structure_review_rejects_an_extreme_existing_heading_demotion() -> None:
    source = "# Chapter One\n\nThe original paragraph remains complete."
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"message": {"content": "PZL1=6"}})
    )

    result = improve_markdown(
        source,
        ImprovementMode.REVIEW_STRUCTURE,
        LOCAL_SETTINGS,
        transport=transport,
    )

    assert result == source


def test_structure_review_maps_a_reordered_heading_back_to_the_exact_source() -> None:
    source = "Opening text.\n\nChapter One\n\nThe original paragraph remains complete."
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"message": {"content": "PZL3=1"}})
    )

    result = improve_markdown(
        source,
        ImprovementMode.REVIEW_STRUCTURE,
        LOCAL_SETTINGS,
        transport=transport,
    )

    assert result == "Opening text.\n\n# Chapter One\n\nThe original paragraph remains complete."


def test_structure_review_preserves_an_unsafe_chunk_instead_of_failing_the_document() -> None:
    source = "**Chapter One**\n\nThe original paragraph remains complete."
    requests = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        changed = "# Different chapter\n\nA rewritten paragraph."
        return httpx.Response(200, json={"message": {"content": changed}})

    result = improve_markdown(
        source,
        ImprovementMode.REVIEW_STRUCTURE,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
    )

    assert result == source
    assert requests == 1


def test_structure_review_does_not_promote_a_long_body_paragraph_to_heading() -> None:
    source = (
        "This is a complete body paragraph with enough words and sentences to remain ordinary "
        "prose. It must never become a chapter heading merely because the model returned it on "
        "one line, while every word and its order happened to remain unchanged."
    )
    proposed = f"# {source}"
    requests = 0
    progress: list[tuple[int, int]] = []

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"message": {"content": proposed}})

    result = improve_markdown(
        source,
        ImprovementMode.REVIEW_STRUCTURE,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
        on_progress=lambda current, total: progress.append((current, total)),
    )

    assert result == source
    assert requests == 0
    assert progress == []


def test_structure_review_recovers_exact_source_words_from_heading_directives() -> None:
    source = "Opening text.\n\nChapter One\n\nThe original paragraph remains complete."
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        numbered = payload["messages"][1]["content"]
        assert "PZL3 CANDIDATA" in numbered
        assert "Chapter One" in numbered
        return httpx.Response(200, json={"message": {"content": "PZL3=1"}})

    result = improve_markdown(
        source,
        ImprovementMode.REVIEW_STRUCTURE,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
    )

    assert result == "Opening text.\n\n# Chapter One\n\nThe original paragraph remains complete."
    assert calls == 1


def test_translation_preserves_a_failing_chunk_instead_of_losing_the_document() -> None:
    source = "Total 10."

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": "Total."}})

    assert (
        improve_markdown(
            source,
            ImprovementMode.TRANSLATE,
            LOCAL_SETTINGS,
            "Español",
            transport=httpx.MockTransport(respond),
        )
        == source
    )


def test_rejects_changes_to_inline_code() -> None:
    source = "Ejecuta `python -m parsezen` localmente."

    def respond(_request: httpx.Request) -> httpx.Response:
        changed = "Ejecuta `python app.py` localmente."
        return httpx.Response(200, json={"message": {"content": changed}})

    with pytest.raises(ImprovementError, match="código en línea"):
        improve_markdown(
            source,
            ImprovementMode.CLEAN,
            LOCAL_SETTINGS,
            transport=httpx.MockTransport(respond),
        )


@pytest.mark.parametrize(
    "mode",
    (ImprovementMode.CLEAN, ImprovementMode.REVIEW_CONTENT),
)
def test_rejects_an_html_document_wrapper_added_by_the_model(mode: ImprovementMode) -> None:
    with pytest.raises(ImprovementError, match="etiquetas HTML"):
        markdown_safety_module._validate_mode_output(
            "Texto conservado.",
            "<html>\nTexto conservado.\n</html>",
            mode,
        )


@pytest.mark.parametrize(
    ("source", "changed", "expected_error"),
    [
        (
            "| Nombre | Estado |\n| --- | --- |\n| Parsezen | Listo |",
            "| Nombre | Estado |\n| --- | --- |",
            "tabla",
        ),
    ],
)
def test_rejects_changes_to_markdown_structure(
    source: str,
    changed: str,
    expected_error: str,
) -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": changed}})

    with pytest.raises(ImprovementError, match=expected_error):
        improve_markdown(
            source,
            ImprovementMode.CLEAN,
            LOCAL_SETTINGS,
            transport=httpx.MockTransport(respond),
        )


def test_cancellation_after_a_model_response_stops_before_the_next_chunk() -> None:
    cancellation = CancellationToken()
    source = f"{'texto ' * 1332}\n\nSegundo fragmento 10."
    requests = 0
    progress: list[tuple[int, int]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        payload = json.loads(request.content)
        cancellation.cancel()
        return httpx.Response(
            200,
            json={"message": {"content": payload["messages"][1]["content"]}},
        )

    with pytest.raises(ProcessingCancelledError):
        improve_markdown(
            source,
            ImprovementMode.CLEAN,
            LOCAL_SETTINGS,
            transport=httpx.MockTransport(respond),
            on_progress=lambda current, total: progress.append((current, total)),
            cancellation=cancellation,
        )

    assert requests == 1
    assert progress == [(1, 3)]


def test_cancellation_interrupts_an_active_stream_before_it_is_published() -> None:
    cancellation = CancellationToken()

    class CancelledStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"message":{"content":"Texto parcial"},"done":false}\n'
            cancellation.cancel()
            yield b'{"message":{"content":" que no debe publicarse"},"done":true}\n'

    transport = httpx.MockTransport(lambda _request: httpx.Response(200, stream=CancelledStream()))

    with pytest.raises(ProcessingCancelledError):
        improve_markdown(
            "Texto original suficientemente claro.",
            ImprovementMode.CLEAN,
            LOCAL_SETTINGS,
            transport=transport,
            cancellation=cancellation,
        )


def test_translation_retries_when_the_first_response_stays_in_the_source_language() -> None:
    source = (
        "This deliberately long English paragraph contains enough natural language to verify "
        "that every passage reaches the requested language. It also confirms that the local "
        "model gets one careful retry before Parsezen rejects an unsafe translation."
    )
    spanish = (
        "Este párrafo deliberadamente largo contiene suficiente lenguaje natural para comprobar "
        "que cada pasaje llega al idioma solicitado. También confirma que el modelo local recibe "
        "un reintento cuidadoso antes de que Parsezen rechace una traducción insegura."
    )
    requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        content = source if len(requests) == 1 else spanish
        return httpx.Response(200, json={"message": {"content": content}})

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == spanish
    assert len(requests) == 2
    retry_messages = requests[1]["messages"]
    assert isinstance(retry_messages, list)
    assert "sin omitir" in retry_messages[0]["content"]
    assert "dejó texto natural" in retry_messages[0]["content"]


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("cambió números o fechas", "elementos protegidos"),
        ("cambió la estructura de las listas", "estructura Markdown"),
        ("cambió la estructura de énfasis Markdown", "estructura Markdown"),
        ("cambió la separación de párrafos", "estructura Markdown"),
        ("no quedó en el idioma solicitado", "dejó texto natural"),
        ("parece haber duplicado o añadido contenido", "exactamente una vez"),
        ("no devolvió el fragmento en una sola línea", "exactamente una sola línea"),
        ("invirtió una relación de certeza", "polaridad"),
    ],
)
def test_validation_retry_instruction_is_specialized_by_failure_type(
    reason: str,
    expected: str,
) -> None:
    instruction = improvement_module._specialized_retry_instruction(ImprovementError(reason))

    assert expected in instruction


def test_translation_hides_emphasis_delimiters_from_the_model_and_restores_them() -> None:
    source = "Translate *ordinary prose* and **useful guidance** in this complete sentence."
    requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        messages = payload["messages"]
        assert isinstance(messages, list)
        protected_source = messages[-1]["content"]
        assert isinstance(protected_source, str)
        markers = re.findall(r"</?PZDOC[A-Z]+XZQ>", protected_source)
        assert len(markers) == 4
        assert "*ordinary prose*" not in protected_source
        assert "**useful guidance**" not in protected_source
        content = (
            f"Traduce {markers[0]}prosa ordinaria{markers[1]} y "
            f"{markers[2]}orientación útil{markers[3]} en esta frase completa."
        )
        return httpx.Response(200, json={"message": {"content": content}})

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
        source_language_code="en",
    )

    assert result == "Traduce *prosa ordinaria* y **orientación útil** en esta frase completa."
    assert len(requests) == 1


def test_translation_hides_combined_bold_italic_delimiters_from_the_model() -> None:
    source = "Translate ***important and nuanced guidance*** carefully."

    def respond(request: httpx.Request) -> httpx.Response:
        protected_source = json.loads(request.content)["messages"][-1]["content"]
        markers = re.findall(r"</?PZDOC[A-Z]+XZQ>", protected_source)
        assert len(markers) == 2
        assert "***" not in protected_source
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": (
                        f"Traduce {markers[0]}orientación importante y matizada"
                        f"{markers[1]} cuidadosamente."
                    )
                }
            },
        )

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
        source_language_code="en",
    )

    assert result == "Traduce ***orientación importante y matizada*** cuidadosamente."


def test_translation_preserves_the_whole_block_when_segmented_coverage_still_fails() -> None:
    source = (
        "This long source paragraph records every relevant condition, exception, promise and "
        "deadline so the translation guard can detect a serious omission. The complete document "
        "must remain available and no model may replace it with a short summary."
    )
    requests = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            json={"message": {"content": "El documento contiene varias condiciones."}},
        )

    assert (
        improve_markdown(
            source,
            ImprovementMode.TRANSLATE,
            LOCAL_SETTINGS,
            "Español",
            transport=httpx.MockTransport(respond),
        )
        == source
    )

    assert requests == 5


def test_translation_recovers_coverage_by_retranslating_every_sentence_safely() -> None:
    source = (
        "The first complete sentence describes the intended weekly meal plan. "
        "The second complete sentence explains that every favourite food remains affordable."
    )
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        fragment = payload["messages"][1]["content"]
        requests.append(fragment)
        if fragment == source:
            content = (
                "Esta respuesta inventa una explicación extensa que no existe en el documento, "
                "añade consejos, conclusiones y varios detalles completamente ajenos al texto. "
                "Después continúa con otro párrafo artificial para que la expansión insegura sea "
                "inequívoca y obligue a la recuperación segmentada."
            )
        elif fragment.startswith("The first"):
            content = "La primera frase completa describe el plan semanal de comidas previsto."
        else:
            content = (
                "La segunda frase completa explica que todos los alimentos favoritos siguen "
                "siendo asequibles."
            )
        return httpx.Response(200, json={"message": {"content": content}})

    translated = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert translated == (
        "La primera frase completa describe el plan semanal de comidas previsto. "
        "La segunda frase completa explica que todos los alimentos favoritos siguen siendo "
        "asequibles."
    )
    assert len(requests) == 4


def test_translation_splits_a_title_run_after_repeated_hallucinated_expansion() -> None:
    source = "# FIRST TITLE\n\n# SECOND TITLE\n\n# THIRD TITLE"
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        fragment = payload["messages"][1]["content"]
        requests.append(fragment)
        if "PZDOC" in fragment:
            content = (
                fragment.replace(
                    "# FIRST TITLE",
                    "# PRIMER TÍTULO. Este texto añade una explicación muy extensa e inventada "
                    "que no aparece en el original y continúa con varios detalles artificiales.",
                )
                .replace(
                    "# SECOND TITLE",
                    "# SEGUNDO TÍTULO. Otra explicación creada por el modelo amplía la portada "
                    "sin ninguna base y agrega todavía más contenido que no estaba presente.",
                )
                .replace(
                    "# THIRD TITLE",
                    "# TERCER TÍTULO. Finalmente suma un tercer párrafo ficticio para provocar "
                    "una expansión desproporcionada que la validación debe rechazar por completo.",
                )
            )
        else:
            content = {
                "FIRST TITLE": "PRIMER TÍTULO",
                "SECOND TITLE": "SEGUNDO TÍTULO",
                "THIRD TITLE": "TERCER TÍTULO",
            }[fragment]
        return httpx.Response(200, json={"message": {"content": content}})

    translated = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert translated == "# PRIMER TÍTULO\n\n# SEGUNDO TÍTULO\n\n# TERCER TÍTULO"
    assert len(requests) == 5


def test_translation_skips_the_model_when_text_is_already_in_the_target_language() -> None:
    source = (
        "Este documento ya está escrito completamente en español y contiene suficiente texto "
        "natural para detectar el idioma sin consultar al modelo local instalado."
    )

    def respond(_request: httpx.Request) -> httpx.Response:
        pytest.fail("No debería realizarse ninguna petición")

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == source


def test_translation_skips_target_language_blocks_inside_a_mixed_document() -> None:
    source = (
        "# English section\n\n"
        "This deliberately long English paragraph explains the first part of the document and "
        "contains enough natural language to establish English as the predominant source.\n\n"
        "# Aviso local\n\n"
        "Este aviso ya está escrito en español y debe conservarse sin enviarlo al modelo.\n\n"
        "# Final section\n\n"
        "This final English paragraph contains additional natural language that must be "
        "translated while the Spanish warning remains untouched."
    )
    requested: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        protected = payload["messages"][1]["content"]
        requested.append(protected)
        translated = (
            protected.replace("English section", "Sección inglesa")
            .replace("Final section", "Sección final")
            .replace(
                "This deliberately long English paragraph explains the first part of the "
                "document and contains enough natural language to establish English as the "
                "predominant source.",
                "Este párrafo inglés deliberadamente largo explica la primera parte del documento "
                "y contiene suficiente lenguaje natural para establecer el inglés como fuente "
                "predominante.",
            )
            .replace(
                "This final English paragraph contains additional natural language that must be "
                "translated while the Spanish warning remains untouched.",
                "Este párrafo final contiene lenguaje natural adicional que debe traducirse "
                "mientras el aviso en español permanece intacto.",
            )
        )
        return httpx.Response(200, json={"message": {"content": translated}})

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert "Este aviso ya está escrito en español" in result
    assert all("Este aviso ya está escrito" not in content for content in requested)


def test_translation_keeps_document_language_for_prose_around_a_foreign_citation() -> None:
    english_context = (
        "This long English paragraph establishes the predominant language of the complete "
        "document before a bibliographic note written with a foreign-language work title. "
        "It contains several ordinary sentences so a quoted title cannot override the known "
        "language of the surrounding publication details. The remaining text confirms that "
        "English is the source language for every prose fragment that still needs translation."
    )
    mixed_citation = (
        "The cover image represents an ancient ceiling from the Temple of Hathor at Dendera, "
        "as illustrated in "
        "Description de l'Égypte, ou Recueil des observations et des recherches qui ont été "
        "faites en Égypte pendant l'expédition de l'armée française."
    )
    source = f"{english_context}\n\n{mixed_citation}"
    spanish_context = (
        "Este largo párrafo en inglés establece el idioma predominante del documento completo "
        "antes de una nota bibliográfica con el título de una obra en otro idioma. Contiene "
        "varias frases comunes para que un título citado no anule el idioma conocido de los "
        "datos editoriales que lo rodean. El texto restante confirma que el inglés es el idioma "
        "de origen de cada fragmento de prosa que aún necesita traducción."
    )
    translated_citation = (
        "La imagen de portada representa un techo antiguo del Templo de Hathor en Dendera, "
        "tal como aparece ilustrado en "
        "Description de l'Égypte, ou Recueil des observations et des recherches qui ont été "
        "faites en Égypte pendant l'expédition de l'armée française."
    )

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requested = payload["messages"][1]["content"]
        translated = spanish_context if english_context in requested else translated_citation
        return httpx.Response(200, json={"message": {"content": translated}})

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == f"{spanish_context}\n\n{translated_citation}"


def test_translation_accepts_explicit_source_language_for_a_mixed_citation() -> None:
    source = (
        "The cover image represents an ancient ceiling from the Temple of Hathor at Dendera, "
        "as illustrated in Description de l'Égypte, ou Recueil des observations et des "
        "recherches qui ont été faites en Égypte pendant l'expédition de l'armée française."
    )
    translated = (
        "La imagen de portada representa un techo antiguo del Templo de Hathor en Dendera, "
        "tal como aparece ilustrado en Description de l'Égypte, ou Recueil des observations "
        "et des recherches qui ont été faites en Égypte pendant l'expédition de l'armée française."
    )

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"message": {"content": translated}})
        ),
        source_language_code="en",
    )

    assert result == translated


def test_translation_plans_a_generated_html_table_as_one_atomic_unit() -> None:
    rows = "\n".join(
        f'<tr><td class="toc-label toc-level-0">Chapter {index} with a long label</td>'
        f'<td class="toc-folio">{index}</td></tr>'
        for index in range(1, 40)
    )
    source = (
        '<table class="document-toc">\n'
        '<thead><tr><th class="toc-label">Entry</th>'
        '<th class="toc-folio">Page</th></tr></thead>\n'
        f"<tbody>{rows}</tbody>\n"
        "</table>"
    )

    parts = improvement_module._plan_markdown_parts(
        source,
        max_characters=MAX_TRANSLATION_CHUNK_CHARACTERS,
        protect_paragraphs=True,
    )

    assert len(parts) == 1
    assert parts[0].text == source
    assert parts[0].should_improve


def test_table_checkpoint_invalidates_pre_bilingual_residue_fallbacks() -> None:
    source = "<table><tbody><tr><td>The Special Lot of the Moon</td></tr></tbody></table>"
    stale_key = hashlib.sha256(
        f"ollama-translation-table-v4\ntranslate\n{source}".encode()
    ).hexdigest()

    assert improvement_module._chunk_checkpoint_key(ImprovementMode.TRANSLATE, source) != stale_key


def test_translation_checkpoint_invalidates_pre_combined_emphasis_protection() -> None:
    source = "***Important guidance must be translated.***"
    stale_key = hashlib.sha256(
        f"ollama-translation-chunk-v18\ntranslate\n{source}".encode()
    ).hexdigest()

    assert improvement_module._chunk_checkpoint_key(ImprovementMode.TRANSLATE, source) != stale_key


def test_contextual_table_checkpoint_keeps_its_table_revision() -> None:
    source = "<table><tbody><tr><td>The Special Lot of the Moon</td></tr></tbody></table>"
    identity = f"Chapter context\n{source}"
    expected = hashlib.sha256(
        f"ollama-translation-table-v6\ntranslate\n{identity}".encode()
    ).hexdigest()

    assert (
        improvement_module._chunk_checkpoint_key(
            ImprovementMode.TRANSLATE,
            identity,
            classification_text=source,
        )
        == expected
    )


def test_translation_changes_only_text_nodes_of_generated_toc_table() -> None:
    source = (
        '<table class="document-toc">\n'
        '<thead><tr><th class="toc-label">Entry</th>'
        '<th class="toc-folio">Page</th></tr></thead>\n'
        '<tbody><tr><td class="toc-label toc-level-1">'
        '<strong><em><a href="#page-12">First practical lesson</a></em></strong>'
        '</td><td class="toc-folio">12</td></tr>\n'
        '<tr><td class="toc-label toc-level-0">Second useful method</td>'
        '<td class="toc-folio">20</td></tr></tbody>\n'
        "</table>"
    )
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        content = payload["messages"][-1]["content"]
        requests.append(content)
        assert "<table" not in content
        translations = {
            "Entry": "Entrada",
            "Page": "Página",
            "First practical lesson": "Primera lección práctica",
            "Second useful method": "Segundo método útil",
        }
        items = json.loads(content)["items"]
        response = {
            "items": [{"id": item["id"], "text": translations[item["text"]]} for item in items]
        }
        return httpx.Response(
            200,
            json={"message": {"content": json.dumps(response)}},
        )

    translated = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert '<table class="document-toc">' in translated
    assert '<a href="#page-12">Primera lección práctica</a>' in translated
    assert '<td class="toc-folio">12</td>' in translated
    assert '<td class="toc-folio">20</td>' in translated
    assert "Second useful method" not in translated
    assert requests


def test_table_uppercase_preservation_never_uppercases_html_tags() -> None:
    source = (
        "<table><thead><tr><th>TITLE</th></tr></thead><tbody><tr><td>TEXT</td></tr></tbody></table>"
    )
    translated = (
        "<table><thead><tr><th>Título</th></tr></thead>"
        "<tbody><tr><td>Texto</td></tr></tbody></table>"
    )

    preserved = improvement_module._preserve_translation_uppercase_fragment(
        source,
        translated,
    )

    assert preserved == (
        "<table><thead><tr><th>TÍTULO</th></tr></thead>"
        "<tbody><tr><td>TEXTO</td></tr></tbody></table>"
    )


def test_table_capitalization_preserves_every_aligned_term_not_only_examples() -> None:
    source = (
        "<table><tbody><tr><td>Day/night, Same/contrary</td></tr>"
        "<tr><td>Domicile, Exaltation</td></tr>"
        "<tr><td>Visibility/Beams/Chariot</td></tr>"
        "<tr><td>Lunar application</td></tr>"
        "<tr><td>The Sun forms a square</td></tr></tbody></table>"
    )
    translated = (
        "<table><tbody><tr><td>Día/noche, misma/contraria</td></tr>"
        "<tr><td>Domicilio, exaltación</td></tr>"
        "<tr><td>Visibilidad/rayos/carro</td></tr>"
        "<tr><td>Aplicación lunar</td></tr>"
        "<tr><td>El Sol forma una cuadratura</td></tr></tbody></table>"
    )

    preserved = improvement_module._preserve_translation_uppercase_fragment(
        source,
        translated,
    )

    assert preserved == (
        "<table><tbody><tr><td>Día/noche, Misma/contraria</td></tr>"
        "<tr><td>Domicilio, Exaltación</td></tr>"
        "<tr><td>Visibilidad/Rayos/Carro</td></tr>"
        "<tr><td>Aplicación Lunar</td></tr>"
        "<tr><td>El Sol forma una cuadratura</td></tr></tbody></table>"
    )


def test_translation_prompt_preserves_deliberate_initial_capitalization() -> None:
    assert "patrón tipográfico deliberado" in improvement_module.BASE_INSTRUCTIONS
    assert "comas, barras o saltos de línea" in improvement_module.BASE_INSTRUCTIONS


def test_focused_table_cell_retries_one_damaged_protected_marker() -> None:
    responses = iter(
        (
            "Interpretación en casas PZDOCAXZ-",
            "Interpretación en casas PZDOCAXZQ",
        )
    )

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": next(responses)}})

    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        translated = improvement_module._translate_table_text_batch(
            client,
            "parsezen-local",
            8_192,
            build_instructions(ImprovementMode.TRANSLATE, "Español"),
            "73. Interpretation in houses 8-12",
            context,
            cancellation=None,
            focused=True,
        )

    assert translated == "73. Interpretación en casas 8-12"


def test_table_batch_keeps_numbering_outside_the_model_request() -> None:
    source = "64. The First House\n\n65. The Second House"

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        prompt = json.loads(payload["messages"][1]["content"])
        assert prompt == {
            "items": [
                {"id": "PZB0001", "context": "", "text": "The First House"},
                {"id": "PZB0002", "context": "", "text": "The Second House"},
            ]
        }
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": json.dumps(
                        {
                            "items": [
                                {"id": "PZB0001", "text": "La primera casa"},
                                {"id": "PZB0002", "text": "La segunda casa"},
                            ]
                        }
                    )
                }
            },
        )

    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        translated = improvement_module._translate_table_text_batch(
            client,
            "parsezen-local",
            8_192,
            build_instructions(ImprovementMode.TRANSLATE, "Español"),
            source,
            context,
            cancellation=None,
        )

    assert translated == "64. La primera casa\n\n65. La segunda casa"


def test_table_batch_preserves_only_an_unaccepted_cell_for_focused_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "64. The First House\n\n65. A deliberately uncommon table label"
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )

    monkeypatch.setattr(
        improvement_module,
        "_translate_aligned_batch",
        lambda *_args, **_kwargs: {0: "La primera casa"},
    )
    with httpx.Client() as client:
        translated = improvement_module._translate_table_text_batch(
            client,
            "parsezen-local",
            8_192,
            build_instructions(ImprovementMode.TRANSLATE, "Español"),
            source,
            context,
            cancellation=None,
        )

    assert translated == "64. La primera casa\n\n65. A deliberately uncommon table label"


def test_table_cell_retranslates_an_unchanged_source_language_title() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        pytest.fail("Un título convencional exacto no debe llegar al modelo.")

    source = (
        "<table><thead><tr><th>1</th></tr></thead>"
        "<tbody><tr><td>64. THE FIRST HOUSE</td></tr></tbody></table>"
    )
    translated = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
        source_language_code="en",
    )

    assert "64. LA PRIMERA CASA" in translated
    assert "THE FIRST HOUSE" not in translated


def test_established_table_title_keeps_number_and_uses_conventional_ordinal() -> None:
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )

    assert (
        improvement_module._translate_established_table_cell(
            "73. THE TENTH HOUSE",
            context,
        )
        == "73. LA DÉCIMA CASA"
    )
    assert (
        improvement_module._translate_established_table_cell(
            "6S. THE FIFTH HOUSE",
            context,
        )
        == "6S. LA QUINTA CASA"
    )
    assert (
        improvement_module._translate_established_table_cell(
            "99· AFTERWORD",
            context,
        )
        == "99· EPÍLOGO"
    )
    assert (
        improvement_module._translate_established_table_cell(
            "TAURUS III",
            context,
        )
        == "TAURO III"
    )
    assert improvement_module._translate_established_table_cell("xviii", context) == "xviii"
    assert (
        improvement_module._translate_established_table_cell("(Oikodespotes)", context)
        == "(Oikodespotes)"
    )
    assert (
        improvement_module._translate_established_table_cell("(Introduction)", context)
        == "(introducción)"
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        ("21. THE SYNODIC CYCLE", "21. EL CICLO SINÓDICO"),
        ("100. Angular Triads", "100. tríadas angulares"),
        ("78. Hermetic Lots", "78. lotes herméticos"),
        (
            "116. Cadent Triplicity Lords of the Light Sect",
            "116. señores cadentes de la triplicidad de la luminaria de la secta",
        ),
        (
            "116. Cadent Triplicity Lords of the Sect Light",
            "116. señores cadentes de la triplicidad de la luminaria de la secta",
        ),
        ("PART SEVEN: DOWN TO EARTH", "PARTE SIETE: CON LOS PIES EN LA TIERRA"),
        ("PART SEVEN: DOWN  TO  EARTH", "PARTE SIETE: CON LOS PIES EN LA TIERRA"),
        (
            "116. Cadent Triplicity Lords ofthe Sect Light",
            "116. señores cadentes de la triplicidad de la luminaria de la secta",
        ),
        (
            "Exercise 49: Triplicity Lords of the Sect Light",
            "ejercicio 49: señores de la triplicidad de la luminaria de la secta",
        ),
        (
            "Exercise 49: Triplicity Lords ofthe Sect Light",
            "ejercicio 49: señores de la triplicidad de la luminaria de la secta",
        ),
        ("Interpreting Retrograde Motion", "interpretación del movimiento retrógrado"),
        ("Determining Phasis", "determinación de la fasis"),
        ("Interpreting Phasis", "interpretación de la fasis"),
        (
            "Historical Overview of Aspect Doctrines",
            "panorama histórico de las doctrinas de los aspectos",
        ),
        ("and the Moon under the Bonds", "y la Luna bajo los lazos"),
        ("76. Traditional Sign Rulerships", "76. regencias tradicionales de los signos"),
        (
            "82. Triplicity Lords of the Sect Light, Chart One",
            "82. señores de la triplicidad de la luminaria de la secta, carta uno",
        ),
        ("Goddess (Thea)", "diosa (Thea)"),
        ("Setting (Dusis)", "ocaso (Dusis)"),
        ("Midheaven (Mesouranema)", "medio cielo (Mesouranema)"),
        ("Good Spirit (Agathos Daimon)", "buen espíritu (Agathos Daimon)"),
        ("Bad Spirit (Kakos Daimon)", "mal espíritu (Kakos Daimon)"),
        ("NINTH HOUSE", "NOVENA CASA"),
        ("TENTH HOUSE", "DÉCIMA CASA"),
        ("ELEVENTH HOUSE", "UNDÉCIMA CASA"),
        ("TWELFTH HOUSE", "DUODÉCIMA CASA"),
        ("Subterranean Place", "Lugar subterráneo"),
        ("Idle", "Inactivo"),
        ("Delineating Planetary Meaning", "interpretación del significado planetario"),
        ("Placing the Planets in the Houses", "colocación de los planetas en las casas"),
        (
            "The Relative Angularity of the Houses",
            "la angularidad relativa de las casas",
        ),
        (
            "The Planet’s Domicile Lord",
            "el regente domiciliario del planeta",
        ),
        (
            "Angularity, Favorability, Testimony",
            "angularidad, favorabilidad y testimonio",
        ),
        (
            "Step Four: The Condition and Location of the Domicile Lord",
            "paso cuatro: condición y ubicación del regente domiciliario",
        ),
        ("Delineations for Chart One", "interpretaciones de la carta uno"),
        ("Delineations for Chart Two", "interpretaciones de la carta dos"),
        ("An Introduction", "una introducción"),
        ("Steering the Ship of Life", "llevar el timón de la vida"),
        (
            "The Domicile Lord of the Ascendant",
            "el regente domiciliario del Ascendente",
        ),
        ("Introducing Lots", "presentación de los lotes"),
        (
            "The Lot of Fortune and the Lord of Fortune",
            "el lote de la fortuna y el regente de la fortuna",
        ),
        ("From Its Domicile Lord", "de su regente domiciliario"),
        (
            "The Domicile Lord of Fortune",
            "el regente domiciliario de la fortuna",
        ),
        (
            "Step Six: Location and Topics of the Lord",
            "paso seis: ubicación y ámbitos del regente",
        ),
        ("kakos daimòn", "kakos daimòn"),
    ),
)
def test_established_table_title_covers_conventional_compact_labels(
    source: str,
    expected: str,
) -> None:
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )

    assert improvement_module._translate_established_table_cell(source, context) == expected


def test_cached_aligned_heading_uses_the_complete_established_source_label() -> None:
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )

    assert (
        improvement_module._restore_established_index_classifications(
            "PART SEVEN: DOWN TO EARTH\n",
            "PARTE SIETE HACIA LA TIERRA\n",
            context,
        )
        == "PARTE SIETE: CON LOS PIES EN LA TIERRA\n"
    )

    assert (
        improvement_module._restore_established_index_classifications(
            "THE RELATIVE ANGULARITY OF THE HOUSES\n",
            "LA ANGULARDAD RELATIVA DE LAS CASAS\n",
            context,
        )
        == "LA ANGULARIDAD RELATIVA DE LAS CASAS\n"
    )
    assert (
        improvement_module._restore_established_index_classifications(
            "90. THE ULTIMATE RULERS OF THE CHART 1035\n",
            "90. LOS GOBIERNALES ULTIMOS DEL GRÁFICO 1035\n",
            context,
        )
        == "90. LOS REGENTES FINALES DE LA CARTA 1035\n"
    )


def test_table_grounding_resolves_known_phrases_inside_longer_labels() -> None:
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )

    assert (
        improvement_module._ground_established_table_terms(
            "Formulas for Calculating the Seven Hermetic Lots",
            context,
        )
        == "Formulas for Calculating los siete lotes herméticos"
    )
    assert (
        improvement_module._ground_established_table_terms(
            "Step Six: Location and Themes of the Lord",
            context,
        )
        == "paso seis: Location and Themes of the señor"
    )
    assert (
        improvement_module._ground_established_table_terms(
            "Introducing the Lots",
            context,
        )
        == "Introducing los lotes"
    )


def test_aligned_table_cache_rejects_partial_source_language_residue() -> None:
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )
    source = (
        "<table><tbody><tr><td>116. Cadent Triplicity Lords of the Sect Light</td>"
        "</tr><tr><td>Step Six: Location and Themes of the Lord</td></tr></tbody></table>"
    )
    translated = (
        "<table><tbody><tr><td>116. Cadent señores de la triplicidad of the secta Light</td>"
        "</tr><tr><td>Paso Six: ubicación y temas del señor</td></tr></tbody></table>"
    )

    assert improvement_module._has_aligned_table_source_language_residue(
        source,
        translated,
        context,
    )


def test_table_normalization_repairs_only_copied_conventional_terms() -> None:
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )

    assert (
        improvement_module._normalize_established_table_translation(
            "Sign Rulerships",
            "Rulerships de los Signos",
            context,
        )
        == "Regencias de los Signos"
    )
    assert (
        improvement_module._normalize_established_table_translation(
            "PART II: THE 36 FACES",
            "PART Ii: LOS 36 FACES",
            context,
        )
        == "PARTE II: LOS 36 FACES"
    )
    assert (
        improvement_module._normalize_established_table_translation(
            "PART TEN: GLOSSARY AND SOURCES",
            "PART DIEZ: GLOSARIO Y FUENTES",
            context,
        )
        == "PARTE DIEZ: GLOSARIO Y FUENTES"
    )


def test_cached_table_is_upgraded_without_calling_the_model() -> None:
    source = (
        '<table class="document-toc"><thead><tr><th class="toc-label">PART II: THE 36 FACES</th>'
        '<th class="toc-folio">Page</th></tr></thead><tbody><tr>'
        '<td class="toc-label toc-level-1">TAURUS III</td>'
        '<td class="toc-folio">80</td></tr></tbody></table>'
    )
    cached = (
        '<table class="document-toc"><thead><tr><th class="toc-label">PART Ii: LOS 36 FACES</th>'
        '<th class="toc-folio">Página</th></tr></thead><tbody><tr>'
        '<td class="toc-label toc-level-1">TAURUS III</td>'
        '<td class="toc-folio">80</td></tr></tbody></table>'
    )
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )

    restored = improvement_module._restore_established_index_classifications(
        source,
        cached,
        context,
    )

    assert "PARTE II: LOS 36 FACES" in restored
    assert "TAURO III" in restored
    assert "TAURUS" not in restored


def test_cached_table_restores_a_substantive_label_collapsed_to_a_roman_number() -> None:
    source = (
        '<table class="document-toc"><tbody><tr>'
        '<td class="toc-label toc-level-0">A complete explanation of planetary timing</td>'
        '<td class="toc-folio">891</td></tr></tbody></table>'
    )
    cached = source.replace(
        "A complete explanation of planetary timing",
        "vii",
    )
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )

    normalized = improvement_module._normalize_aligned_table_translation(
        source,
        cached,
        context,
    )

    assert "A complete explanation of planetary timing" in normalized
    assert ">vii<" not in normalized


def test_table_cell_retries_a_substantive_label_collapsed_to_a_roman_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (
        '<table class="document-toc"><tbody><tr>'
        '<td class="toc-label toc-level-0">A complete explanation of planetary timing</td>'
        '<td class="toc-folio">891</td></tr></tbody></table>'
    )
    responses = iter(("vii", "Una explicación completa de la cronología planetaria"))

    def translate(*_args: object, **_kwargs: object) -> str:
        return next(responses)

    monkeypatch.setattr(improvement_module, "_translate_table_text_batch", translate)
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(500))
    ) as client:
        translated = improvement_module._translate_safe_html_table(
            client,
            "model",
            8192,
            "Translate.",
            source,
            context,
            None,
        )

    assert "Una explicación completa de la cronología planetaria" in translated
    assert ">vii<" not in translated


def test_established_table_cells_do_not_repeat_a_whole_table_language_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (
        "<table><thead><tr><th>INTRODUCTION</th></tr></thead>"
        "<tbody><tr><td>TAURUS III</td></tr></tbody></table>"
    )
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )

    def unexpected_global_validation(*_args: object) -> None:
        raise AssertionError("Validated cells must not be rejected by a redundant table check.")

    monkeypatch.setattr(improvement_module, "_validate_translation", unexpected_global_validation)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(500))
    ) as client:
        translated = improvement_module._translate_safe_html_table(
            client,
            "parsezen-local",
            8_192,
            "Translate",
            source,
            context,
            None,
        )

    assert "INTRODUCCIÓN" in translated
    assert "TAURO III" in translated


def test_table_translation_preserves_numeric_html_entities_byte_for_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "<table><tbody><tr><td>First&#10;House</td></tr></tbody></table>"
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )

    def translate(*args: object, **_kwargs: object) -> str:
        value = str(args[4])
        assert "&#10;" not in value
        assert "\n" not in value
        assert "PZTABLEENTITY" in value
        return value.replace("First", "Primera").replace("House", "Casa")

    monkeypatch.setattr(improvement_module, "_translate_table_text_batch", translate)
    with httpx.Client() as client:
        translated = improvement_module._translate_safe_html_table(
            client,
            "parsezen-local",
            8_192,
            "Translate",
            source,
            context,
            None,
        )

    assert translated == "<table><tbody><tr><td>Primera&#10;Casa</td></tr></tbody></table>"
    assert context.preserved_segments == []


def test_html_table_keeps_a_source_internal_linebreak_without_false_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "<table><tbody><tr><td>First\nHouse</td></tr></tbody></table>"
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )

    def translate(*args: object, **_kwargs: object) -> str:
        return {
            "First\n\nHouse": "Primera\n\nCasa",
            "First": "Primera",
            "House": "Casa",
        }[str(args[4])]

    monkeypatch.setattr(improvement_module, "_translate_table_text_batch", translate)
    with httpx.Client() as client:
        translated = improvement_module._translate_safe_html_table(
            client,
            "parsezen-local",
            8_192,
            "Translate",
            source,
            context,
            None,
        )

    assert translated == "<table><tbody><tr><td>Primera\nCasa</td></tr></tbody></table>"
    assert context.preserved_segments == []


def test_html_table_rejects_a_linebreak_added_to_a_single_line_cell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "<table><tbody><tr><td>Sample Heading</td></tr></tbody></table>"
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )

    monkeypatch.setattr(
        improvement_module,
        "_translate_table_text_batch",
        lambda *_args, **_kwargs: "Primera\nCasa",
    )
    with httpx.Client() as client:
        translated = improvement_module._translate_safe_html_table(
            client,
            "parsezen-local",
            8_192,
            "Translate",
            source,
            context,
            None,
        )

    assert translated == source
    assert context.preserved_segments == ["Sample Heading"]


def test_table_does_not_retry_a_translated_cell_on_a_short_language_false_positive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "<table><tbody><tr><td>61. Down to Earth</td></tr></tbody></table>"
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )
    requests: list[str] = []

    def translated_batch(
        _client: httpx.Client,
        _model: str,
        _context_window: int,
        _instructions: str,
        value: str,
        _context: object,
        *,
        cancellation: object,
        focused: bool = False,
    ) -> str:
        del cancellation
        requests.append(f"{focused}:{value}")
        return "61. HACIA ABAJO EN LA TIERRA"

    monkeypatch.setattr(improvement_module, "_translate_table_text_batch", translated_batch)
    monkeypatch.setattr(improvement_module, "detect_language_code", lambda *_args, **_kwargs: "en")
    with httpx.Client() as client:
        translated = improvement_module._translate_safe_html_table(
            client,
            "parsezen-local",
            8_192,
            "Translate",
            source,
            context,
            None,
        )

    assert "61. con los pies en la tierra" in translated
    assert requests == []


def test_table_batch_accepts_one_line_per_cell_without_individual_fallback() -> None:
    assert improvement_module._aligned_table_response_values(
        "Primera casa\nSegunda casa\nTercera casa",
        3,
    ) == ["Primera casa", "Segunda casa", "Tercera casa"]


@pytest.mark.parametrize(
    ("translated", "expected"),
    (
        ("<Hacia abajo en la Tierra>", "Hacia abajo en la Tierra"),
        ("<span>Hacia abajo en la Tierra</span>", "Hacia abajo en la Tierra"),
        ("La <traducción> natural", "La traducción natural"),
        ("La <em>traducción</em> natural", "La traducción natural"),
        ("La <traducción natural", "La traducción natural"),
    ),
)
def test_table_text_removes_added_markup_wrappers(
    translated: str,
    expected: str,
) -> None:
    assert (
        improvement_module._strip_added_table_text_markup("Down to Earth", translated) == expected
    )
    assert improvement_module._strip_added_table_text_markup("A < B", translated) == translated


def test_focused_table_retry_removes_an_added_markup_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "<table><tbody><tr><td>THE UNKNOWN SOURCE READING</td></tr></tbody></table>"
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )
    calls = 0

    def translate(*_args: object, **_kwargs: object) -> str:
        nonlocal calls
        calls += 1
        return "THE UNKNOWN SOURCE READING" if calls == 1 else "<Título traducido>"

    monkeypatch.setattr(improvement_module, "_translate_table_text_batch", translate)
    with httpx.Client() as client:
        translated = improvement_module._translate_safe_html_table(
            client,
            "parsezen-local",
            8_192,
            "Translate",
            source,
            context,
            None,
        )

    assert "Título traducido" in translated
    assert "&lt;" not in translated
    assert context.preserved_segments == []


def test_table_residual_uses_bilingual_single_cell_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "<table><tbody><tr><td>9. Images from the Picatrix</td></tr></tbody></table>"
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )
    bilingual_parts: list[improvement_module._TranslationReviewPart] = []

    monkeypatch.setattr(
        improvement_module,
        "_translate_table_text_batch",
        lambda *_args, **_kwargs: "9. Images from the Picatrix",
    )

    def bilingual_repair(
        _client: httpx.Client,
        _model: str,
        _context_window: int,
        part: improvement_module._TranslationReviewPart,
        **_kwargs: object,
    ) -> tuple[str, bool]:
        bilingual_parts.append(part)
        return "9. Imágenes del Picatrix", True

    monkeypatch.setattr(
        improvement_module,
        "_retranslate_priority_title",
        bilingual_repair,
    )
    with httpx.Client() as client:
        translated = improvement_module._translate_safe_html_table(
            client,
            "parsezen-local",
            8_192,
            "Translate",
            source,
            context,
            None,
        )

    assert "9. Imágenes del Picatrix" in translated
    assert len(bilingual_parts) == 1
    assert context.preserved_segments == []


def test_table_residual_recognizes_a_partially_translated_compact_title() -> None:
    assert improvement_module._has_table_source_language_residue(
        "PART SEVEN: DOWN TO EARTH",
        "PARTE SIETE: DOWN TOEARTH",
        "en",
        "es",
    )


@pytest.mark.parametrize("residue", ("by", "agencylessness"))
def test_table_residual_recognizes_unambiguous_copied_english_words(residue: str) -> None:
    assert improvement_module._has_table_source_language_residue(
        "A condition marked by agencylessness",
        f"Una condición marcada {residue}",
        "en",
        "es",
    )


def test_table_residual_recognizes_an_unchanged_lowercase_index_term() -> None:
    assert improvement_module._has_table_source_language_residue(
        "ancestors,",
        "ancestors,",
        "en",
        "es",
    )


def test_split_html_table_nodes_receive_the_complete_parent_cell_as_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "<table><tbody><tr><td>Struck by a<br>ray from a<br>malefic</td></tr></tbody></table>"
    observed_contexts: list[str] = []
    translations = ("Golpeado por un", "rayo procedente de un", "maléfico")

    def translate_aligned(
        _client: httpx.Client,
        _model: str,
        _context_window: int,
        _instructions: str,
        items: tuple[improvement_module._AlignedTranslationBatchItem, ...],
        _context: improvement_module._TranslationContext,
        _cancellation: object,
        **_kwargs: object,
    ) -> dict[int, str]:
        observed_contexts.extend(item.hierarchical_context for item in items)
        return {item.part_index: translations[item.part_index] for item in items}

    monkeypatch.setattr(improvement_module, "_translate_aligned_batch", translate_aligned)
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )
    with httpx.Client() as client:
        translated = improvement_module._translate_safe_html_table(
            client,
            "parsezen-local",
            8_192,
            "Translate",
            source,
            context,
            None,
        )

    assert translated == (
        "<table><tbody><tr><td>Golpeado por un<br>rayo procedente de un<br>maléfico"
        "</td></tr></tbody></table>"
    )
    assert observed_contexts == ["Struck by a ray from a malefic"] * 2


def test_table_residual_is_preserved_when_bilingual_repair_still_has_source_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "<table><tbody><tr><td>The Special Lot of the Moon</td></tr></tbody></table>"
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )
    monkeypatch.setattr(
        improvement_module,
        "_translate_table_text_batch",
        lambda *_args, **_kwargs: "The Special Lot of the Moon",
    )
    monkeypatch.setattr(
        improvement_module,
        "_retranslate_priority_title",
        lambda *_args, **_kwargs: ("The Special Lot of the Moon", True),
    )
    with httpx.Client() as client:
        translated = improvement_module._translate_safe_html_table(
            client,
            "parsezen-local",
            8_192,
            "Translate",
            source,
            context,
            None,
        )

    assert translated == source
    assert context.preserved_segments == ["The Special Lot of the Moon"]


def test_table_keeps_and_escapes_source_angle_brackets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "<table><tbody><tr><td>A &lt; B</td></tr></tbody></table>"
    context = improvement_module._TranslationContext(
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )

    monkeypatch.setattr(
        improvement_module,
        "_translate_table_text_batch",
        lambda *_args, **_kwargs: "A < B",
    )
    with httpx.Client() as client:
        translated = improvement_module._translate_safe_html_table(
            client,
            "parsezen-local",
            8_192,
            "Translate",
            source,
            context,
            None,
        )

    assert "A &lt; B" in translated
    assert "A < B" not in translated
    assert context.preserved_segments == []
