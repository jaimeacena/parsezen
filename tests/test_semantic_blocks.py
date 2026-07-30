from __future__ import annotations

from parsezen.semantic_blocks import (
    SemanticRole,
    analyze_markdown,
    discover_document_terms,
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
