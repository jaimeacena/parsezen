from __future__ import annotations

import pytest

from parsezen.epub_builder import EPUB_CHAPTER_MARKER, plan_epub
from parsezen.epub_structure import EditableEpubStructure


def _book() -> str:
    return "# First\n\nOpening.\n\n## Details\n\nMore details.\n\n## Second part\n\nSecond body.\n"


def test_explicit_structure_can_split_rename_reorder_and_merge_chapters() -> None:
    structure = EditableEpubStructure(_book(), "Book")

    second = structure.split_at_heading(0, 9)
    structure.rename(second, None, "Renamed second")
    structure.move_chapter(second, -1)

    assert structure.chapter_count == 2
    assert structure.chapter_title(0) == "Renamed second"
    assert EPUB_CHAPTER_MARKER in structure.markdown()
    plan = plan_epub(structure.markdown(), "Book")
    assert [chapter.title for chapter in plan.chapters] == ["Renamed second", "First"]

    structure.merge_with_previous(1)
    assert structure.chapter_count == 1


def test_structure_can_add_chapter_and_adjust_heading_hierarchy() -> None:
    structure = EditableEpubStructure(_book(), "Book")
    added = structure.add_chapter(0, "Appendix")
    assert structure.chapter_title(added) == "Appendix"

    original_level = structure.headings(0)[1].level
    structure.adjust_heading_level(0, structure.headings(0)[1].line_number, 1)

    assert structure.headings(0)[1].level == original_level + 1
    assert len(plan_epub(structure.markdown(), "Book").chapters) == 2


def test_explicit_epub_markers_control_short_chapters_but_never_enter_chapter_text() -> None:
    markdown = f"# One\n\nShort.\n\n{EPUB_CHAPTER_MARKER}\n\n# Two\n\nAlso short.\n"

    plan = plan_epub(markdown, "Book")

    assert len(plan.chapters) == 2
    assert all(EPUB_CHAPTER_MARKER not in chapter.markdown for chapter in plan.chapters)


def test_headingless_book_uses_fallback_and_can_name_its_only_division() -> None:
    structure = EditableEpubStructure("Opening text without a heading.", "Fallback title")

    assert structure.chapter_title(0) == "Fallback title"

    structure.rename(0, None, "Named division")

    assert structure.chapter_title(0) == "Named division"
    assert structure.chapter_markdown(0).startswith("# Named division\n\nOpening text")


def test_invalid_structure_edits_fail_without_touching_book_content() -> None:
    structure = EditableEpubStructure(_book(), "Book")
    original = structure.markdown()

    with pytest.raises(ValueError, match="vacío"):
        structure.rename(0, None, " ")
    with pytest.raises(ValueError, match="ya no existe"):
        structure.rename(0, 999, "Missing")
    with pytest.raises(ValueError, match="no es un título"):
        structure.rename(0, 2, "Not a heading")
    with pytest.raises(ValueError, match="vacío"):
        structure.add_chapter(0, "\0")
    with pytest.raises(ValueError, match="dentro del capítulo"):
        structure.split_at_heading(0, 1)
    with pytest.raises(ValueError, match="anterior"):
        structure.merge_with_previous(0)
    assert structure.move_chapter(0, -1) == 0
    with pytest.raises(ValueError, match="ya no existe"):
        structure.adjust_heading_level(0, 999, 1)
    with pytest.raises(ValueError, match="no es un título"):
        structure.adjust_heading_level(0, 2, 1)

    assert structure.markdown() == original
