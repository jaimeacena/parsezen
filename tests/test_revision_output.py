from __future__ import annotations

from pathlib import Path

import pytest

from parsezen.application.review_plan import review_steps_for_result
from parsezen.domain.jobs import JobConfiguration, ProcessingPlan
from parsezen.epub_builder import EpubBookMetadata, iter_epub_text_documents
from parsezen.errors import RequestValidationError
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


def test_reviewed_output_rejects_an_unsafe_numeric_mix_before_writing(tmp_path: Path) -> None:
    original = "Primera referencia 202.\n\nSegunda referencia 260.\n"
    proposed = "# Primera referencia 202.\n\n## Segunda referencia 260.\n"
    draft = build_revision_draft(
        original,
        proposed,
        kinds=frozenset({RevisionKind.STRUCTURE}),
    )
    destination = tmp_path / "book.mended.md"
    destination.write_text(original, encoding="utf-8")
    result = ProcessResult(destination, revision_draft=draft)

    with pytest.raises(RequestValidationError, match="numéricos de forma insegura"):
        apply_reviewed_revision(
            result,
            "# Primera referencia 260.\n\n## Segunda referencia 260.\n",
        )

    assert destination.read_text(encoding="utf-8") == original


def test_hallucinated_insertion_has_no_correction_gate_and_publishes_original(
    tmp_path: Path,
) -> None:
    original = "Original conservado.\n"
    draft = build_revision_draft(
        original,
        "Original conservado.\n\nPárrafo inventado.\n",
        kinds=frozenset({RevisionKind.CONTENT}),
    )
    destination = tmp_path / "book.mended.md"
    destination.write_text(original, encoding="utf-8")
    result = ProcessResult(
        destination,
        revision_draft=draft,
        review_markdown=original,
        review_required=False,
    )

    assert (
        review_steps_for_result(
            result,
            JobConfiguration(plan=ProcessingPlan.LOCAL_AI_REVIEWED),
        )
        == ()
    )
    assert draft.render() == original
    apply_reviewed_revision(result, original)

    assert destination.read_text(encoding="utf-8") == original
