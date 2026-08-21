from __future__ import annotations

from parsezen.semantic_blocks import (
    SemanticRole,
    analyze_markdown,
    discover_document_terms,
    reconcile_document_evidence,
)


def test_semantic_block_identity_survives_an_insertion_before_it() -> None:
    original = analyze_markdown("Alpha paragraph.\n\nBeta paragraph.\n")
    expanded = analyze_markdown("New introduction.\n\nAlpha paragraph.\n\nBeta paragraph.\n")

    original_ids = {block.markdown.strip(): block.identifier for block in original.blocks}
    expanded_ids = {block.markdown.strip(): block.identifier for block in expanded.blocks}

    assert expanded_ids["Alpha paragraph."] == original_ids["Alpha paragraph."]
    assert expanded_ids["Beta paragraph."] == original_ids["Beta paragraph."]


def test_semantic_analysis_distinguishes_front_matter_toc_and_body() -> None:
    markdown = (
        "# The Example Book\n\n"
        "Jane Doe\n\n"
        "Copyright 2026 Example Press. All rights reserved.\n\n"
        "# Contents\n\n"
        "Chapter One ........ 1\n\n"
        "Chapter Two ........ 9\n\n"
        "# Chapter One\n\n" + "This is the opening body paragraph. " * 12 + "\n"
    )

    document = analyze_markdown(markdown)
    roles = [block.role for block in document.blocks]

    assert roles[:3] == [SemanticRole.FRONT_MATTER] * 3
    assert roles[3:6] == [SemanticRole.TOC] * 3
    assert roles[6] is SemanticRole.HEADING
    assert roles[7] is SemanticRole.BODY
    assert document.front_matter_blocks == 3
    assert document.toc_blocks == 3


def test_semantic_analysis_recognizes_a_translated_index_with_reflowable_entries() -> None:
    markdown = (
        "# Example\n\n"
        "# Index\n\n"
        "- Introduction — 9\n"
        "- Chapter One — 12\n\n"
        "# Chapter One\n\n" + "Opening body paragraph. " * 12 + "\n"
    )

    document = analyze_markdown(markdown)

    assert document.toc_blocks == 2
    assert [block.role for block in document.blocks][1:3] == [SemanticRole.TOC] * 2


def test_document_term_memory_keeps_only_repeated_proper_terms() -> None:
    terms = discover_document_terms(
        "Written by Austin Coppock. "
        "Later, Austin Coppock explains the same method. "
        "This Ordinary Sentence appears only once."
    )

    assert [(term.text, term.occurrences) for term in terms] == [("Austin Coppock", 2)]


def test_document_term_memory_ignores_uppercase_headings_and_roman_numerals() -> None:
    terms = discover_document_terms(
        "THE HISTORY OF FACES\n\nPART II\n\nARIES\n\nTHE HISTORY OF FACES\n\nPART II\n\nARIES\n"
    )

    assert terms == ()


def test_document_term_memory_does_not_freeze_repeated_technical_labels() -> None:
    terms = discover_document_terms(
        "Rejoicing by Zodiacal Sign 95\n"
        "Rejoicing by Solar Phase 99\n"
        "Bonification by Domicile Lord 185\n\n"
        "Rejoicing by Zodiacal Sign 205\n"
        "Rejoicing by Solar Phase 209\n"
        "Bonification by Domicile Lord 285\n"
    )

    assert terms == ()


def test_document_term_memory_accepts_plain_by_only_as_a_standalone_byline() -> None:
    terms = discover_document_terms(
        "By Austin Coppock\n\nAustin Coppock explains the method in this paragraph."
    )

    assert [(term.text, term.occurrences) for term in terms] == [("Austin Coppock", 2)]


def test_document_evidence_repairs_one_strict_proper_spelling_outlier() -> None:
    source = (
        "Horimaea appears in the first reference.\n"
        "Horimaea appears in the second reference.\n"
        "Horimaea appears in the third reference.\n"
        "Horimaea appears in the fourth reference.\n"
        "Horimiea is one isolated extraction error.\n"
    )

    reconciled, changes = reconcile_document_evidence(source)

    assert changes == 1
    assert "Horimiea" not in reconciled
    assert reconciled.count("Horimaea") == 5


def test_document_evidence_does_not_rewrite_ambiguous_or_protected_spellings() -> None:
    source = (
        "Alexandra appears four times: Alexandra Alexandra Alexandra.\n"
        "Alexandria is a distinct place.\n"
        "`Alexandrx` and https://example.test/Alexandrx stay protected.\n"
    )

    reconciled, changes = reconcile_document_evidence(source)

    assert changes == 0
    assert reconciled == source


def test_document_evidence_uses_a_unique_body_heading_to_repair_its_toc_label() -> None:
    source = (
        "# Contents\n\n"
        "Delineating Planetery Meaning ........ 12\n\n"
        "# Delineating Planetary Meaning\n\n"
        "Opening body text.\n"
    )

    reconciled, changes = reconcile_document_evidence(source)

    assert changes == 1
    assert "Delineating Planetary Meaning ........ 12" in reconciled


def test_document_evidence_repairs_generated_toc_table_without_losing_markup() -> None:
    source = (
        '<table class="document-toc">\n'
        '<thead><tr><th class="toc-label">Entry</th>'
        '<th class="toc-folio">Page</th></tr></thead>\n'
        '<tbody><tr><td class="toc-label toc-level-1">'
        '<strong><em><a href="#page-12">Delineating Planetery Meaning</a></em></strong>'
        '</td><td class="toc-folio">12</td></tr></tbody>\n'
        "</table>\n\n"
        "# Delineating Planetary Meaning\n"
    )

    reconciled, changes = reconcile_document_evidence(source)

    assert changes == 1
    assert (
        '<strong><em><a href="#page-12">Delineating Planetary Meaning</a></em></strong>'
        in reconciled
    )
