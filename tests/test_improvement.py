from __future__ import annotations

import json
import re

import httpx
import pytest

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
)

LOCAL_SETTINGS = AppSettings(
    model="parsezen-local",
    context_window=8_192,
    timeout_seconds=30,
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
            assert "forma FOCUS" in payload["messages"][0]["content"]
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
    assert calls == 3
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
            assert 'FOCUS="decans"' in prompt
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
            assert 'FOCUS="gaggles"' in prompt
            content = json.dumps(
                [
                    {"old": "gaggles", "new": "grupos"},
                    {"old": "scholarship", "new": "tradición académica"},
                ]
            )
        else:
            assert "scholarship" in prompt
            assert 'FOCUS="scholarship"' in prompt
            content = json.dumps([{"old": "scholarship", "new": "tradición académica"}])
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
    assert calls == 4


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


def test_bilingual_review_caches_safe_preservation_after_two_unsafe_responses() -> None:
    source = "The document keeps two complete paragraphs.\n"
    translated = "El documento conserva dos párrafos completos.\n"
    cache: dict[str, str] = {}
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
    )

    resumed = review_translation_markdown(
        source,
        translated,
        LOCAL_SETTINGS,
        "es",
        transport=httpx.MockTransport(
            lambda _request: pytest.fail("La preservación segura debe reanudarse desde caché.")
        ),
        load_checkpoint=cache.get,
    )

    assert first == resumed == translated
    assert calls == 2
    assert cache


def test_bilingual_review_recognizes_an_explicit_cached_preservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "The source-language fragment remains intentionally unchanged.\n"
    translated = "El fragmento conservado permanece intencionadamente sin cambios.\n"
    part = improvement_module._TranslationReviewPart(source, translated)
    cache = {improvement_module._translation_review_checkpoint_key(part): translated}

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
    )

    assert result == translated


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
    assert calls == 3
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


def test_bilingual_review_rejects_unaligned_source_and_translation() -> None:
    with pytest.raises(ImprovementError, match="alinear"):
        review_translation_markdown(
            "First.\n\nSecond.\n",
            "Primero y segundo en un solo bloque.\n",
            LOCAL_SETTINGS,
            "es",
            transport=httpx.MockTransport(lambda _request: pytest.fail("Unexpected request")),
        )


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


def test_short_index_term_keeps_the_document_source_language(
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
    assert fragments
    assert all("SUMMARY AND SOURCE READINGS" not in fragment for fragment in fragments)


def test_structural_title_is_fully_grounded_before_the_model_request() -> None:
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
    assert len(fragments) == 1
    assert "PART SIX" not in fragments[0]
    assert "ART OF JUDGMENT" not in fragments[0]


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
        len(fragment) <= improvement_module.MAX_FOCUSED_LEXICAL_TRANSLATION_CHARACTERS
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
            translated = "Lee la guía completa.\nConserva cada detalle útil."
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
    assert calls == 1 + len(source_lines)


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


def test_translation_recovers_table_rows_when_a_full_response_breaks_the_table() -> None:
    source = (
        "| Topic | Description |\n"
        "| --- | --- |\n"
        "| First house | This section explains the first astrological house clearly. |\n"
        "| Second house | This section explains the second astrological house clearly. |"
    )
    translations = {
        "| Topic | Description |": "| Tema | Descripción |",
        (
            "| First house | This section explains the first astrological house clearly. |"
        ): "| Primera casa | Esta sección explica claramente la primera casa astrológica. |",
        (
            "| Second house | This section explains the second astrological house clearly. |"
        ): "| Segunda casa | Esta sección explica claramente la segunda casa astrológica. |",
    }

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        content = payload["messages"][1]["content"]
        translated = (
            content.replace("| --- | --- |\n", "")
            if "\n" in content
            else translations.get(content, content)
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
        if "FOCUS:" in content:
            focus = re.search(r"FOCUS: ([^\n]+)", content)
            assert focus is not None
            equivalents = {
                "practical": "práctica",
                "useful": "útil",
                "method": "método",
            }
            response = equivalents.get(focus.group(1).casefold(), "término")
        else:
            assert "<table" not in content
            response = "Entrada\n\nPágina\n\nPrimera lección práctica\n\nSegundo método útil"
        return httpx.Response(200, json={"message": {"content": response}})

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
        prompt = payload["messages"][1]["content"]
        assert "64." not in prompt
        assert "65." not in prompt
        return httpx.Response(
            200,
            json={"message": {"content": "La primera casa\n\nLa segunda casa"}},
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


def test_table_batch_accepts_one_line_per_cell_without_individual_fallback() -> None:
    assert improvement_module._aligned_table_response_values(
        "Primera casa\nSegunda casa\nTercera casa",
        3,
    ) == ["Primera casa", "Segunda casa", "Tercera casa"]
