from __future__ import annotations

import pytest

from parsezen.errors import RequestValidationError, TranslationError
from parsezen.glossary import (
    GlossaryEntry,
    glossary_fingerprint,
    protect_glossary,
    validate_glossary,
)


def test_glossary_protects_longest_terms_without_touching_code_or_links() -> None:
    entries = (
        GlossaryEntry("Source", "Destino"),
        GlossaryEntry("Parsezen", "Parsezen revisado"),
    )
    source = (
        "Parsezen ayuda al SOURCE. `Source`\n\n"
        "[Source](https://example.test/Source)\n\n"
        "```txt\nParsezen\n```"
    )

    protected = protect_glossary(source, entries)
    restored = protected.restore(protected.text)

    assert restored.startswith("Parsezen revisado ayuda al Destino.")
    assert "`Source`" in restored
    assert "[Destino](https://example.test/Source)" in restored
    assert "```txt\nParsezen\n```" in restored
    assert len(protected.replacements) == 3


def test_glossary_rejects_duplicates_and_missing_markers() -> None:
    with pytest.raises(RequestValidationError, match="repetido"):
        validate_glossary((GlossaryEntry("Term", "Uno"), GlossaryEntry("term", "Dos")))

    protected = protect_glossary("Term", (GlossaryEntry("Term", "Término"),))
    with pytest.raises(TranslationError, match="glosario"):
        protected.restore("marker removed")


def test_glossary_fingerprint_changes_without_exposing_terms() -> None:
    first = glossary_fingerprint((GlossaryEntry("Private", "Privado"),))
    second = glossary_fingerprint((GlossaryEntry("Private", "Confidencial"),))

    assert first != second
    assert "Private" not in first
    assert len(first) == 64


@pytest.mark.parametrize(
    "entries",
    [
        (GlossaryEntry("", "Target"),),
        (GlossaryEntry("Source", ""),),
        (GlossaryEntry("Source\nline", "Target"),),
        (GlossaryEntry("S" * 201, "Target"),),
    ],
)
def test_glossary_rejects_incomplete_or_oversized_entries(
    entries: tuple[GlossaryEntry, ...],
) -> None:
    with pytest.raises(RequestValidationError):
        validate_glossary(entries)


def test_empty_glossary_is_a_noop() -> None:
    protected = protect_glossary("Text", ())

    assert protected.text == "Text"
    assert protected.replacements == ()
    assert protected.restore("Text") == "Text"
