from __future__ import annotations

import pytest

from parsezen.errors import ImprovementError
from parsezen.revision import (
    RevisionDecision,
    RevisionKind,
    RevisionRisk,
    build_revision_draft,
    markdown_headings,
    markdown_outline_tree,
    set_heading_level,
    split_markdown_blocks,
    validate_revision_selection,
)


def test_outline_tree_explains_when_no_headings_exist() -> None:
    assert markdown_outline_tree("A plain paragraph.") == "Sin títulos detectados"


def test_revision_draft_can_accept_and_reject_independent_changes() -> None:
    original = "# Título\n\nPrimer texto.\n\nSegundo texto.\n"
    proposed = "# Título\n\nPrimer texto corregido.\n\nSegundo texto.\n"

    draft = build_revision_draft(
        original,
        proposed,
        kinds=frozenset({RevisionKind.CONTENT}),
    )

    assert len(draft.changes) == 1
    assert draft.render() == proposed
    assert draft.render({draft.changes[0].identifier: RevisionDecision.REJECTED}) == original


def test_structure_only_heading_change_is_classified_as_structure() -> None:
    draft = build_revision_draft(
        "Capítulo uno\n\nTexto.\n",
        "# Capítulo uno\n\nTexto.\n",
        kinds=frozenset({RevisionKind.CONTENT, RevisionKind.STRUCTURE}),
    )

    assert len(draft.changes) == 1
    assert draft.changes[0].kind is RevisionKind.STRUCTURE


def test_extreme_heading_level_change_is_rejected_by_default() -> None:
    draft = build_revision_draft(
        "# Capítulo uno\n\nTexto.\n",
        "###### Capítulo uno\n\nTexto.\n",
        kinds=frozenset({RevisionKind.STRUCTURE}),
    )

    assert draft.changes[0].risk is RevisionRisk.HIGH
    assert draft.changes[0].recommended_decision is RevisionDecision.REJECTED
    assert draft.render() == draft.original_markdown


def test_structure_revision_that_changes_a_number_is_rejected_by_default() -> None:
    draft = build_revision_draft(
        "Entrada del índice 202.\n",
        "# Entrada del índice 260.\n",
        kinds=frozenset({RevisionKind.STRUCTURE}),
    )

    assert draft.changes[0].kind is RevisionKind.STRUCTURE
    assert draft.changes[0].risk is RevisionRisk.HIGH
    assert draft.changes[0].recommended_decision is RevisionDecision.REJECTED
    assert draft.render() == draft.original_markdown


def test_revision_selection_rejects_numeric_counts_outside_both_safe_versions() -> None:
    draft = build_revision_draft(
        "Primera referencia 202.\n\nSegunda referencia 260.\n",
        "Primera referencia 202.\n\n## Segunda referencia 260.\n",
        kinds=frozenset({RevisionKind.STRUCTURE}),
    )

    validate_revision_selection(draft, draft.render())
    with pytest.raises(ImprovementError, match="numéricos de forma insegura"):
        validate_revision_selection(
            draft,
            "Primera referencia 260.\n\n## Segunda referencia 260.\n",
        )


def test_markdown_blocks_do_not_split_inside_fences() -> None:
    blocks = split_markdown_blocks("Antes.\n\n```\na\n\nb\n```\n\nDespués.\n")

    assert len(blocks) == 3
    assert "a\n\nb" in blocks[1].markdown


def test_heading_outline_and_level_edit() -> None:
    markdown = "Título\n\n## Sección\n\nTexto\n"

    edited = set_heading_level(markdown, 1, 1)
    edited = set_heading_level(edited, 3, None)

    assert edited == "# Título\n\nSección\n\nTexto\n"
    assert markdown_headings(edited) == ((1, "Título"),)


def test_revision_input_limits_and_empty_documents_are_safe(monkeypatch) -> None:
    unchanged = build_revision_draft(
        "Sin cambios.\n",
        "Sin cambios.\n",
        kinds=frozenset({RevisionKind.CONTENT}),
    )
    assert unchanged.render() == "Sin cambios.\n"
    assert split_markdown_blocks("") == ()
    with pytest.raises(ImprovementError, match="no es válido"):
        split_markdown_blocks("bad\0text")

    monkeypatch.setattr("parsezen.revision._MAX_REVIEW_MARKDOWN_CHARACTERS", 3)
    with pytest.raises(ImprovementError, match="demasiado grande"):
        split_markdown_blocks("four")
    assert [
        block.markdown for block in split_markdown_blocks("four", enforce_review_limit=False)
    ] == ["four"]


def test_revision_summarizes_insertions_and_deletions() -> None:
    inserted = build_revision_draft(
        "Primero.\n\n",
        "Primero.\n\nAñadido.\n",
        kinds=frozenset({RevisionKind.CONTENT}),
    )
    deleted = build_revision_draft(
        "Primero.\n\nEliminado.\n",
        "Primero.\n\n",
        kinds=frozenset({RevisionKind.CONTENT}),
    )

    assert inserted.changes[0].summary == "Contenido propuesto para añadir"
    assert deleted.changes[0].summary == "Contenido propuesto para eliminar"
    assert inserted.changes[0].risk is RevisionRisk.HIGH
    assert deleted.changes[0].risk is RevisionRisk.HIGH
    assert inserted.render() == inserted.original_markdown
    assert deleted.render() == deleted.original_markdown


def test_revision_keeps_risky_numbers_names_and_large_rewrites_original_by_default() -> None:
    numbered = build_revision_draft(
        "El estudio de Austin Coppock contiene 36 capítulos y mantiene todas sus referencias.\n",
        "El estudio de Austin Cooper contiene 35 capítulos y mantiene todas sus referencias.\n",
        kinds=frozenset({RevisionKind.CONTENT}),
    )
    shortened = build_revision_draft(
        (
            "Este párrafo conserva una explicación extensa con todos los matices, ejemplos, "
            "condiciones, nombres y conclusiones necesarios para entender correctamente la obra.\n"
        ),
        "Este párrafo resume la obra.\n",
        kinds=frozenset({RevisionKind.CONTENT}),
    )

    assert numbered.changes[0].risk is RevisionRisk.HIGH
    assert numbered.changes[0].recommended_decision is RevisionDecision.REJECTED
    assert numbered.render() == numbered.original_markdown
    assert shortened.changes[0].risk is RevisionRisk.HIGH
    assert shortened.render() == shortened.original_markdown


def test_heading_word_change_is_high_risk_and_bulk_default_keeps_original() -> None:
    draft = build_revision_draft(
        "# The History\n",
        "# Che History\n",
        kinds=frozenset({RevisionKind.CONTENT}),
    )

    assert draft.changes[0].risk is RevisionRisk.HIGH
    assert draft.changes[0].recommended_decision is RevisionDecision.REJECTED
    assert draft.render() == draft.original_markdown


def test_validated_bilingual_heading_correction_is_recommended() -> None:
    source = "# The History, Astrology and Magic of the Decans\n\nBy\n\nAustin Coppock\n"
    original = (
        "# Historia del Ajedrez; Astrología y Magia de los Decanos\n\nKor\n\nAustin Coppock\n"
    )
    proposed = "# Historia, Astrología y Magia de los Decanos\n\nPor\n\nAustin Coppock\n"

    draft = build_revision_draft(
        original,
        proposed,
        kinds=frozenset({RevisionKind.CONTENT}),
        translation_source_markdown=source,
        translation_target_language="es",
        protected_translation_terms=("Austin Coppock",),
    )

    assert draft.changes[0].risk is RevisionRisk.LOW
    assert draft.changes[0].recommended_decision is RevisionDecision.ACCEPTED
    assert draft.render() == proposed


def test_validated_bilingual_correction_remains_recommended_with_safe_heading_changes() -> None:
    source = "# The History, Astrology and Magic of the Decans\n\nBy\n\nAustin Coppock\n"
    original = (
        "# Historia del Ajedrez; Astrología y Magia de los Decanos\n\nKor\n\nAustin Coppock\n"
    )
    proposed = "## Historia, Astrología y Magia de los Decanos\n\n# Por\n\nAustin Coppock\n"

    draft = build_revision_draft(
        original,
        proposed,
        kinds=frozenset({RevisionKind.CONTENT, RevisionKind.STRUCTURE}),
        translation_source_markdown=source,
        translation_target_language="es",
        protected_translation_terms=("Austin Coppock",),
    )

    assert draft.changes[0].risk is RevisionRisk.LOW
    assert draft.changes[0].recommended_decision is RevisionDecision.ACCEPTED
    assert draft.render() == proposed


def test_bilingual_correction_cannot_change_a_shared_source_name() -> None:
    draft = build_revision_draft(
        "Austin Coppock escribió este libro.\n",
        "Austin Cooper escribió este libro.\n",
        kinds=frozenset({RevisionKind.CONTENT}),
        translation_source_markdown="Austin Coppock wrote this book.\n",
        translation_target_language="es",
    )

    assert draft.changes[0].risk is RevisionRisk.HIGH
    assert draft.changes[0].recommended_decision is RevisionDecision.REJECTED


def test_revision_still_recommends_a_bounded_typographical_correction() -> None:
    draft = build_revision_draft(
        "Este texot contiene una errata pequeña.\n",
        "Este texto contiene una errata pequeña.\n",
        kinds=frozenset({RevisionKind.CONTENT}),
    )

    assert draft.changes[0].risk is RevisionRisk.LOW
    assert draft.changes[0].recommended_decision is RevisionDecision.ACCEPTED
    assert draft.render() == draft.proposed_markdown


def test_heading_helpers_ignore_fences_and_invalid_edits() -> None:
    markdown = "```\n# No es título\n```\n\n## Sí es título\n"

    assert markdown_headings(markdown) == ((2, "Sí es título"),)
    assert set_heading_level(markdown, 99, 1) == markdown
    assert set_heading_level("   \n", 1, 2) == "   \n"
    with pytest.raises(ValueError, match="between 1 and 6"):
        set_heading_level(markdown, 1, 7)
