from pathlib import Path, PurePosixPath

from parsezen.document_model import ConvertedResource
from parsezen.domain.jobs import MarkdownOrganization
from parsezen.markdown_export import prepare_markdown_export
from parsezen.output import replace_markdown_output, write_conversion_output


def test_chapter_export_builds_an_index_and_portable_relative_images(tmp_path: Path) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"pdf")
    markdown = """# A useful book

<!-- PZDOC PDF PAGE 1 -->

## First chapter

![Figure](<__parsezen_resources__/pdf/figure.png>)

<!-- PZDOC PDF PAGE 8 -->

## Second chapter

Final text.
"""
    resource = ConvertedResource(PurePosixPath("pdf/figure.png"), b"image", "image/png")

    result = write_conversion_output(
        source,
        markdown,
        resources=(resource,),
        markdown_organization=MarkdownOrganization.BY_CHAPTER,
        markdown_include_metadata=True,
        markdown_include_page_references=True,
    )

    assert result.name == "book.md"
    index = result.read_text(encoding="utf-8")
    assert 'source: "book.pdf"' in index
    assert "# A useful book" in index
    assert "[First chapter](<book.chapters/01-first-chapter.md>)" in index
    first = (tmp_path / "book.chapters" / "01-first-chapter.md").read_text(encoding="utf-8")
    assert "> Página original 1" in first
    assert "../book.assets/pdf/figure.png" in first
    assert (tmp_path / "book.assets" / "pdf" / "figure.png").read_bytes() == b"image"


def test_reviewed_chapter_export_replaces_companion_and_can_return_to_one_file(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "book.md"
    destination.write_text("old", encoding="utf-8")
    (tmp_path / "book.chapters").mkdir()
    (tmp_path / "book.chapters" / "old.md").write_text("old", encoding="utf-8")

    replace_markdown_output(
        destination,
        "# Book\n\n## One\n\nFirst.\n\n## Two\n\nSecond.",
        organization=MarkdownOrganization.BY_CHAPTER,
        source_name="source.pdf",
        include_metadata=False,
        include_page_references=False,
    )
    assert not (tmp_path / "book.chapters" / "old.md").exists()
    assert len(tuple((tmp_path / "book.chapters").glob("*.md"))) == 2

    replace_markdown_output(
        destination,
        "# Book\n\nOne complete document.",
        organization=MarkdownOrganization.BY_CHAPTER,
        source_name="source.pdf",
        include_metadata=False,
        include_page_references=False,
    )
    assert not (tmp_path / "book.chapters").exists()
    assert "One complete document" in destination.read_text(encoding="utf-8")


def test_single_file_export_keeps_private_markers_for_a_pending_review() -> None:
    exported = prepare_markdown_export(
        "<!-- PZDOC PDF PAGE 4 -->\n\nText",
        organization=MarkdownOrganization.SINGLE_FILE,
        source_name="source.pdf",
        include_metadata=False,
        include_page_references=False,
    )

    assert "PZDOC" in exported.primary_markdown
    assert exported.primary_markdown.rstrip().endswith("Text")
