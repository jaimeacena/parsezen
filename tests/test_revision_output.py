from __future__ import annotations

from pathlib import Path

from parsezen.epub_builder import EpubBookMetadata, iter_epub_text_documents
from parsezen.processing import ProcessResult, apply_reviewed_revision
from parsezen.revision import RevisionKind, build_revision_draft


def _draft():
    return build_revision_draft(
        "# Original\n\nText.\n",
        "# Proposed\n\nCorrected text.\n",
        kinds=frozenset({RevisionKind.CONTENT}),
    )


def test_reviewed_markdown_replaces_only_the_app_created_result(tmp_path: Path) -> None:
    destination = tmp_path / "book.mended.md"
    destination.write_text(_draft().original_markdown, encoding="utf-8")
    result = ProcessResult(destination, revision_draft=_draft())

    updated = apply_reviewed_revision(result, "# User choice\n")

    assert destination.read_text(encoding="utf-8") == "# User choice\n"
    assert updated.revision_approved


def test_reviewed_epub_is_rebuilt_from_the_approved_markdown(tmp_path: Path) -> None:
    destination = tmp_path / "book.epub"
    destination.write_bytes(b"temporary")
    result = ProcessResult(
        destination,
        revision_draft=_draft(),
        revision_epub_metadata=EpubBookMetadata("Book", "en"),
    )

    updated = apply_reviewed_revision(result, _draft().proposed_markdown)

    xhtml = "\n".join(
        content for _name, content in iter_epub_text_documents(destination.read_bytes())
    )
    assert "Corrected text." in xhtml
    assert updated.revision_approved
    assert updated.epub_chapters == 1
