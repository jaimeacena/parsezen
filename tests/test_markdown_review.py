from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QPdfWriter

import parsezen.presentation.markdown_review as review_module
from parsezen.errors import ReviewUnavailableError
from parsezen.pdf_conversion import PdfQualityReport, PdfReviewIssue
from parsezen.presentation.markdown_review import (
    LocalMarkdownView,
    MarkdownReviewDialog,
    PdfQualityReviewDialog,
    TranslationQualityReviewDialog,
    read_markdown_for_review,
    read_markdown_preview,
)
from parsezen.translation_quality import (
    TranslationIssueKind,
    TranslationQualityIssue,
    TranslationQualityReport,
)


def test_reads_bounded_utf8_markdown_for_local_review(tmp_path: Path) -> None:
    markdown = tmp_path / "result.md"
    markdown.write_text("# Resultado\n\nTexto con acentos.", encoding="utf-8")

    assert read_markdown_for_review(markdown) == "# Resultado\n\nTexto con acentos."


def test_explains_missing_invalid_or_excessive_review_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ReviewUnavailableError, match="Ya no existe missing.md"):
        read_markdown_for_review(tmp_path / "missing.md")

    invalid = tmp_path / "invalid.md"
    invalid.write_bytes(b"\xff")
    with pytest.raises(ReviewUnavailableError, match="No se pudo leer invalid.md"):
        read_markdown_for_review(invalid)

    monkeypatch.setattr(review_module, "MAX_REVIEW_CHARACTERS", 10)
    oversized = tmp_path / "oversized.md"
    oversized.write_text("12345678901", encoding="utf-8")
    with pytest.raises(ReviewUnavailableError, match="demasiado grande"):
        read_markdown_for_review(oversized)


def test_preview_only_loads_the_requested_prefix(tmp_path: Path) -> None:
    markdown = tmp_path / "long.md"
    markdown.write_text("abcdefghij", encoding="utf-8")

    text, truncated = read_markdown_preview(markdown, 5)

    assert text == "abcde"
    assert truncated


def test_dialog_compares_original_and_result_as_read_only_markdown(qtbot) -> None:
    dialog = MarkdownReviewDialog(
        "# Resultado\n\nTexto reparado.",
        "documento.mended.md",
        original_text="# Original\n\nTexto.",
        original_name="documento.raw.md",
    )
    qtbot.addWidget(dialog)

    assert dialog.windowTitle() == "Revisar Markdown"
    assert dialog.original_editor is not None
    assert dialog.original_editor.toPlainText() == "Original\nTexto."
    assert dialog.result_editor.toPlainText() == "Resultado\nTexto reparado."
    assert dialog.original_editor.isReadOnly()
    assert dialog.result_editor.isReadOnly()
    assert dialog.original_editor.accessibleName() == "Markdown anterior a la IA"
    assert dialog.result_editor.accessibleName() == "Markdown resultante"
    assert dialog.splitter.count() == 2
    assert dialog.close_button.text() == "Cerrar"


def test_dialog_uses_one_pane_when_there_is_no_pre_ai_markdown(qtbot) -> None:
    dialog = MarkdownReviewDialog("# Convertido", "documento.md")
    qtbot.addWidget(dialog)

    assert dialog.original_editor is None
    assert dialog.result_editor.toPlainText() == "Convertido"
    assert dialog.splitter.count() == 1


def test_markdown_view_renders_headings_and_github_tables(qtbot) -> None:
    dialog = MarkdownReviewDialog(
        "# Informe\n\n| Columna | Valor |\n|---|---:|\n| Uno | 1 |",
        "informe.md",
    )
    qtbot.addWidget(dialog)

    html = dialog.result_editor.toHtml()
    assert "Informe" in html
    assert "<table" in html
    assert "Columna" in html
    assert "Valor" in html


def test_book_preview_hides_internal_page_markers_and_keeps_paragraph_spacing(qtbot) -> None:
    view = LocalMarkdownView()
    qtbot.addWidget(view)

    view.set_markdown("Primer párrafo.\n\n<!-- PZDOC PDF PAGE 2 -->\n\nSegundo párrafo.")

    assert "PZDOC PDF PAGE" not in view.toPlainText()
    assert "Primer párrafo." in view.toPlainText()
    assert "Segundo párrafo." in view.toPlainText()
    assert "margin: 0 0 0.9em 0" in view.document().defaultStyleSheet()


def test_translation_quality_review_navigates_bounded_local_excerpts(qtbot) -> None:
    report = TranslationQualityReport(
        source_language="en",
        target_language="es",
        detected_language="es",
        checked_segments=12,
        source_characters=1_000,
        translated_characters=980,
        total_issues=2,
        issues=(
            TranslationQualityIssue(
                3,
                TranslationIssueKind.SOURCE_TEXT,
                "Parece conservar una frase en el idioma original.",
                "Original sentence.",
                "Original sentence.",
            ),
            TranslationQualityIssue(
                9,
                TranslationIssueKind.LENGTH,
                "La traducción es mucho más corta que el fragmento original.",
                "Long original excerpt.",
                "Texto breve.",
            ),
        ),
    )
    dialog = TranslationQualityReviewDialog(report)
    qtbot.addWidget(dialog)

    assert dialog.windowTitle() == "Revisar traducción"
    assert dialog.summary_label.text() == "12 fragmentos comprobados · 2 avisos"
    assert dialog.position_label.text() == "Fragmento 3 · 1 de 2"
    assert dialog.original_editor.toPlainText() == "Original sentence."
    assert dialog.translated_editor.toPlainText() == "Original sentence."
    assert dialog.original_editor.isReadOnly()
    assert dialog.translated_editor.isReadOnly()

    qtbot.mouseClick(dialog.next_button, Qt.MouseButton.LeftButton)

    assert dialog.position_label.text() == "Fragmento 9 · 2 de 2"
    assert dialog.translated_editor.toPlainText() == "Texto breve."
    assert not dialog.next_button.isEnabled()


def test_guided_pdf_review_navigates_issues_and_returns_the_retry_page(
    tmp_path: Path,
    qtbot,
) -> None:
    source = tmp_path / "book.pdf"
    _write_two_page_pdf(source)
    report = PdfQualityReport(
        processed_pages=(1, 2),
        ocr_pages=(2,),
        issues=(
            PdfReviewIssue(1, "Comprueba el encabezado.", "# Primer encabezado"),
            PdfReviewIssue(2, "Comprueba la tabla.", "| A | B |\n|---|---|\n| 1 | 2 |"),
        ),
    )
    dialog = PdfQualityReviewDialog(source, report, allow_retry=True)
    qtbot.addWidget(dialog)

    assert dialog.windowTitle() == "Revisar páginas"
    assert dialog.summary_label.text() == (
        "2 páginas procesadas · 1 analizada con OCR · 2 necesitan revisión"
    )
    assert dialog.page_label.text() == "Página 1 del PDF · 1 de 2"
    assert dialog.issue_label.text() == "Comprueba el encabezado."
    assert not dialog.page_preview._page_pixmap.isNull()
    assert not dialog.previous_button.isEnabled()
    assert dialog.next_button.isEnabled()

    qtbot.mouseClick(dialog.next_button, Qt.MouseButton.LeftButton)

    assert dialog.page_label.text() == "Página 2 del PDF · 2 de 2"
    assert dialog.issue_label.text() == "Comprueba la tabla."
    assert "A" in dialog.markdown_preview.toPlainText()
    assert not dialog.next_button.isEnabled()

    qtbot.mouseClick(dialog.retry_button, Qt.MouseButton.LeftButton)

    assert dialog.retry_page == 2


def test_guided_pdf_review_disables_a_repeated_exhaustive_retry(
    tmp_path: Path,
    qtbot,
) -> None:
    source = tmp_path / "book.pdf"
    _write_two_page_pdf(source)
    report = PdfQualityReport(
        processed_pages=(2,),
        ocr_pages=(2,),
        issues=(PdfReviewIssue(2, "Aún requiere comparación.", ""),),
    )
    dialog = PdfQualityReviewDialog(source, report, allow_retry=False)
    qtbot.addWidget(dialog)

    assert not dialog.retry_button.isEnabled()
    assert "No se reconoció texto legible" in dialog.markdown_preview.toPlainText()


def _write_two_page_pdf(destination: Path) -> None:
    writer = QPdfWriter(str(destination))
    painter = QPainter(writer)
    painter.drawText(100, 100, "Página uno")
    writer.newPage()
    painter.drawText(100, 100, "Página dos")
    painter.end()
