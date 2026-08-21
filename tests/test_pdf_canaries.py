from __future__ import annotations

from pathlib import Path

from parsezen.pdf_conversion import convert_pdf_document
from pdf_canaries import write_canary_suite


def test_synthetic_pdf_canaries_preserve_contents_columns_and_emphasis(tmp_path: Path) -> None:
    canaries = write_canary_suite(tmp_path / "canaries")

    contents = convert_pdf_document(canaries["contents"]).markdown
    columns = convert_pdf_document(canaries["columns"]).markdown
    formatting = convert_pdf_document(canaries["formatting"]).markdown

    assert '<table class="document-toc">' in contents
    assert 'class="toc-label toc-level-1"' in contents
    assert "<strong>1. FIRST CHAPTER</strong>" in contents
    assert "<em>Opening principles</em>" in contents
    assert columns.index("Left column final sentence") < columns.index(
        "Right column first sentence"
    )
    assert "# Bold source emphasis" in formatting
    assert "*Italic source emphasis*" in formatting
    assert "E = mc^2" in formatting
    assert "[12-14]" in formatting


def test_pdf_conversion_is_identical_with_and_without_native_page_resume(tmp_path: Path) -> None:
    source = write_canary_suite(tmp_path / "canaries")["contents"]
    checkpoints: dict[int, str] = {}

    def save_checkpoint(page: int, payload: str) -> bool:
        checkpoints[page] = payload
        return True

    first = convert_pdf_document(
        source,
        save_page_checkpoint=save_checkpoint,
    )
    resumed = convert_pdf_document(
        source,
        load_page_checkpoint=checkpoints.get,
    )

    assert resumed.markdown == first.markdown
    assert resumed.resources == first.resources
