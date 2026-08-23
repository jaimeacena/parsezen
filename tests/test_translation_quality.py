from __future__ import annotations

import builtins

import pytest

import parsezen.translation_quality as translation_quality_module
from parsezen.translation_quality import (
    MAX_REPORT_EXCERPT_CHARACTERS,
    NUMBER_PATTERN,
    LinguisticReviewCoverage,
    LinguisticReviewMode,
    TranslationIssueKind,
    TranslationQualityError,
    build_aligned_translation_quality_report,
    build_translation_quality_report,
    detect_language_code,
    find_untranslated_source_sentences,
    is_literal_work_title_translation,
    natural_language_text,
    numeric_tokens_are_conserved,
    repair_untranslated_source_text,
    resolve_language_code,
    restore_changed_third_language_headings,
    validate_translation_quality,
)

SOURCE = """# Contract

On March 14, 2026, Elena paid $1,250.50 for three services. The committee reviewed every clause
carefully and rejected the proposal because it changed the original scope.

- Keep the original order.
- Read [the official guide](https://example.com/guide).

```python
print("This code remains in English")
```
"""

SPANISH = """# Contrato

El 14 de marzo de 2026, Elena pagó $1,250.50 por tres servicios. El comité revisó cuidadosamente
cada cláusula y rechazó la propuesta porque cambiaba el alcance original.

- Mantén el orden original.
- Lee [la guía oficial](https://example.com/guide).

```python
print("This code remains in English")
```
"""


def test_rejects_a_markdown_link_with_a_missing_closing_delimiter() -> None:
    with pytest.raises(TranslationQualityError, match="enlace Markdown incompleto"):
        validate_translation_quality(
            "Read [the note](<#page-36>) before continuing.",
            "Lee [la nota](<#page-36> antes de continuar.",
            source_language="en",
            target_language=None,
            preserve_paragraphs=True,
        )


def test_rejects_a_new_near_duplicate_long_paragraph() -> None:
    first_source = (
        "The first independent paragraph explains the complete method, its historical origin, "
        "its practical limits, and every condition required before applying the procedure. "
        "It then gives a distinct example so the reader can verify each step safely."
    )
    second_source = (
        "A separate discussion examines another technique, compares several interpretations, "
        "and records the evidence needed to choose among them. Its conclusion concerns a wholly "
        "different question and does not repeat the earlier explanation."
    )
    repeated = (
        "El primer párrafo independiente explica el método completo, su origen histórico, sus "
        "límites prácticos y todas las condiciones necesarias antes de aplicar el procedimiento. "
        "Después ofrece un ejemplo distinto para verificar cada paso con seguridad."
    )
    near_copy = repeated.replace("primer párrafo", "segundo párrafo").replace(
        "un ejemplo distinto",
        "otro ejemplo",
    )

    with pytest.raises(TranslationQualityError, match="repetido un párrafo"):
        validate_translation_quality(
            f"{first_source}\n\n{second_source}",
            f"{repeated}\n\n{near_copy}",
            source_language="en",
            target_language=None,
            preserve_paragraphs=True,
        )


def test_compact_label_matching_tolerates_a_missing_ocr_possessive_apostrophe() -> None:
    assert (
        translation_quality_module.established_compact_label_translation(
            "Exercise 43: A Planets Assistance from Its Domicile Lord",
            "en",
            "es",
        )
        == "ejercicio 43: ayuda de un planeta procedente de su regente domiciliario"
    )
    assert (
        translation_quality_module.established_compact_label_translation(
            "Exercise 48: The Lot of Fortune and the Domicile Lord of Fortune",
            "en",
            "es",
        )
        == "ejercicio 48: el lote de la fortuna y el regente domiciliario de la fortuna"
    )


@pytest.mark.parametrize(
    "coverage",
    (
        LinguisticReviewCoverage(LinguisticReviewMode.NOT_REVIEWED, 1, 0, 0, 0, 0),
        LinguisticReviewCoverage(LinguisticReviewMode.NOT_REVIEWED, 1, 1, 0, 0, 0),
    ),
)
def test_accepts_consistent_linguistic_coverage(coverage: LinguisticReviewCoverage) -> None:
    assert coverage.semantically_unreviewed_blocks == 1


@pytest.mark.parametrize(
    "arguments",
    (
        (-1, 0, 0, 0, 0),
        (1, 2, 0, 0, 0),
        (1, 1, 0, 1, 0),
    ),
)
def test_rejects_inconsistent_linguistic_coverage(
    arguments: tuple[int, int, int, int, int],
) -> None:
    with pytest.raises(ValueError, match="Linguistic review counts"):
        LinguisticReviewCoverage(LinguisticReviewMode.NOT_REVIEWED, *arguments)


def test_accepts_a_complete_translation_with_preserved_markdown() -> None:
    validate_translation_quality(
        SOURCE,
        SPANISH,
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )


def test_rejects_a_raw_html_table_that_drops_an_empty_cell_and_its_column() -> None:
    source = (
        "<table><thead><tr><th>DECAN</th><th>QUALITY</th><th>IMAGE</th></tr></thead>"
        "<tbody><tr><td>Aries I</td><td></td><td>A complete figure description.</td></tr>"
        "</tbody></table>"
    )
    translated = (
        "<table><thead><tr><th>DECANO</th><th>IMAGEN</th></tr></thead>"
        "<tbody><tr><td>Aries I</td><td>Una descripción completa de la figura.</td></tr>"
        "</tbody></table>"
    )

    with pytest.raises(TranslationQualityError, match="tabla HTML"):
        validate_translation_quality(
            source,
            translated,
            source_language="en",
            target_language="es",
            preserve_paragraphs=True,
        )


def test_raw_html_table_structure_allows_equivalent_self_closing_break_syntax() -> None:
    source = "<table><tbody><tr><td>First line<br>second line</td></tr></tbody></table>"
    translated = "<table><tbody><tr><td>Primera línea<br />segunda línea</td></tr></tbody></table>"

    validate_translation_quality(
        source,
        translated,
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )


def test_rejects_a_toc_folio_moved_away_from_the_end_of_its_list_entry() -> None:
    source = "- Appendices 258\n- Tables of Correspondence 266\n"
    translated = "- 258 Apéndices\n- Tablas de correspondencia 266\n"

    with pytest.raises(TranslationQualityError, match="referencias finales"):
        validate_translation_quality(
            source,
            translated,
            source_language="en",
            target_language="es",
            preserve_paragraphs=True,
        )


def test_rejects_text_that_did_not_reach_the_requested_language() -> None:
    with pytest.raises(TranslationQualityError, match="idioma solicitado"):
        validate_translation_quality(
            SOURCE,
            SOURCE,
            source_language="en",
            target_language="es",
            preserve_paragraphs=True,
        )


def test_reports_a_short_title_left_in_the_source_language() -> None:
    source = SOURCE.replace("# Contract", "# IMPORTANT CONTRACT")
    translated = SPANISH.replace("# Contrato", "# IMPORTANT CONTRACT")

    validate_translation_quality(
        source,
        translated,
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )
    report = build_translation_quality_report(
        source,
        translated,
        source_language="en",
        target_language="es",
    )

    assert report.total_issues > 0


def test_report_counts_checked_and_unaligned_output_blocks_separately() -> None:
    report = build_translation_quality_report(
        "First source paragraph.\n\nSecond source paragraph.",
        "Primer párrafo traducido.\n\nSegundo párrafo traducido.\n\nBloque adicional.",
        source_language="en",
        target_language="es",
    )

    assert report.source_blocks == 2
    assert report.translated_blocks == 3
    assert report.checked_segments == 2
    assert report.issues_by_kind[TranslationIssueKind.ALIGNMENT] == 1


def test_reports_an_untranslated_title_below_the_general_language_threshold() -> None:
    source = "30 DAY MILLIONAIRE CHALLENGE\n\nShort English text."
    translated = "30 DAY MILLIONAIRE CHALLENGE\n\nTexto breve en español."

    validate_translation_quality(
        source,
        translated,
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )
    report = build_translation_quality_report(
        source,
        translated,
        source_language="en",
        target_language="es",
    )

    assert report.total_issues > 0


@pytest.mark.parametrize(
    "title",
    (
        "CHOOSE YO' CHARACTER",
        "EXPENSE CATEGORY",
        "BIBLIOGRAPHY",
        "INDEX",
    ),
)
def test_reports_common_short_english_labels_as_untranslated(title: str) -> None:
    report = build_translation_quality_report(
        title,
        title,
        source_language="en",
        target_language="es",
    )

    assert report.issues_by_kind[TranslationIssueKind.SOURCE_TEXT] >= 1


def test_repair_retries_each_aligned_common_short_label_once() -> None:
    source = "EXPENSE CATEGORY\n\nBIBLIOGRAPHY"
    replacements = {
        "EXPENSE CATEGORY": "CATEGORÍA DE GASTOS",
        "BIBLIOGRAPHY": "BIBLIOGRAFÍA",
    }
    requested: list[str] = []

    repair = repair_untranslated_source_text(
        source,
        source,
        source_language="en",
        target_language="es",
        translate_segment=lambda source_fragment, _current: (
            requested.append(source_fragment) or replacements[source_fragment]
        ),
    )

    assert requested == ["EXPENSE CATEGORY", "BIBLIOGRAPHY"]
    assert repair.repaired_segments == 2
    assert repair.translated == "CATEGORÍA DE GASTOS\n\nBIBLIOGRAFÍA"


def test_reports_short_spanish_index_residue_inside_an_otherwise_english_page() -> None:
    source = (
        "<!-- PZDOC PDF PAGE 8 -->\n\n"
        "- EXPOSICIÓN 307\n"
        "- APÉNDICE 5: EDICIONES DE LUJO 427\n"
        "- CENTRO DE TRANSURFING 437\n\n"
        "Este párrafo suficientemente largo establece con claridad el idioma de origen."
    )
    translated = (
        "<!-- PZDOC PDF PAGE 8 -->\n\n"
        "- EXPOSICION 307\n"
        "- APPENDIX 5:EDITIONSOFLUJO 427\n"
        "- CENTRO OF TRANSURFING 437\n\n"
        "This sufficiently long paragraph clearly establishes the target language."
    )

    report = build_translation_quality_report(
        source,
        translated,
        source_language="es",
        target_language="en",
    )

    assert report.issues_by_kind[TranslationIssueKind.SOURCE_TEXT] >= 3


def test_repairs_aligned_short_index_residue_independently() -> None:
    source = "<!-- PZDOC PDF PAGE 8 -->\n\n- EXPOSICIÓN 307\n- APÉNDICE 5: EDICIONES DE LUJO 427\n"
    translated = "<!-- PZDOC PDF PAGE 8 -->\n\n- EXPOSICION 307\n- APPENDIX 5:EDICIONESDELUJO 427\n"
    replacements = {
        "- EXPOSICIÓN 307": "- EXHIBITION 307",
        "- APÉNDICE 5: EDICIONES DE LUJO 427": ("- APPENDIX 5: LUXURY EDITIONS 427"),
    }

    repair = repair_untranslated_source_text(
        source,
        translated,
        source_language="es",
        target_language="en",
        translate_segment=lambda source_segment, _current: replacements.get(
            source_segment.strip(),
            _current,
        ),
    )

    assert repair.attempted_segments == 2
    assert repair.repaired_segments == 2
    assert "- EXHIBITION 307" in repair.translated
    assert "- APPENDIX 5: LUXURY EDITIONS 427" in repair.translated


def test_accepts_a_changed_short_work_title_despite_unreliable_language_detection() -> None:
    validate_translation_quality(
        "WHOLE LOTTA MONEY (feat. N.",
        "TANTA DINERO (feat. N.)",
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )


def test_accepts_an_unchanged_person_name_used_as_a_heading() -> None:
    validate_translation_quality(
        "# Austin Coppock\n\nA sufficiently long English paragraph explains the complete source.",
        "# Austin Coppock\n\nUn párrafo suficientemente largo explica la fuente completa.",
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )


def test_accepts_a_repeated_uppercase_person_name_used_as_headings() -> None:
    validate_translation_quality(
        (
            "# CHRIS BRENNAN\n\n"
            "A sufficiently long English paragraph establishes the complete source language.\n\n"
            "## CHRIS BRENNAN\n\n"
            "Another sufficiently long English paragraph completes the source."
        ),
        (
            "# CHRIS BRENNAN\n\n"
            "Un párrafo suficientemente largo establece por completo el idioma de origen.\n\n"
            "## CHRIS BRENNAN\n\n"
            "Otro párrafo suficientemente largo completa la fuente."
        ),
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )


def test_reports_an_unchanged_uppercase_subject_heading() -> None:
    source = "# PLANETARY CONDITION\n\nA sufficiently long English paragraph explains the source."
    translated = "# PLANETARY CONDITION\n\nUn párrafo suficientemente largo explica la fuente."

    validate_translation_quality(
        source,
        translated,
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )
    report = build_translation_quality_report(
        source,
        translated,
        source_language="en",
        target_language="es",
    )

    assert report.total_issues > 0


def test_accepts_an_unchanged_publisher_name() -> None:
    validate_translation_quality(
        "THREE HANDS PRESS",
        "THREE HANDS PRESS",
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )


def test_reports_a_partially_untranslated_short_title() -> None:
    source = "30 DAY MILLIONAIRE CHALLENGE"
    translated = "30 días MILLIONAIRE CHALLENGE"

    validate_translation_quality(
        source,
        translated,
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )
    report = build_translation_quality_report(
        source,
        translated,
        source_language="en",
        target_language="es",
    )

    assert report.total_issues > 0


def test_reports_partially_untranslated_short_titles_in_a_dense_index() -> None:
    source = "SCORPIO III 186\nSAGITTARIUS I 192\nPISCES I 244\nAPPENDICES 258"
    translated = "SCORPION III 186\n\nSAGITARIO I 192\nPISCIS I 244\nAPÉNDICES 258"

    validate_translation_quality(
        source,
        translated,
        source_language="en",
        target_language="es",
        preserve_paragraphs=False,
    )
    report = build_translation_quality_report(
        source,
        translated,
        source_language="en",
        target_language="es",
    )

    assert report.total_issues > 0


def test_report_accepts_an_established_title_term_unchanged_in_the_target_language() -> None:
    report = build_translation_quality_report(
        "ARIES I 244\nARIES II 251\nARIES III 258",
        "ARIES I 244\nARIES II 251\nARIES III 258",
        source_language="en",
        target_language="es",
    )

    assert report.total_issues == 0


def test_report_still_requires_a_localized_classification_term() -> None:
    report = build_translation_quality_report(
        "TAURUS I 69\nTAURUS II 74\nTAURUS III 80",
        "TAURUS I 69\nTAURUS II 74\nTAURUS III 80",
        source_language="en",
        target_language="es",
    )

    assert report.issues_by_kind[TranslationIssueKind.SOURCE_TEXT] >= 3


def test_report_flags_an_english_word_copied_inside_a_spanish_paragraph() -> None:
    source = "The author tried to combine narrative and scholarship while preserving every detail."
    translated = (
        "El autor intentó combinar narrativa y scholarship mientras conservaba cada detalle."
    )

    report = build_translation_quality_report(
        source,
        translated,
        source_language="en",
        target_language="es",
    )

    assert report.issues_by_kind[TranslationIssueKind.SOURCE_TEXT] == 1


def test_repair_retries_an_aligned_paragraph_with_a_copied_source_word_once() -> None:
    source = "The author tried to combine narrative and scholarship while preserving every detail."
    translated = (
        "El autor intentó combinar narrativa y scholarship mientras conservaba cada detalle."
    )
    calls: list[tuple[str, str]] = []

    result = repair_untranslated_source_text(
        source,
        translated,
        source_language="en",
        target_language="es",
        translate_segment=lambda original, current: (
            calls.append((original, current))
            or "El autor intentó combinar narrativa y erudición mientras conservaba cada detalle."
        ),
    )

    assert result.repaired_segments == 1
    assert result.translated.endswith("mientras conservaba cada detalle.")
    assert len(calls) == 1


def test_rejects_a_translation_that_omits_most_content() -> None:
    shortened = """# Contrato

El comité lo rechazó el 14 de marzo de 2026 por $1,250.50.

- Orden original.
- [Guía](https://example.com/guide).

```python
print("This code remains in English")
```
"""
    with pytest.raises(TranslationQualityError, match="omitido"):
        validate_translation_quality(
            SOURCE,
            shortened,
            source_language="en",
            target_language="es",
            preserve_paragraphs=True,
        )


def test_numeric_tokens_are_detected_even_when_adjacent_to_letters() -> None:
    assert NUMBER_PATTERN.findall("DAYS6+7 and H2O") == ["6", "7", "2"]


def test_numeric_conservation_ignores_html_character_entity_codes() -> None:
    assert translation_quality_module.numeric_tokens_are_conserved(
        "House 73&#160;Meaning",
        "Casa 73\N{NO-BREAK SPACE}Significado",
    )


def test_does_not_treat_a_prefixed_translation_as_an_unchanged_sentence() -> None:
    source = "Read the complete guide before continuing with the workflow."

    assert (
        find_untranslated_source_sentences(
            source,
            f"ES:{source}",
            "en",
        )
        == ()
    )


def test_accepts_a_written_number_converted_to_digits_without_duplication() -> None:
    assert numeric_tokens_are_conserved("ten and ten", "diez y 10")
    assert numeric_tokens_are_conserved(
        "50. Elevenfold Division of the Lunar Cycle",
        "50. División del ciclo lunar en 11 partes",
    )


def test_rejects_a_changed_small_written_number_during_translation() -> None:
    with pytest.raises(TranslationQualityError, match="cantidad escrita"):
        validate_translation_quality(
            "PART SEVEN: DOWN TO EARTH",
            "PARTE SEIS: HACIA LA TIERRA",
            source_language="en",
            target_language="es",
            preserve_paragraphs=True,
        )


def test_accepts_equivalent_small_written_numbers_and_both() -> None:
    validate_translation_quality(
        "PART SEVEN: TWO HOUSES",
        "PARTE SIETE: AMBAS CASAS",
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )


def test_ignores_ambiguous_written_numbers_in_ordinary_prose() -> None:
    validate_translation_quality(
        "Once the two sides agree, both can move forward with the plan.",
        "Una vez que las partes se ponen de acuerdo, la pareja puede seguir adelante con el plan.",
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )


def test_does_not_treat_english_once_as_the_spanish_number_eleven() -> None:
    validate_translation_quality(
        "# ONCE UPON A TIME",
        "# ÉRASE UNA VEZ",
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )


def test_rejects_singular_spanish_unit_for_a_plural_numeric_duration() -> None:
    with pytest.raises(TranslationQualityError, match="duración numérica"):
        validate_translation_quality(
            "30 DAY MILLIONAIRE CHALLENGE",
            "30 DÍA MILLONARIO DESAFÍO",
            source_language="en",
            target_language="es",
            preserve_paragraphs=True,
        )


def test_accepts_reordered_plural_spanish_duration() -> None:
    validate_translation_quality(
        "30 DAY MILLIONAIRE CHALLENGE",
        "DESAFÍO DEL MILLONARIO DE 30 DÍAS",
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )


def test_rejects_an_ungrounded_or_duplicated_new_digit() -> None:
    assert not numeric_tokens_are_conserved("ordinary text", "texto ordinario 10")
    assert not numeric_tokens_are_conserved("ten", "diez 10")


def test_rejects_local_paragraph_duplication_hidden_by_a_long_document() -> None:
    source = (
        ("A long surrounding paragraph keeps the whole-document ratio deceptively normal. " * 18)
        + "\n\n"
        + ("A compact footnote explains one source in sufficient detail. " * 4)
    )
    translated = (
        ("Un párrafo largo circundante mantiene normal la proporción de todo el documento. " * 18)
        + "\n\n"
        + ("Una nota breve explica una fuente con suficiente detalle. " * 9)
    )

    with pytest.raises(TranslationQualityError, match="dentro de un párrafo"):
        translation_quality_module._validate_content_coverage(source, translated)


def test_rejects_a_changed_roman_numeral_in_an_index_reference() -> None:
    with pytest.raises(TranslationQualityError, match="números romanos"):
        validate_translation_quality(
            "LIBRA III 168",
            "LIBRA TERCERA 168",
            source_language="en",
            target_language="es",
            preserve_paragraphs=True,
        )


def test_accepts_a_written_ordinal_rendered_as_a_roman_century() -> None:
    validate_translation_quality(
        "- Seventeenth-Century England 17",
        "- Inglaterra del siglo XVII 17",
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )


def test_rejects_an_ungrounded_roman_century_on_the_same_index_line() -> None:
    with pytest.raises(TranslationQualityError, match="números romanos"):
        validate_translation_quality(
            "- Twentieth-Century England 17",
            "- Inglaterra del siglo XVII 17",
            source_language="en",
            target_language="es",
            preserve_paragraphs=True,
        )


def test_accepts_translated_short_labels_dominated_by_proper_names() -> None:
    source = """Moment for Life

Nicki Minaj and Drake

Best Life featuring Chance

Cardi B and Chance the Rapper

Ariana Grande

No Hands featuring Roscoe
"""
    translated = """Momento para toda la vida

Nicki Minaj y Drake

La mejor vida con Chance

Cardi B y Chance the Rapper

Ariana Grande

Sin manos con Roscoe
"""

    validate_translation_quality(
        source,
        translated,
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )


def test_rejects_an_unchanged_short_label_collection() -> None:
    source = """Moment for Life

Nicki Minaj and Drake

Best Life featuring Chance

Cardi B and Chance the Rapper

Ariana Grande

No Hands featuring Roscoe
"""

    with pytest.raises(TranslationQualityError, match="idioma solicitado"):
        validate_translation_quality(
            source,
            source,
            source_language="en",
            target_language="es",
            preserve_paragraphs=True,
        )


def test_recognizes_translated_short_labels_separated_only_by_line_breaks() -> None:
    source = """JANUARY 2024
Austin Coppock 36
FEBRUARY 2024
Demetra George 48
MARCH 2024
Vadim Zeland 72"""
    translated = """ENERO 2024
Austin Coppock 36
FEBRERO 2024
Demetra George 48
MARZO 2024
Vadim Zeland 72"""

    assert translation_quality_module._is_translated_short_label_collection(
        source,
        translated,
    )
    assert not translation_quality_module._is_translated_short_label_collection(
        source,
        source,
    )


def test_accepts_an_unchanged_index_made_only_of_names_and_numbers() -> None:
    names = """AUSTIN COPPOCK 1
DEMETRA GEORGE 2
VADIM ZELAND 3
ALICE WALKER 4
JORGE LUIS BORGES 5"""

    assert translation_quality_module._is_translated_short_label_collection(
        names,
        names,
        source_language="en",
    )


@pytest.mark.parametrize(
    ("unsafe", "message"),
    [
        (SPANISH.replace("- Lee", "Lee"), "listas"),
        (SPANISH.replace("\n\n- Mantén", "\n- Mantén"), "párrafos"),
        (SPANISH.replace("https://example.com/guide", "https://example.com/otro"), "enlaces"),
        (SPANISH.replace('print("This code remains in English")', 'print("cambiado")'), "código"),
    ],
)
def test_rejects_structural_damage(unsafe: str, message: str) -> None:
    with pytest.raises(TranslationQualityError, match=message):
        validate_translation_quality(
            SOURCE,
            unsafe,
            source_language="en",
            target_language="es",
            preserve_paragraphs=True,
        )


def test_language_detection_ignores_code_and_link_destinations() -> None:
    markdown = (
        "Este párrafo contiene suficiente texto natural en español para detectar su idioma. "
        "También explica claramente el propósito de esta prueba local.\n\n"
        "```python\nprint('A long English code example that must not affect detection')\n```\n"
        "[documentación](https://example.com/very/long/english/path)"
    )

    assert detect_language_code(markdown) == "es"
    assert "print" not in natural_language_text(markdown)
    assert "https" not in natural_language_text(markdown)


def test_language_detection_degrades_safely_when_local_detector_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def import_without_langdetect(name: str, *args: object, **kwargs: object) -> object:
        if name == "langdetect":
            raise ImportError("dependency unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_langdetect)

    assert detect_language_code("Este texto contiene suficientes palabras en español.") is None


def test_accepts_target_prose_around_a_conserved_third_language_citation() -> None:
    source = (
        "The cover image represents an ancient ceiling, as illustrated in "
        "Description de l'Égypte, ou Recueil des observations et des recherches qui ont été "
        "faites en Égypte pendant l'expédition de l'armée française, publié à Paris par la "
        "Commission des sciences et des arts."
    )
    translated = (
        "La imagen de portada representa un techo antiguo, tal como aparece ilustrado en "
        "Description de l'Égypte, ou Recueil des observations et des recherches qui ont été "
        "faites en Égypte pendant l'expédition de l'armée française, publié à Paris par la "
        "Commission des sciences et des arts."
    )

    assert translation_quality_module._has_target_language_change_evidence(
        source,
        translated,
        source_language="en",
        target_language="es",
    )
    validate_translation_quality(
        source,
        translated,
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )


def test_rejects_a_small_target_prefix_before_large_source_language_residue() -> None:
    source = (
        "The translated introduction is followed by a long source paragraph that still "
        "explains every original condition, exception, deadline, promise and limitation. "
        "This entire second sentence also remains in English and must not be mistaken for "
        "a foreign citation merely because a short prefix was translated."
    )
    translated = source.replace(
        "The translated introduction is followed by",
        "La introducción traducida va seguida de",
    )

    assert not translation_quality_module._has_target_language_change_evidence(
        source,
        translated,
        source_language="en",
        target_language="es",
    )
    with pytest.raises(TranslationQualityError, match="idioma solicitado"):
        validate_translation_quality(
            source,
            translated,
            source_language="en",
            target_language="es",
            preserve_paragraphs=True,
        )


def test_natural_text_keeps_xml_content_while_ignoring_namespace_urls() -> None:
    xml = '<html:p xmlns:html="http://www.w3.org/1999/xhtml">Visible text</html:p>'

    assert natural_language_text(xml) == "Visible text"


@pytest.mark.parametrize(
    ("language", "expected"),
    [("Español", "es"), ("EN", "en"), ("desconocido", None), (None, None)],
)
def test_resolves_only_supported_language_codes(
    language: str | None,
    expected: str | None,
) -> None:
    assert resolve_language_code(language) == expected


def test_translation_report_has_no_warnings_for_a_sound_translation() -> None:
    report = build_translation_quality_report(
        SOURCE,
        SPANISH,
        source_language="en",
        target_language="es",
    )

    assert report.source_language == "en"
    assert report.target_language == "es"
    assert report.detected_language == "es"
    assert report.checked_segments == 3
    assert report.total_issues == 0
    assert report.issues == ()


def test_translation_report_flags_inconsistent_cognates_in_repeated_headings() -> None:
    source = (
        "# ANCIENT ASTROLOGY\n\n"
        "A complete English paragraph establishes the source language for this document.\n\n"
        "## MODERN ASTROLOGY\n\n"
        "Another complete paragraph supplies enough natural language for reliable checks.\n\n"
        "## PRACTICAL ASTROLOGY"
    )
    translated = (
        "# ASTROLOGÍA ANTIGUA\n\n"
        "Un párrafo completo establece el idioma de origen de este documento.\n\n"
        "## ASTROLOGÍA MODERNA\n\n"
        "Otro párrafo completo aporta suficiente lenguaje natural para comprobaciones fiables.\n\n"
        "## ASTRONOMÍA PRÁCTICA"
    )

    report = build_translation_quality_report(
        source,
        translated,
        source_language="en",
        target_language="es",
    )

    assert any(
        issue.kind is TranslationIssueKind.FIDELITY and "formas incompatibles" in issue.message
        for issue in report.issues
    )


def test_translation_report_flags_a_changed_third_language_heading() -> None:
    source = (
        "# COMPLETE ENGLISH GUIDE\n\n"
        "This complete English paragraph establishes the source language reliably.\n\n"
        "## SCRIBE SANGUINE QUIA SANGUIS SPIRITUS"
    )
    translated = (
        "# GUÍA COMPLETA EN ESPAÑOL\n\n"
        "Este párrafo completo establece de forma fiable el idioma de origen.\n\n"
        "## ESCRIBIR SANGUINE QUE SANGUE EL ESPÍRITU"
    )

    report = build_translation_quality_report(
        source,
        translated,
        source_language="en",
        target_language="es",
    )

    assert any(
        issue.kind is TranslationIssueKind.FIDELITY and "tercer idioma" in issue.message
        for issue in report.issues
    )


def test_translation_report_accepts_an_unchanged_third_language_heading() -> None:
    foreign_heading = "## SCRIBE SANGUINE QUIA SANGUIS SPIRITUS"
    report = build_translation_quality_report(
        (
            "# COMPLETE ENGLISH GUIDE\n\n"
            "This complete English paragraph establishes the source language reliably.\n\n"
            f"{foreign_heading}"
        ),
        (
            "# GUÍA COMPLETA EN ESPAÑOL\n\n"
            "Este párrafo completo establece de forma fiable el idioma de origen.\n\n"
            f"{foreign_heading}"
        ),
        source_language="en",
        target_language="es",
    )

    assert not any(issue.kind is TranslationIssueKind.FIDELITY for issue in report.issues)


def test_restores_changed_third_language_heading_without_reverting_its_level() -> None:
    source = (
        "# COMPLETE ENGLISH GUIDE\n\n"
        "English explanatory prose remains the source language throughout the document.\n\n"
        "## SCRIBE SANGUINE QUIA SANGUIS SPIRITUS\n"
    )
    translated = (
        "# GUÍA COMPLETA EN ESPAÑOL\n\n"
        "La prosa explicativa ya está traducida correctamente al español.\n\n"
        "### ESCRIBIR SANGUINE QUE SANGUE EL ESPÍRITU\n"
    )

    repaired = restore_changed_third_language_headings(
        source,
        translated,
        source_language="en",
        target_language="es",
    )

    assert "### SCRIBE SANGUINE QUIA SANGUIS SPIRITUS" in repaired
    assert "# GUÍA COMPLETA EN ESPAÑOL" in repaired


def test_does_not_restore_a_heading_from_the_document_source_language() -> None:
    source = (
        "# COMPLETE ENGLISH GUIDE\n\n"
        "English explanatory prose remains the source language throughout the document.\n"
    )
    translated = (
        "# GUÍA COMPLETA EN ESPAÑOL\n\n"
        "La prosa explicativa ya está traducida correctamente al español.\n"
    )

    repaired = restore_changed_third_language_headings(
        source,
        translated,
        source_language="en",
        target_language="es",
    )

    assert repaired == translated


def test_translation_report_flags_a_short_unchanged_source_sentence() -> None:
    source = "Original paragraph 30. This is deliberately substantial text for the source document."
    translated = (
        "Original paragraph 30. Este texto es deliberadamente sustancial para el documento."
    )

    report = build_translation_quality_report(
        source,
        translated,
        source_language="en",
        target_language="es",
    )

    assert report.total_issues == 1
    assert report.issues[0].kind is TranslationIssueKind.SOURCE_TEXT
    assert "idioma original" in report.issues[0].message


def test_translation_report_does_not_flag_a_tiny_unchanged_person_name() -> None:
    source = (
        "Austin Coppock\n\n"
        "This complete English paragraph provides enough context for reliable language checks."
    )
    translated = (
        "Austin Coppock\n\n"
        "Este párrafo completo proporciona contexto suficiente para comprobar bien el idioma."
    )

    report = build_translation_quality_report(
        source,
        translated,
        source_language="en",
        target_language="es",
    )

    assert report.total_issues == 0


def test_translation_report_does_not_treat_a_name_catalogue_as_untranslated_prose() -> None:
    source = "\n".join(
        (
            "| DECAN | EGYPTIAN NAME | DEITY |",
            "| --- | --- | --- |",
            "| Virgo | Thoptius | Osiris |",
            "| Libra | Serecuth | Zeuda |",
            "| Scorpio | Sentacer | Arimanius |",
            "| Sagittarius | Eregbuo | Tolmophta |",
            "| Capricorn | Themeso | Soda |",
            "| Aquarius | Oroasoer | Brondeus |",
            "| Pisces | Archatapias | Rephan |",
        )
    )

    report = build_aligned_translation_quality_report(
        (source,),
        (source,),
        source_language="en",
        target_language="es",
    )

    assert TranslationIssueKind.SOURCE_TEXT not in report.issues_by_kind


def test_translation_report_does_not_treat_a_bibliography_as_untranslated_prose() -> None:
    source = (
        "BIBLIOGRAPHY. Ada Author. The Complete Book of Stars, translated by Bea Editor, "
        "University Press. Carla Writer. Ancient Astronomy, edited by Dan Scholar, London "
        "Academic Press. Eva Researcher. The Planetary Journal, revised edition, Cambridge "
        "University Press."
    )

    report = build_aligned_translation_quality_report(
        (source,),
        (source,),
        source_language="en",
        target_language="es",
    )

    assert TranslationIssueKind.SOURCE_TEXT not in report.issues_by_kind


def test_translation_report_flags_a_residual_english_yo_possessive() -> None:
    report = build_aligned_translation_quality_report(
        ("CHOOSE YO' CHARACTER",),
        ("ELIGE YO' PERSONAJE",),
        source_language="en",
        target_language="es",
    )

    assert report.issues_by_kind[TranslationIssueKind.SOURCE_TEXT] == 1
    assert report.review_segment_numbers == (1,)


def test_repairs_a_residual_english_yo_possessive_as_one_aligned_unit() -> None:
    source = "CHOOSE YO' CHARACTER"
    translated = "ELIGE YO' PERSONAJE"
    requested: list[tuple[str, str]] = []

    def translate_segment(source_fragment: str, current_fragment: str) -> str:
        requested.append((source_fragment, current_fragment))
        return "ELIGE TU PERSONAJE"

    repair = repair_untranslated_source_text(
        source,
        translated,
        source_language="en",
        target_language="es",
        translate_segment=translate_segment,
    )

    assert requested == [(source, translated)]
    assert repair.repaired_segments == 1
    assert repair.translated == "ELIGE TU PERSONAJE"


def test_translation_report_does_not_hide_a_long_uppercase_title() -> None:
    source = "THE COMPLETE PRACTICAL GUIDE TO PLANETARY CONDITIONS AND THEIR EFFECTS ON DAILY LIFE"

    report = build_aligned_translation_quality_report(
        (source,),
        (source,),
        source_language="en",
        target_language="es",
    )

    assert TranslationIssueKind.SOURCE_TEXT in report.issues_by_kind


def test_translation_report_checks_prose_after_an_index_heading() -> None:
    source = (
        "INDEX\n\n"
        "The following section explains how planetary conditions influence every daily decision."
    )

    report = build_aligned_translation_quality_report(
        (source,),
        (source,),
        source_language="en",
        target_language="es",
    )

    assert TranslationIssueKind.SOURCE_TEXT in report.issues_by_kind


def test_translation_report_does_not_count_repeated_citation_hints_as_distinct() -> None:
    source = (
        "London appears in this ordinary sentence because London shaped the account and London "
        "remains essential to its conclusion."
    )

    report = build_aligned_translation_quality_report(
        (source,),
        (source,),
        source_language="en",
        target_language="es",
    )

    assert TranslationIssueKind.SOURCE_TEXT in report.issues_by_kind


def test_translation_report_checks_prose_mixed_with_bibliographic_entries() -> None:
    source = (
        "BIBLIOGRAPHY. The Complete Book of Stars, translated by Bea Editor, University Press. "
        "Ancient Astronomy, edited by Dan Scholar, London Academic Press. "
        "This ordinary explanatory sentence still needs a complete Spanish translation."
    )

    report = build_aligned_translation_quality_report(
        (source,),
        (source,),
        source_language="en",
        target_language="es",
    )

    assert TranslationIssueKind.SOURCE_TEXT in report.issues_by_kind


def test_translation_report_accepts_a_simple_press_citation() -> None:
    source = "The Quiet Path Through Inner Freedom, Meridian Press."

    report = build_aligned_translation_quality_report(
        (source,),
        (source,),
        source_language="en",
        target_language="es",
    )

    assert TranslationIssueKind.SOURCE_TEXT not in report.issues_by_kind


def test_translation_report_accepts_a_lowercase_index_entry_with_a_folio() -> None:
    source = "INDEX\n- planetary conditions and daily decisions 142"

    report = build_aligned_translation_quality_report(
        (source,),
        (source,),
        source_language="en",
        target_language="es",
    )

    assert TranslationIssueKind.SOURCE_TEXT not in report.issues_by_kind


def test_translation_report_accepts_collapsed_index_entries_with_folios() -> None:
    source = "planetary conditions 142; daily decisions 148; practical examples 153"

    report = build_aligned_translation_quality_report(
        (source,),
        (source,),
        source_language="en",
        target_language="es",
    )

    assert TranslationIssueKind.SOURCE_TEXT not in report.issues_by_kind


def test_translation_report_accepts_prefixed_collapsed_index_entries() -> None:
    source = "INDEX. planetary conditions 142; daily decisions 148; practical examples 153"

    report = build_aligned_translation_quality_report(
        (source,),
        (source,),
        source_language="en",
        target_language="es",
    )

    assert TranslationIssueKind.SOURCE_TEXT not in report.issues_by_kind


def test_translation_report_accepts_one_compact_bibliography_entry() -> None:
    source = (
        "BIBLIOGRAPHY. Smith, John. a history of practical astronomy. "
        "New York: Meridian Press, 2020."
    )

    report = build_aligned_translation_quality_report(
        (source,),
        (source,),
        source_language="en",
        target_language="es",
    )

    assert TranslationIssueKind.SOURCE_TEXT not in report.issues_by_kind


def test_translation_report_checks_prose_after_a_compact_bibliography_entry() -> None:
    source = (
        "BIBLIOGRAPHY. Smith, John. a history of practical astronomy. "
        "New York: Meridian Press, 2020. "
        "This explanatory sentence after the citation still needs translation."
    )

    report = build_aligned_translation_quality_report(
        (source,),
        (source,),
        source_language="en",
        target_language="es",
    )

    assert TranslationIssueKind.SOURCE_TEXT in report.issues_by_kind


def test_translation_report_does_not_treat_a_numbered_title_as_an_index_entry() -> None:
    heading = "THE COMPLETE PRACTICAL GUIDE TO PERSONAL FREEDOM 2024"
    source = f"{heading}\n\nThis paragraph explains the guide."
    translated = f"{heading}\n\nEste párrafo explica la guía."

    report = build_aligned_translation_quality_report(
        (source,),
        (translated,),
        source_language="en",
        target_language="es",
    )

    assert TranslationIssueKind.SOURCE_TEXT in report.issues_by_kind


def test_translation_report_does_not_treat_a_bulleted_year_as_an_index_folio() -> None:
    residual = "- This agreement remains fully effective until 2024"
    source = f"{residual}\n\nThis paragraph explains the agreement."
    translated = f"{residual}\n\nEste párrafo explica el acuerdo."

    report = build_aligned_translation_quality_report(
        (source,),
        (translated,),
        source_language="en",
        target_language="es",
    )

    assert TranslationIssueKind.SOURCE_TEXT in report.issues_by_kind


def test_translation_report_does_not_treat_a_numbered_bullet_as_an_index_entry() -> None:
    residual = "- Read the complete agreement before page 42"
    source = f"{residual}\n\nThis paragraph explains the agreement."
    translated = f"{residual}\n\nEste párrafo explica el acuerdo."

    report = build_aligned_translation_quality_report(
        (source,),
        (translated,),
        source_language="en",
        target_language="es",
    )

    assert TranslationIssueKind.SOURCE_TEXT in report.issues_by_kind


def test_translation_report_does_not_treat_prose_table_cells_as_a_name_catalogue() -> None:
    source = (
        "| The artisan makes a useful instrument | The traveller crosses the river | "
        "The teacher explains every symbol |"
    )
    translated = (
        "| The artisan makes a useful instrument | El viajero cruza el río | "
        "El profesor explica cada símbolo |"
    )

    report = build_aligned_translation_quality_report(
        (source,),
        (translated,),
        source_language="en",
        target_language="es",
    )

    assert TranslationIssueKind.SOURCE_TEXT in report.issues_by_kind


def test_translation_report_does_not_treat_title_case_prose_as_a_name_catalogue() -> None:
    source = (
        "| Practical Guidance For Daily Life | Careful Choices Create Better Results | "
        "Every Person Can Change Today |"
    )
    translated = (
        "| Practical Guidance For Daily Life | "
        "Las decisiones cuidadosas crean mejores resultados | "
        "Cada persona puede cambiar hoy |"
    )

    report = build_aligned_translation_quality_report(
        (source,),
        (translated,),
        source_language="en",
        target_language="es",
    )

    assert TranslationIssueKind.SOURCE_TEXT in report.issues_by_kind


def test_translation_report_finds_a_long_heading_on_an_otherwise_translated_page() -> None:
    heading = (
        "THE COMPLETE PRACTICAL GUIDE TO PLANETARY CONDITIONS AND THEIR MANY EFFECTS ON "
        "EVERY IMPORTANT DECISION THROUGHOUT ORDINARY DAILY LIFE AND WORK"
    )
    source = f"{heading}\n\nThis paragraph explains the first practical consequence."
    translated = f"{heading}\n\nEste párrafo explica la primera consecuencia práctica."

    report = build_aligned_translation_quality_report(
        (source,),
        (translated,),
        source_language="en",
        target_language="es",
    )

    assert TranslationIssueKind.SOURCE_TEXT in report.issues_by_kind


def test_translation_report_still_flags_untranslated_prose_inside_a_table() -> None:
    source = (
        "DECAN IMAGE POWER. Capricorn. A woman carries a sealed letter across the city. "
        "Aquarius. A careful artisan makes a useful instrument for the community. "
        "Pisces. A traveller crosses the river and returns safely before night."
    )
    translated = (
        "DECANO IMAGEN PODER. Capricornio. Una mujer lleva una carta sellada por la ciudad. "
        "Acuario. A careful artisan makes a useful instrument for the community. "
        "Piscis. Un viajero cruza el río y regresa sano y salvo antes de la noche."
    )

    report = build_aligned_translation_quality_report(
        (source,),
        (translated,),
        source_language="en",
        target_language="es",
    )

    assert TranslationIssueKind.SOURCE_TEXT in report.issues_by_kind


def test_aligned_translation_report_ignores_a_bare_web_identifier() -> None:
    report = build_aligned_translation_quality_report(
        ("publisher.example.com .",),
        ("publisher.example.com .",),
        source_language="en",
        target_language="es",
    )

    assert not any(issue.kind is TranslationIssueKind.SOURCE_TEXT for issue in report.issues)


def test_aligned_translation_report_ignores_text_already_in_the_target_language() -> None:
    spanish = "La vida se abre cuando aceptamos plenamente este momento."

    report = build_aligned_translation_quality_report(
        (spanish,),
        (spanish,),
        source_language="en",
        target_language="es",
    )

    assert not any(issue.kind is TranslationIssueKind.SOURCE_TEXT for issue in report.issues)


def test_translation_report_accepts_a_translated_connector_between_names() -> None:
    source = "Morganfield by Michael A. Singer"
    unchanged = build_aligned_translation_quality_report(
        (source,),
        (source,),
        source_language="en",
        target_language="es",
    )
    translated = build_aligned_translation_quality_report(
        (source,),
        ("Morganfield por Michael A. Singer",),
        source_language="en",
        target_language="es",
    )

    assert any(issue.kind is TranslationIssueKind.SOURCE_TEXT for issue in unchanged.issues)
    assert not any(issue.kind is TranslationIssueKind.SOURCE_TEXT for issue in translated.issues)


def test_aligned_report_can_preserve_a_literal_emphasized_work_title() -> None:
    source = "THE QUIET PATH THROUGH INNER FREEDOM"
    translated = source

    assert is_literal_work_title_translation(
        source,
        translated,
        source_language="en",
        target_language="es",
    )
    report = build_aligned_translation_quality_report(
        (source,),
        (translated,),
        source_language="en",
        target_language="es",
        literal_work_title_segments=(1,),
    )

    assert TranslationIssueKind.SOURCE_TEXT not in report.issues_by_kind


def test_literal_work_title_accepts_only_a_localized_leading_article() -> None:
    assert is_literal_work_title_translation(
        "The Quiet Mountain Beyond Thought",
        "El Quiet Mountain Beyond Thought",
        source_language="en",
        target_language="es",
    )
    assert not is_literal_work_title_translation(
        "The Quiet Mountain Beyond Thought",
        "El Quiet Montaña Beyond Thought",
        source_language="en",
        target_language="es",
    )


def test_rejects_and_reports_an_explicit_ambiguity_reversal() -> None:
    source = "Small conversion errors may be corrected only when their meaning is unambiguous."
    reversed_translation = (
        "Los errores pequeños de conversión solo pueden corregirse cuando su significado "
        "es ambiguo."
    )

    with pytest.raises(TranslationQualityError, match="certeza o ambigüedad"):
        validate_translation_quality(
            source,
            reversed_translation,
            source_language="en",
            target_language="es",
            preserve_paragraphs=True,
        )

    report = build_translation_quality_report(
        source,
        reversed_translation,
        source_language="en",
        target_language="es",
    )

    assert report.total_issues == 1
    assert report.issues[0].kind is TranslationIssueKind.FIDELITY


def test_accepts_and_can_repair_the_same_ambiguity_polarity() -> None:
    source = "Small conversion errors may be corrected only when their meaning is unambiguous."
    reversed_translation = (
        "Los errores pequeños de conversión solo pueden corregirse cuando su significado "
        "es ambiguo."
    )
    corrected = (
        "Los errores pequeños de conversión solo pueden corregirse cuando su significado "
        "es inequívoco."
    )

    repair = repair_untranslated_source_text(
        source,
        reversed_translation,
        source_language="en",
        target_language="es",
        translate_segment=lambda _source, _current: corrected,
    )

    assert repair.attempted_segments == 1
    assert repair.repaired_segments == 1
    assert repair.translated == corrected
    validate_translation_quality(
        source,
        corrected,
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )


def test_translation_report_flags_an_extreme_segment_length_without_blocking() -> None:
    source = "A substantial original sentence with useful meaning. " * 5
    translated = "Frase breve."

    report = build_translation_quality_report(
        source,
        translated,
        source_language="en",
        target_language="es",
    )

    assert report.total_issues >= 1
    assert any(issue.kind is TranslationIssueKind.LENGTH for issue in report.issues)


def test_rejects_a_short_cover_title_expanded_with_invented_paragraphs() -> None:
    source = "36 FACES The History, Astrology and Magic of the Decans Austin Coppock"
    hallucinated = (
        "36 CARAS. La historia, la astrología y la magia de los decanos. Austin Coppock. "
        "Esta introducción inventada desarrolla una explicación extensa que no aparece en "
        "la portada original y añade datos, conclusiones y contexto completamente nuevos. "
        "También incorpora otro párrafo artificial para superar con claridad el límite seguro."
    )

    with pytest.raises(TranslationQualityError, match="duplicado o añadido contenido"):
        validate_translation_quality(
            source,
            hallucinated,
            source_language="en",
            target_language="es",
            preserve_paragraphs=True,
        )

    report = build_translation_quality_report(
        source,
        hallucinated,
        source_language="en",
        target_language="es",
    )

    assert any(issue.kind is TranslationIssueKind.LENGTH for issue in report.issues)


def test_translation_report_bounds_excerpts_and_visible_issue_count() -> None:
    source = "\n\n".join(f"The unchanged source sentence number {index}." for index in range(30))

    report = build_translation_quality_report(
        source,
        source,
        source_language="en",
        target_language="es",
    )

    assert report.total_issues > len(report.issues)
    assert len(report.issues) == 20
    assert sum(report.issues_by_kind.values()) == report.total_issues
    assert report.issues_by_kind[TranslationIssueKind.SOURCE_TEXT] >= 20
    assert all(
        len(issue.original_excerpt) <= MAX_REPORT_EXCERPT_CHARACTERS for issue in report.issues
    )


def test_aligned_translation_report_does_not_shift_after_an_internal_blank_line() -> None:
    source_segments = (
        "The first substantial source segment contains enough language for a reliable review.",
        "The second substantial source segment also remains independently aligned for review.",
    )
    translated_segments = (
        "El primer segmento traducido contiene suficiente lenguaje.\n\n"
        "Su segunda frase continúa dentro de la misma unidad estructural.",
        "El segundo segmento traducido también permanece alineado de forma independiente.",
    )

    report = build_aligned_translation_quality_report(
        source_segments,
        translated_segments,
        source_language="en",
        target_language="es",
    )

    assert report.checked_segments == 2
    assert TranslationIssueKind.ALIGNMENT not in report.issues_by_kind
    assert TranslationIssueKind.LENGTH not in report.issues_by_kind


def test_repairs_only_an_aligned_block_with_source_language_residue() -> None:
    source = (
        "The first complete paragraph explains the purpose of the document clearly.\n\n"
        "The second complete paragraph remains in English and needs one focused retry."
    )
    translated = (
        "El primer párrafo completo explica claramente el propósito del documento.\n\n"
        "The second complete paragraph remains in English and needs one focused retry."
    )
    calls: list[tuple[str, str]] = []

    def retry(source_block: str, current_block: str) -> str:
        calls.append((source_block, current_block))
        return "El segundo párrafo completo necesitaba un único reintento específico."

    repair = repair_untranslated_source_text(
        source,
        translated,
        source_language="en",
        target_language="es",
        translate_segment=retry,
    )

    assert repair.attempted_segments == 1
    assert repair.repaired_segments == 1
    assert len(calls) == 1
    assert calls[0][0].startswith("The second")
    assert "El segundo párrafo" in repair.translated
    assert "The second complete" not in repair.translated


def test_pdf_report_and_repair_align_by_page_when_paragraph_counts_change() -> None:
    source = (
        "<!-- PZDOC PDF PAGE 1 -->\n\n"
        "The first complete paragraph explains the page clearly.\n\n"
        "Another source paragraph contains useful context.\n\n"
        "<!-- PZDOC PDF PAGE 2 -->\n\n"
        "The second complete paragraph remains in English and needs one focused retry."
    )
    translated = (
        "<!-- PZDOC PDF PAGE 1 -->\n\n"
        "El primer párrafo explica claramente la página y otro párrafo aporta contexto.\n\n"
        "<!-- PZDOC PDF PAGE 2 -->\n\n"
        "The second complete paragraph remains in English and needs one focused retry."
    )
    calls: list[tuple[str, str]] = []

    def retry(source_page: str, current_page: str) -> str:
        calls.append((source_page, current_page))
        return current_page.replace(
            "The second complete paragraph remains in English and needs one focused retry.",
            "El segundo párrafo completo necesitaba un único reintento específico.",
        )

    report = build_translation_quality_report(
        source,
        translated,
        source_language="en",
        target_language="es",
    )
    repair = repair_untranslated_source_text(
        source,
        translated,
        source_language="en",
        target_language="es",
        translate_segment=retry,
        preserve_paragraphs=False,
    )

    assert report.checked_segments == 2
    assert not any(issue.kind is TranslationIssueKind.ALIGNMENT for issue in report.issues)
    assert any(issue.kind is TranslationIssueKind.SOURCE_TEXT for issue in report.issues)
    assert len(calls) == 1
    assert calls[0][0].startswith("The second complete paragraph")
    assert "PZDOC PDF PAGE 2" not in calls[0][0]
    assert repair.attempted_segments == 1
    assert repair.repaired_segments == 1
    assert "The second complete" not in repair.translated


def test_pdf_report_keeps_empty_pages_so_later_review_pairs_do_not_shift() -> None:
    source = (
        "<!-- PZDOC PDF PAGE 1 -->\n\n"
        "<!-- PZDOC PDF PAGE 2 -->\n\n"
        "DEDICATION TO MY FAMILY AND FRIENDS\n\n"
        "<!-- PZDOC PDF PAGE 3 -->\n\n"
        "This acknowledgements paragraph remains completely in English and needs review."
    )
    translated = (
        "<!-- PZDOC PDF PAGE 1 -->\n\n"
        "SÍMBOLOS Y GLIFOS\n\n"
        "<!-- PZDOC PDF PAGE 2 -->\n\n"
        "DEDICATORIA A MI FAMILIA Y AMIGOS\n\n"
        "<!-- PZDOC PDF PAGE 3 -->\n\n"
        "This acknowledgements paragraph remains completely in English and needs review."
    )

    report = build_translation_quality_report(
        source,
        translated,
        source_language="en",
        target_language="es",
    )

    assert report.checked_segments == 3
    assert not any(issue.kind is TranslationIssueKind.ALIGNMENT for issue in report.issues)
    residual = next(
        issue for issue in report.issues if issue.kind is TranslationIssueKind.SOURCE_TEXT
    )
    assert "acknowledgements paragraph" in residual.original_excerpt
    assert "acknowledgements paragraph" in residual.translated_excerpt


def test_pdf_repair_targets_one_residual_paragraph_instead_of_the_whole_page() -> None:
    source = (
        "<!-- PZDOC PDF PAGE 1 -->\n\n"
        "The opening publication paragraph identifies the edition and its publisher clearly.\n\n"
        "The cover image represents the astronomical ceiling and explains its provenance.\n\n"
        "The closing publication paragraph states the final rights and permissions."
    )
    translated = (
        "<!-- PZDOC PDF PAGE 1 -->\n\n"
        "El parrafo inicial identifica claramente la edicion y su editorial.\n\n"
        "The cover image represents the astronomical ceiling and explains its provenance.\n\n"
        "El parrafo final indica los derechos y permisos de la publicacion."
    )
    calls: list[tuple[str, str]] = []

    def retry(source_paragraph: str, current_paragraph: str) -> str:
        calls.append((source_paragraph, current_paragraph))
        return "La imagen de portada representa el techo astronomico y explica su procedencia."

    repair = repair_untranslated_source_text(
        source,
        translated,
        source_language="en",
        target_language="es",
        translate_segment=retry,
    )

    assert calls == [
        (
            "The cover image represents the astronomical ceiling and explains its provenance.",
            "The cover image represents the astronomical ceiling and explains its provenance.",
        )
    ]
    assert repair.attempted_segments == 1
    assert repair.repaired_segments == 1
    assert "The cover image" not in repair.translated
    assert "PZDOC PDF PAGE 1" in repair.translated


def test_pdf_repair_finds_an_exact_residual_when_paragraph_counts_differ() -> None:
    source = (
        "<!-- PZDOC PDF PAGE 1 -->\n\n"
        "The opening publication paragraph identifies the edition and its publisher clearly.\n\n"
        "The cover image represents the astronomical ceiling and explains its provenance.\n\n"
        "The closing publication paragraph states the final rights and permissions."
    )
    translated = (
        "<!-- PZDOC PDF PAGE 1 -->\n\n"
        "El parrafo inicial identifica la edicion y el parrafo final indica los permisos.\n\n"
        "The cover image represents the astronomical ceiling and explains its provenance."
    )
    calls: list[tuple[str, str]] = []

    repair = repair_untranslated_source_text(
        source,
        translated,
        source_language="en",
        target_language="es",
        translate_segment=lambda source_paragraph, current_paragraph: (
            calls.append((source_paragraph, current_paragraph))
            or "La imagen de portada representa el techo astronomico y explica su procedencia."
        ),
    )

    assert len(calls) == 1
    assert calls[0][0] == calls[0][1]
    assert calls[0][0].startswith("The cover image")
    assert repair.attempted_segments == 1
    assert repair.repaired_segments == 1
    assert "The cover image" not in repair.translated
    assert repair.translated.count("\n\n") == translated.count("\n\n")


def test_pdf_repair_ignores_soft_line_wrap_differences_in_residual_prose() -> None:
    source = (
        "<!-- PZDOC PDF PAGE 1 -->\n\n"
        "The opening publication paragraph identifies the edition clearly.\n\n"
        "The cover image represents the astronomical ceiling\n"
        "and explains its provenance for readers.\n\n"
        "The closing publication paragraph states the final permissions."
    )
    translated = (
        "<!-- PZDOC PDF PAGE 1 -->\n\n"
        "El parrafo inicial identifica la edicion y el parrafo final indica los permisos.\n\n"
        "The cover image represents the astronomical ceiling and explains its provenance "
        "for readers."
    )
    calls: list[tuple[str, str]] = []

    repair = repair_untranslated_source_text(
        source,
        translated,
        source_language="en",
        target_language="es",
        translate_segment=lambda source_paragraph, current_paragraph: (
            calls.append((source_paragraph, current_paragraph))
            or "La imagen de portada representa el techo astronomico y explica su procedencia."
        ),
    )

    assert len(calls) == 1
    assert "\n" in calls[0][0]
    assert "\n" not in calls[0][1]
    assert repair.repaired_segments == 1
    assert "The cover image" not in repair.translated


def test_pdf_repair_keeps_a_safe_sentence_fix_when_another_page_still_needs_review() -> None:
    first = "The first residual sentence can be translated safely and completely."
    second = "The second residual sentence still requires a human decision."
    source = f"<!-- PZDOC PDF PAGE 1 -->\n\n{first}\n\n<!-- PZDOC PDF PAGE 2 -->\n\n{second}"
    translated = source

    repair = repair_untranslated_source_text(
        source,
        translated,
        source_language="en",
        target_language="es",
        translate_segment=lambda source_fragment, current_fragment: (
            "La primera frase residual puede traducirse de forma segura y completa."
            if first in source_fragment
            else current_fragment
        ),
    )

    assert repair.attempted_segments == 2
    assert repair.repaired_segments == 1
    assert first not in repair.translated
    assert second in repair.translated


def test_repair_keeps_a_safe_title_fix_when_another_title_still_needs_review() -> None:
    first = "CONTENTS"
    second = "EXPOSITION OF REALITY"
    source = f"{first}\n\n{second}"

    repair = repair_untranslated_source_text(
        source,
        source,
        source_language="en",
        target_language="es",
        translate_segment=lambda source_fragment, current_fragment: (
            "CONTENIDO" if source_fragment == first else current_fragment
        ),
    )

    assert repair.attempted_segments == 2
    assert repair.repaired_segments == 1
    assert first not in repair.translated
    assert second in repair.translated


def test_pdf_repair_can_target_one_residual_sentence_inside_a_mixed_page() -> None:
    residual = "This sentence was preserved in English after a failed chunk."
    source = (
        "<!-- PZDOC PDF PAGE 1 -->\n\n"
        f"{residual} A second source sentence also contains useful context."
    )
    translated = (
        "<!-- PZDOC PDF PAGE 1 -->\n\n"
        f"{residual} Una segunda frase ya se tradujo correctamente al español."
    )
    calls: list[tuple[str, str]] = []

    repair = repair_untranslated_source_text(
        source,
        translated,
        source_language="en",
        target_language="es",
        translate_segment=lambda source_fragment, current_fragment: (
            calls.append((source_fragment, current_fragment))
            or "Esta frase se conservó en inglés tras fallar un fragmento."
        ),
    )

    assert calls == [(residual, residual)]
    assert repair.repaired_segments == 1
    assert residual not in repair.translated
    assert "Una segunda frase ya se tradujo" in repair.translated


def test_repairs_a_short_unchanged_source_sentence() -> None:
    calls: list[tuple[str, str]] = []

    repair = repair_untranslated_source_text(
        "Modern EPUB content.",
        "Modern EPUB content.",
        source_language="en",
        target_language="es",
        translate_segment=lambda source, current: (
            calls.append((source, current)) or "Contenido EPUB moderno."
        ),
    )

    assert calls == [("Modern EPUB content.", "Modern EPUB content.")]
    assert repair.translated == "Contenido EPUB moderno."
    assert repair.attempted_segments == 1
    assert repair.repaired_segments == 1


def test_repairs_source_residue_when_ocr_collapses_word_spacing() -> None:
    source = "YO' MONEY YO' MONEY YO' MONEY LEVEL UP"
    collapsed = "YO' MONEYYO' MONEYYO' MONEYLEVELUP"
    calls: list[tuple[str, str]] = []

    repair = repair_untranslated_source_text(
        source,
        collapsed,
        source_language="en",
        target_language="es",
        translate_segment=lambda source_fragment, current_fragment: (
            calls.append((source_fragment, current_fragment))
            or "TU DINERO, TU DINERO, TU DINERO: SUBE DE NIVEL"
        ),
    )

    assert calls == [(source, collapsed)]
    assert repair.attempted_segments == 1
    assert repair.repaired_segments == 1
    assert collapsed not in repair.translated


def test_repairs_one_residual_sentence_inside_a_non_pdf_paragraph() -> None:
    residual = "The second complete sentence still needs a focused translation."
    source = f"The first sentence is already handled. {residual}"
    translated = f"La primera frase ya está resuelta. {residual}"
    calls: list[tuple[str, str]] = []

    repair = repair_untranslated_source_text(
        source,
        translated,
        source_language="en",
        target_language="es",
        translate_segment=lambda source_fragment, current_fragment: (
            calls.append((source_fragment, current_fragment))
            or "La segunda frase completa aún necesita una traducción específica."
        ),
    )

    assert calls == [(residual, residual)]
    assert repair.attempted_segments == 1
    assert repair.repaired_segments == 1
    assert "La primera frase ya está resuelta." in repair.translated
    assert residual not in repair.translated


def test_repairs_a_residual_sentence_between_protected_xml_markers() -> None:
    opening = "<!-- PZDOC_EPUB_XML_A_A_XZQ -->"
    closing = "<!-- PZDOC_EPUB_XML_A_B_XZQ -->"
    residual = "The second complete sentence still needs a focused translation."
    source = f"{opening}The first sentence is already handled. {residual}{closing}"
    translated = f"{opening}La primera frase ya está resuelta. {residual}{closing}"
    calls: list[tuple[str, str]] = []

    repair = repair_untranslated_source_text(
        source,
        translated,
        source_language="en",
        target_language="es",
        translate_segment=lambda source_fragment, current_fragment: (
            calls.append((source_fragment, current_fragment))
            or "La segunda frase completa recibe una traducción localizada."
        ),
    )

    assert calls == [(residual, residual)]
    assert repair.repaired_segments == 1
    assert residual not in repair.translated
    assert opening in repair.translated
    assert closing in repair.translated


def test_repairs_a_residual_sentence_after_whitespace_normalization() -> None:
    residual_source = "The  second complete sentence still needs a focused translation."
    residual_current = "The second complete sentence still needs a focused translation."
    source = f"The first sentence is already handled. {residual_source}"
    translated = f"La primera frase ya está resuelta. {residual_current}"
    calls: list[tuple[str, str]] = []

    repair = repair_untranslated_source_text(
        source,
        translated,
        source_language="en",
        target_language="es",
        translate_segment=lambda source_fragment, current_fragment: (
            calls.append((source_fragment, current_fragment))
            or "La segunda frase completa recibe una traducción localizada."
        ),
    )

    assert calls == [(residual_current, residual_current)]
    assert repair.repaired_segments == 1
    assert residual_current not in repair.translated


def test_repairs_whitespace_around_a_protected_epub_marker() -> None:
    marker = "<!-- PZDOC_EPUB_XML_A_B_XZQ -->"
    residual_source = f"The  second {marker} complete sentence still needs translation."
    residual_current = f"The second {marker} complete sentence still needs translation."
    source = f"The first sentence is already handled. {residual_source}"
    translated = f"La primera frase ya está resuelta. {residual_current}"

    repair = repair_untranslated_source_text(
        source,
        translated,
        source_language="en",
        target_language="es",
        translate_segment=lambda _source, _current: (
            f"La segunda {marker} frase completa recibe una traducción localizada."
        ),
    )

    assert repair.repaired_segments == 1
    assert residual_current not in repair.translated
    assert repair.translated.count(marker) == 1


def test_rejects_an_unsafe_automatic_repair_and_keeps_the_review_warning() -> None:
    source = "The original agreement keeps reference 2026 and every important condition."
    translated = source

    repair = repair_untranslated_source_text(
        source,
        translated,
        source_language="en",
        target_language="es",
        translate_segment=lambda _source, _current: (
            "El acuerdo traducido cambia la referencia 2027 y conserva cada condición."
        ),
    )

    assert repair.attempted_segments == 1
    assert repair.repaired_segments == 0
    assert repair.translated == translated
    report = build_translation_quality_report(
        source,
        repair.translated,
        source_language="en",
        target_language="es",
    )
    assert any(issue.kind is TranslationIssueKind.SOURCE_TEXT for issue in report.issues)


def test_does_not_automatically_repair_ambiguous_length_warnings() -> None:
    source = "A substantial original sentence with useful meaning. " * 5
    translated = "Esta es una frase muy breve en español."

    repair = repair_untranslated_source_text(
        source,
        translated,
        source_language="en",
        target_language="es",
        translate_segment=lambda _source, _current: pytest.fail(
            "Los avisos de longitud deben seguir siendo solo revisables."
        ),
    )

    assert repair.attempted_segments == 0
    assert repair.repaired_segments == 0
    assert repair.translated == translated


def test_finds_high_confidence_source_words_before_translation() -> None:
    source = "Although his scholarship brought recognition, Coppock remained independent."

    assert translation_quality_module.source_words_requiring_translation(source, "en") == (
        "although",
        "scholarship",
    )
    assert translation_quality_module.source_words_requiring_focused_translation(source, "en") == (
        "scholarship",
    )


def test_source_word_preflight_does_not_classify_capitalized_names_by_suffix() -> None:
    source = "A note from Fellowship Press accompanies the chart."

    assert translation_quality_module.source_words_requiring_translation(source, "en") == ()


def test_established_term_translation_is_available_before_document_generation() -> None:
    assert translation_quality_module.established_term_translation("rulership", "en", "es") == (
        "regencia"
    )
    assert translation_quality_module.established_term_translation("unknown", "en", "es") is None
    assert (
        translation_quality_module.established_term_translation("cusps", "en", "es") == "cúspides"
    )
    assert (
        translation_quality_module.replace_established_term_residues(
            "The remaining house cusps continue around the chart.",
            "Las cusps restantes de las casas continúan alrededor de la carta.",
            "en",
            "es",
        )
        == "Las cúspides restantes de las casas continúan alrededor de la carta."
    )
    assert (
        translation_quality_module.established_term_translation(
            "Triplicity Rulerships",
            "en",
            "es",
        )
        == "regencias por triplicidad"
    )
    assert (
        translation_quality_module.established_term_translation(
            "Triplicity Lords",
            "en",
            "es",
        )
        == "señores de la triplicidad"
    )
    assert (
        translation_quality_module.established_term_translation(
            "Science of Judgment",
            "en",
            "es",
        )
        == "ciencia del juicio"
    )
    assert (
        translation_quality_module.established_term_translation(
            "The Science of Judgment",
            "en",
            "es",
        )
        == "la ciencia del juicio"
    )
    assert (
        translation_quality_module.established_term_translation(
            "The Art of Judgment",
            "en",
            "es",
        )
        == "el arte del juicio"
    )
    assert (
        translation_quality_module.established_term_translation(
            "Part Six: The Art of Judgment",
            "en",
            "es",
        )
        == "parte seis: el arte del juicio"
    )
    assert (
        translation_quality_module.established_term_translation(
            "Primary Source Readings",
            "en",
            "es",
        )
        == "lecturas de fuentes primarias"
    )
    assert (
        translation_quality_module.established_term_translation(
            "Delineating a Planet in a House",
            "en",
            "es",
        )
        == "delineación de un planeta en una casa"
    )
    assert (
        translation_quality_module.established_term_translation(
            "Benefic and Malefic Planets, Conditions, and Houses",
            "en",
            "es",
        )
        == "planetas benéficos y maléficos, condiciones y casas"
    )
    assert (
        translation_quality_module.established_term_translation(
            "The Tenth House",
            "en",
            "es",
        )
        == "la décima casa"
    )
    assert (
        translation_quality_module.established_term_translation(
            "Zodiacal Sign Rulerships",
            "en",
            "es",
        )
        == "regencias de los signos zodiacales"
    )
    assert (
        translation_quality_module.established_term_translation(
            "The Synodic Cycle",
            "en",
            "es",
        )
        == "el ciclo sinódico"
    )
    assert (
        translation_quality_module.established_term_translation(
            "Hermetic Lots",
            "en",
            "es",
        )
        == "lotes herméticos"
    )
    assert (
        translation_quality_module.established_term_translation(
            "Cadent  Triplicity Lords ofthe Sect Light",
            "en",
            "es",
        )
        == "señores cadentes de la triplicidad de la luminaria de la secta"
    )
    assert (
        translation_quality_module.replace_established_term_residues(
            "PART SEVEN: DOWN  TO  EARTH",
            "PART SEVEN: DOWN  TO  EARTH",
            "en",
            "es",
        )
        == "PARTE SIETE: CON LOS PIES EN LA TIERRA"
    )
    assert (
        translation_quality_module.established_compact_label_translation(
            "PART SEVEN: DOWN TO EARTH",
            "en",
            "es",
        )
        == "PARTE SIETE: CON LOS PIES EN LA TIERRA"
    )
    assert (
        translation_quality_module.established_compact_label_translation(
            "Triplicity Lords of the Sect Light, Chart Two",
            "en",
            "es",
        )
        == "señores de la triplicidad de la luminaria de la secta, carta dos"
    )
    assert (
        translation_quality_module.established_compact_label_translation(
            "Goddess (Thea)",
            "en",
            "es",
        )
        == "diosa (Thea)"
    )
    assert (
        translation_quality_module.established_compact_label_translation(
            "Introducing the Lots",
            "en",
            "es",
        )
        is None
    )
    assert translation_quality_module.is_probable_third_language_compact_value(
        "kakos daimòn",
        source_language="en",
        target_language="es",
    )
    assert not translation_quality_module.is_probable_third_language_compact_value(
        "Subterranean Place",
        source_language="en",
        target_language="es",
    )
    assert not translation_quality_module.is_probable_third_language_compact_value(
        "Idle",
        source_language="en",
        target_language="es",
    )
    assert translation_quality_module.established_terms_requiring_translation(
        "Planetary Rulership and Virgo",
        "en",
        "es",
    ) == ("rulership",)
    assert translation_quality_module.established_terms_requiring_translation(
        "15. TRIPLICITY RULERSHIPS 199",
        "en",
        "es",
    )[:1] == ("triplicity rulerships",)
    assert translation_quality_module.established_terms_requiring_translation(
        "Other Uses of the Triplicity Lords 202",
        "en",
        "es",
    )[:1] == ("the triplicity lords",)
    assert translation_quality_module.contains_established_translation_candidate(
        "Planetary Rulership"
    )
    assert (
        translation_quality_module.established_compact_label_translation(
            "Delineating Planetary Meaning",
            "en",
            "es",
        )
        == "interpretación del significado planetario"
    )
    assert (
        translation_quality_module.established_compact_label_translation(
            "The Relative Angularity of the Houses",
            "en",
            "es",
        )
        == "la angularidad relativa de las casas"
    )
    assert (
        translation_quality_module.established_compact_label_translation(
            "Angularity, Favorability, Testimony",
            "en",
            "es",
        )
        == "angularidad, favorabilidad y testimonio"
    )
    assert (
        translation_quality_module.established_compact_label_translation(
            "Delineations for Chart Two",
            "en",
            "es",
        )
        == "interpretaciones de la carta dos"
    )


def test_title_residue_detection_does_not_reject_a_target_language_derivative() -> None:
    assert (
        translation_quality_module.find_titles_with_source_language_residue(
            "- 13. PLANETARY RECEPTION 177",
            "- 13. RECEPCIÓN PLANETARIA 177",
            "en",
        )
        == ()
    )


def test_title_residue_detection_requires_complete_ocr_joined_source_words() -> None:
    source = (
        "# VOLUME TWO: DELINEATING PLANETARY MEANING\n\n# THE RELATIVE ANGULARITY OF THE HOUSES"
    )
    translated = (
        "# Volumen Dos: Definición del Significado Planetario\n\n"
        "# LA ANGULARDAD RELATIVA DE LAS CASAS"
    )

    assert not translation_quality_module._has_title_language_hint(
        "Volumen Dos: Definición del Significado Planetario",
        "en",
    )
    assert not translation_quality_module._has_title_language_hint(
        "LA ANGULARDAD RELATIVA DE LAS CASAS",
        "en",
    )
    assert not translation_quality_module._has_title_language_hint(
        "ASPECTOS",
        "en",
    )
    assert translation_quality_module._has_title_language_hint(
        "PARTSEVEN: DOWNTOEARTH",
        "en",
    )
    assert (
        translation_quality_module.find_titles_with_source_language_residue(
            source,
            translated,
            "en",
        )
        == ()
    )
