from __future__ import annotations

import pytest

import parsezen.translation_quality as translation_quality_module
from parsezen.translation_quality import (
    MAX_REPORT_EXCERPT_CHARACTERS,
    NUMBER_PATTERN,
    TranslationIssueKind,
    TranslationQualityError,
    build_translation_quality_report,
    detect_language_code,
    natural_language_text,
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


def test_accepts_a_complete_translation_with_preserved_markdown() -> None:
    validate_translation_quality(
        SOURCE,
        SPANISH,
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


def test_rejects_a_short_title_left_in_the_source_language() -> None:
    source = SOURCE.replace("# Contract", "# IMPORTANT CONTRACT")
    translated = SPANISH.replace("# Contrato", "# IMPORTANT CONTRACT")

    with pytest.raises(TranslationQualityError, match="título o encabezado"):
        validate_translation_quality(
            source,
            translated,
            source_language="en",
            target_language="es",
            preserve_paragraphs=True,
        )


def test_rejects_an_untranslated_title_below_the_general_language_threshold() -> None:
    with pytest.raises(TranslationQualityError, match="título o encabezado"):
        validate_translation_quality(
            "30 DAY MILLIONAIRE CHALLENGE\n\nShort English text.",
            "30 DAY MILLIONAIRE CHALLENGE\n\nTexto breve en español.",
            source_language="en",
            target_language="es",
            preserve_paragraphs=True,
        )


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


def test_rejects_an_unchanged_uppercase_subject_heading() -> None:
    with pytest.raises(TranslationQualityError, match="título o encabezado"):
        validate_translation_quality(
            "# PLANETARY CONDITION\n\nA sufficiently long English paragraph explains the source.",
            "# PLANETARY CONDITION\n\nUn párrafo suficientemente largo explica la fuente.",
            source_language="en",
            target_language="es",
            preserve_paragraphs=True,
        )


def test_accepts_an_unchanged_publisher_name() -> None:
    validate_translation_quality(
        "THREE HANDS PRESS",
        "THREE HANDS PRESS",
        source_language="en",
        target_language="es",
        preserve_paragraphs=True,
    )


def test_rejects_a_partially_untranslated_short_title() -> None:
    with pytest.raises(TranslationQualityError, match="parte de un título"):
        validate_translation_quality(
            "30 DAY MILLIONAIRE CHALLENGE",
            "30 días MILLIONAIRE CHALLENGE",
            source_language="en",
            target_language="es",
            preserve_paragraphs=True,
        )


def test_rejects_partially_untranslated_short_titles_in_a_dense_index() -> None:
    source = "SCORPIO III 186\nSAGITTARIUS I 192\nPISCES I 244\nAPPENDICES 258"
    translated = "SCORPION III 186\n\nSAGITARIO I 192\nPISCIS I 244\nAPÉNDICES 258"

    with pytest.raises(TranslationQualityError, match="parte de un título"):
        validate_translation_quality(
            source,
            translated,
            source_language="en",
            target_language="es",
            preserve_paragraphs=False,
        )


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


def test_rejects_a_changed_roman_numeral_in_an_index_reference() -> None:
    with pytest.raises(TranslationQualityError, match="números romanos"):
        validate_translation_quality(
            "LIBRA III 168",
            "LIBRA TERCERA 168",
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
    assert all(
        len(issue.original_excerpt) <= MAX_REPORT_EXCERPT_CHARACTERS for issue in report.issues
    )


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
