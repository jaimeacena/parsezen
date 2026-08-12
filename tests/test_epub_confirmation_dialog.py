from pathlib import Path

from PySide6.QtWidgets import QDialog

from parsezen.domain.books import BookDocument, BookMetadata, BookSection
from parsezen.infrastructure.artifact_store import ArtifactStore
from parsezen.presentation.epub_confirmation_dialog import EpubConfirmationDialog


def _book(tmp_path: Path) -> tuple[BookDocument, ArtifactStore]:
    store = ArtifactStore(tmp_path / "artifacts")
    chapter = store.put_text(
        job_id="job",
        text="<html xmlns='http://www.w3.org/1999/xhtml'><body><p>Text</p></body></html>",
        media_type="application/xhtml+xml",
    )
    book = BookDocument(
        BookMetadata("Original title", "en", "Original author"),
        (BookSection("chapter", "Chapter", chapter.id),),
        ("chapter",),
    )
    return book, store


def test_lightweight_confirmation_shows_every_essential_book_fact(qtbot, tmp_path: Path) -> None:
    book, store = _book(tmp_path)
    dialog = EpubConfirmationDialog(book, store, job_id="job")
    qtbot.addWidget(dialog)

    assert dialog.title_input.text() == "Original title"
    assert dialog.author_input.text() == "Original author"
    assert dialog.language_input.text() == "en"
    assert dialog.cover_value.text() == "Sin portada"
    assert dialog.chapter_value.text() == "1"


def test_lightweight_confirmation_updates_metadata_and_publishes(qtbot, tmp_path: Path) -> None:
    book, store = _book(tmp_path)
    dialog = EpubConfirmationDialog(book, store, job_id="job")
    qtbot.addWidget(dialog)
    dialog.title_input.setText("Final title")
    dialog.author_input.setText("Final author")
    dialog.language_input.setText("es")

    dialog.publish_button.click()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.book.metadata == BookMetadata("Final title", "es", "Final author")
    assert not dialog.open_editor_requested
    assert not dialog.saved_for_later


def test_full_editor_opens_only_when_explicitly_requested(qtbot, tmp_path: Path) -> None:
    book, store = _book(tmp_path)
    dialog = EpubConfirmationDialog(book, store, job_id="job")
    qtbot.addWidget(dialog)

    dialog.editor_button.click()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.open_editor_requested


def test_closing_confirmation_preserves_a_recoverable_draft(qtbot, tmp_path: Path) -> None:
    book, store = _book(tmp_path)
    dialog = EpubConfirmationDialog(book, store, job_id="job")
    qtbot.addWidget(dialog)
    dialog.title_input.setText("Saved draft")

    dialog.reject()

    assert dialog.result() == QDialog.DialogCode.Rejected
    assert dialog.saved_for_later
    assert dialog.book.metadata.title == "Saved draft"
