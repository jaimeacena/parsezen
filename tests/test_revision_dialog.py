from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QPdfWriter
from PySide6.QtWidgets import QInputDialog, QMessageBox, QPushButton

from parsezen.epub_builder import plan_epub
from parsezen.pdf_conversion import PdfQualityReport, PdfReviewIssue
from parsezen.presentation.design_system import COLORS
from parsezen.presentation.revision_dialog import (
    EpubStructureEditor,
    RevisionReviewDialog,
    _StructureActionButton,
)
from parsezen.revision import RevisionDecision, RevisionKind, build_revision_draft
from parsezen.translation_quality import (
    TranslationIssueKind,
    TranslationQualityIssue,
    TranslationQualityReport,
)


def _draft():
    return build_revision_draft(
        "# Title\n\nOriginal paragraph.\n",
        "# Better title\n\nCorrected paragraph.\n",
        kinds=frozenset({RevisionKind.CONTENT, RevisionKind.STRUCTURE}),
    )


def test_revision_dialog_starts_with_proposals_and_can_reject_them(qtbot) -> None:
    draft = _draft()
    dialog = RevisionReviewDialog(draft, "book.md")
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.wait(20)

    assert dialog.editor.toPlainText() == draft.proposed_markdown
    assert dialog.changes_list.count() == len(draft.changes)

    dialog.reject_all_button.click()

    assert dialog.editor.toPlainText() == draft.original_markdown
    assert all(
        dialog.changes_list.item(row).checkState() is Qt.CheckState.Unchecked
        for row in range(dialog.changes_list.count())
    )


def test_revision_dialog_keeps_manual_edits_when_saved(qtbot) -> None:
    dialog = RevisionReviewDialog(_draft(), "book.md")
    qtbot.addWidget(dialog)
    dialog.proposal_editor.setPlainText("# Final")
    while dialog.result() != dialog.DialogCode.Accepted:
        dialog.save_button.click()

    assert dialog.result() == dialog.DialogCode.Accepted
    assert "# Final" in dialog.final_text


def test_structure_review_can_change_the_current_heading_level(qtbot) -> None:
    dialog = RevisionReviewDialog(_draft(), "book.md")
    qtbot.addWidget(dialog)
    cursor = dialog.editor.textCursor()
    cursor.setPosition(0)
    dialog.editor.setTextCursor(cursor)
    dialog.heading_combo.setCurrentIndex(dialog.heading_combo.findData(2))

    dialog.apply_heading_button.click()

    assert dialog.editor.toPlainText().startswith("## Better title")


def test_individual_decision_and_plain_text_preview_are_local(qtbot) -> None:
    dialog = RevisionReviewDialog(_draft(), "book.txt", plain_text=True)
    qtbot.addWidget(dialog)
    item = dialog.changes_list.item(0)
    identifier = str(item.data(Qt.ItemDataRole.UserRole))

    item.setCheckState(Qt.CheckState.Unchecked)
    decisions = dialog.decisions
    decisions[identifier] = RevisionDecision.ACCEPTED
    dialog._show_change_detail(-1)
    dialog._refresh_preview()

    assert dialog.decisions[identifier] is RevisionDecision.REJECTED
    assert dialog.change_detail.text() == ""
    assert not dialog.heading_combo.isVisible()


def test_empty_manual_revision_is_not_saved(qtbot, monkeypatch) -> None:
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, title, message: warnings.append((title, message)),
    )
    dialog = RevisionReviewDialog(None, "book.md", initial_text=" \n")
    qtbot.addWidget(dialog)
    dialog.save_button.click()

    assert dialog.result() != dialog.DialogCode.Accepted
    assert warnings == [("Resultado vacío", "El resultado debe contener texto antes de guardarlo.")]


def test_manual_edits_are_not_overwritten_by_a_later_proposal_toggle(qtbot) -> None:
    dialog = RevisionReviewDialog(_draft(), "book.md")
    qtbot.addWidget(dialog)
    dialog.editor.appendPlainText("Manual note.")
    edited = dialog.editor.toPlainText()
    first = dialog.changes_list.item(0)

    first.setCheckState(Qt.CheckState.Unchecked)

    assert dialog.editor.toPlainText() == edited
    assert first.checkState() is Qt.CheckState.Checked
    assert "protegidos" in dialog.change_detail.text()


def test_manual_edit_in_one_step_does_not_disable_decisions_for_later_steps(qtbot) -> None:
    draft = build_revision_draft(
        "# Original\n\nTexto estable.\n\nPrimer bloque.\n\nSeparador.\n\nSegundo bloque.\n",
        (
            "# Propuesto\n\nTexto estable.\n\nPrimer bloque corregido.\n\n"
            "Separador.\n\nSegundo bloque corregido.\n"
        ),
        kinds=frozenset({RevisionKind.CONTENT, RevisionKind.STRUCTURE}),
    )
    assert len(draft.changes) >= 2
    dialog = RevisionReviewDialog(draft, "book.md")
    qtbot.addWidget(dialog)

    dialog.proposal_editor.insertPlainText("Mi edición manual.\n\n")

    assert dialog.proposal_choice_button.isChecked()
    assert dialog.confirm_review_button.isHidden()
    assert dialog.keep_original_button.isHidden()
    assert dialog.use_proposal_button.isHidden()
    dialog.save_button.click()

    assert dialog.changes_list.currentRow() == 1
    dialog.original_choice_button.click()
    dialog.save_button.click()

    assert "Mi edición manual." in dialog.editor.toPlainText()
    assert draft.changes[1].original_markdown.strip() in dialog.editor.toPlainText()


def test_unified_review_can_edit_translation_warnings_and_show_the_exact_epub_plan(qtbot) -> None:
    text = "# First\n\n" + "Translated text. " * 120 + "\n\n# Second\n\nFinal text."
    issue = TranslationQualityIssue(
        1,
        TranslationIssueKind.SOURCE_TEXT,
        "Conviene revisar este fragmento.",
        "Original text.",
        "Translated text.",
        identifier="translation-1",
    )
    report = TranslationQualityReport("en", "es", "es", 1, 14, 16, 1, (issue,))
    dialog = RevisionReviewDialog(
        None,
        "book.epub",
        initial_text=text,
        translation_report=report,
        epub_title="Book",
    )
    qtbot.addWidget(dialog)

    assert dialog.changes_list.count() == 2
    dialog.changes_list.setCurrentRow(0)
    assert dialog.result_tabs.isTabVisible(dialog.editor_tab_index)
    assert not dialog.result_tabs.isTabVisible(dialog.outline_tab_index)
    assert dialog.original_editor.toPlainText() == "Original text."
    assert dialog.proposal_editor.toPlainText() == "Translated text."
    plan = plan_epub(text, "Book")
    assert dialog.outline.topLevelItemCount() == len(plan.chapters) == 2

    dialog.proposal_editor.setPlainText("Edited translation.")
    dialog.save_button.click()
    assert dialog.result_tabs.isTabVisible(dialog.outline_tab_index)
    assert not dialog.result_tabs.isTabVisible(dialog.editor_tab_index)
    dialog.save_button.click()

    assert dialog.result() == dialog.DialogCode.Accepted
    assert "Edited translation." in dialog.final_text


def test_blocking_pdf_issue_requires_an_explicit_decision_before_saving(
    qtbot,
    monkeypatch,
) -> None:
    issue = PdfReviewIssue(
        3,
        "No se reconoció texto legible.",
        "",
        identifier="page-3",
        blocking=True,
        target_marker="<!-- PZDOC PDF PAGE 3 -->",
    )
    report = PdfQualityReport((3,), (3,), (issue,))
    dialog = RevisionReviewDialog(
        None,
        "book.md",
        initial_text="<!-- PZDOC PDF PAGE 3 -->\n\nText kept after review.",
        pdf_report=report,
    )
    qtbot.addWidget(dialog)
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message),
    )

    assert dialog.save_button.isEnabled()
    assert dialog.save_button.text() == "Aplicar todos los cambios"
    dialog.save_button.click()

    assert dialog.result() == dialog.DialogCode.Accepted
    assert warnings == []


def test_pdf_page_markers_are_visually_muted_only_in_the_editable_source(qtbot) -> None:
    dialog = RevisionReviewDialog(
        None,
        "book.md",
        initial_text="<!-- PZDOC PDF PAGE 3 -->\n\nTexto visible.",
    )
    qtbot.addWidget(dialog)
    dialog._pdf_marker_highlighter.rehighlight()

    marker_formats = dialog.editor.document().firstBlock().layout().formats()

    assert marker_formats
    assert marker_formats[0].format.foreground().color().name() == COLORS.text_muted.casefold()
    assert "PZDOC PDF PAGE" not in dialog.preview.toPlainText()


def test_guided_review_shows_only_one_step_and_advances_with_a_clear_decision(qtbot) -> None:
    draft = _draft()
    dialog = RevisionReviewDialog(draft, "book.md")
    qtbot.addWidget(dialog)

    assert not hasattr(dialog, "splitter")
    assert dialog.review_progress_label.text().startswith("Paso 1 de")
    assert dialog.result_tabs.currentIndex() == dialog.editor_tab_index
    assert dialog.original_editor.isReadOnly()
    assert dialog.original_editor.toPlainText() == draft.changes[0].original_markdown
    assert dialog.proposal_editor.toPlainText() == draft.changes[0].proposed_markdown
    assert dialog.help_label.isHidden()
    assert dialog.change_detail.isHidden()
    assert dialog.keep_original_button.isHidden()
    assert dialog.use_proposal_button.isHidden()
    assert dialog.confirm_review_button.isHidden()
    assert dialog.save_button.text() in {"Siguiente", "Aplicar todos los cambios"}

    assert dialog.proposal_choice_button.isChecked()
    dialog.original_choice_button.click()
    assert dialog.original_choice_button.isChecked()
    dialog.save_button.click()

    assert dialog.review_progress_bar.value() == 1
    if dialog.changes_list.count() > 1:
        assert dialog.changes_list.currentRow() == 1
    else:
        assert dialog.save_button.isEnabled()


def test_comparison_can_restore_focus_and_keeps_edits_scoped_to_current_change(qtbot) -> None:
    draft = _draft()
    dialog = RevisionReviewDialog(draft, "book.md")
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.wait(20)

    dialog.proposal_editor.setPlainText("# My title\n\n")
    dialog.locate_original_button.click()
    assert dialog.original_editor.hasFocus()
    assert dialog.original_editor.textCursor().hasSelection()
    dialog.locate_proposal_button.click()
    assert dialog.proposal_editor.hasFocus()
    assert dialog.proposal_editor.toPlainText() == draft.changes[0].proposed_markdown
    dialog.proposal_editor.setPlainText("# My title\n\n")
    dialog.save_button.click()

    assert "# My title" in dialog.editor.toPlainText()


def test_pdf_warning_uses_page_on_left_and_editable_recognition_on_right(
    tmp_path,
    qtbot,
) -> None:
    source = tmp_path / "page.pdf"
    writer = QPdfWriter(str(source))
    painter = QPainter(writer)
    painter.drawText(100, 100, "Graphic page")
    painter.end()
    marker = "<!-- PZDOC PDF PAGE 1 -->"
    issue = PdfReviewIssue(
        1,
        "Comprueba el contenido gráfico.",
        "Recognized text.",
        identifier="page-1",
        target_marker=marker,
    )
    dialog = RevisionReviewDialog(
        None,
        "page.md",
        initial_text=f"{marker}\n\nRecognized text.\n",
        pdf_report=PdfQualityReport((1,), (1,), (issue,)),
        source_path=source,
    )
    qtbot.addWidget(dialog)

    assert dialog.original_stack.currentWidget() is dialog.page_preview
    assert dialog.proposal_editor.toPlainText() == "Recognized text.\n"
    dialog.proposal_editor.setPlainText("Corrected text.\n")
    dialog.save_button.click()

    assert "Corrected text." in dialog.editor.toPlainText()


def test_epub_outline_uses_plain_collapsible_titles_without_technical_prefixes(qtbot) -> None:
    text = "# First\n\n" + "Text. " * 300 + "\n\n## Detail\n\nMore.\n\n### Nested detail\n\nNested."
    dialog = RevisionReviewDialog(None, "book.epub", initial_text=text, epub_title="Book")
    qtbot.addWidget(dialog)

    chapter = dialog.outline.topLevelItem(0)
    assert chapter is not None
    assert not chapter.text(0).startswith("Capítulo")
    assert "Título ·" not in chapter.text(0)
    assert not chapter.isExpanded()
    assert chapter.child(0).text(0) == "Detail"
    assert chapter.child(0).child(0).text(0) == "Nested detail"


def test_epub_structure_editor_applies_chapter_and_hierarchy_actions(
    qtbot,
    monkeypatch,
) -> None:
    text = (
        "# First\n\n"
        + "Long opening. " * 180
        + "\n\n## Detail\n\nDetail body.\n\n### Nested\n\nNested body.\n"
    )
    editor = EpubStructureEditor(text, "Book", ())
    qtbot.addWidget(editor)
    changes: list[bool] = []
    editor.changed.connect(lambda: changes.append(True))

    def choose(_parent, title, _label, **_kwargs):
        return ("Renamed first" if title == "Renombrar" else "Appendix", True)

    monkeypatch.setattr(QInputDialog, "getText", choose)
    editor.tree.setCurrentItem(editor.tree.topLevelItem(0))
    editor.rename_button.click()
    editor.add_button.click()

    first = editor.tree.topLevelItem(0)
    detail = first.child(0)
    editor.tree.setCurrentItem(detail)
    editor.demote_button.click()
    detail = editor.tree.topLevelItem(0).child(0)
    editor.tree.setCurrentItem(detail)
    editor.split_button.click()

    editor.tree.setCurrentItem(editor.tree.topLevelItem(1))
    editor.move_down_button.click()
    editor.tree.setCurrentItem(editor.tree.topLevelItem(2))
    editor.merge_button.click()

    plan = plan_epub(editor.final_markdown(), "Book")
    assert plan.chapters[0].title == "Renamed first"
    assert len(plan.chapters) == 2
    assert len(changes) == 6


def test_epub_structure_inline_actions_remove_only_the_division(qtbot) -> None:
    first_body = "First body. " * 180
    second_body = "Second body. " * 180
    editor = EpubStructureEditor(
        f"# First\n\n{first_body}\n\n# Second\n\n{second_body}",
        "Book",
        (),
    )
    qtbot.addWidget(editor)

    second = editor.tree.topLevelItem(1)
    assert second is not None
    row = editor.tree.itemWidget(second, 0)
    remove = next(
        button
        for button in row.findChildren(QPushButton)
        if button.accessibleName().startswith("Quitar esta división")
    )
    remove.click()

    final = editor.final_markdown()
    assert "First body." in final
    assert "Second body." in final
    assert editor.tree.topLevelItemCount() == 1


def test_structure_action_icons_render_every_supported_direction(qtbot) -> None:
    for action in ("up", "down", "left", "right", "split", "rename", "remove"):
        button = _StructureActionButton(action, action)
        qtbot.addWidget(button)
        button.show()
        assert not button.grab().isNull()
        button.setEnabled(False)
        assert not button.grab().isNull()


def test_unified_review_disables_repeating_exhaustive_pdf_ocr(qtbot) -> None:
    issue = PdfReviewIssue(
        3,
        "El resultado todavía necesita una comparación manual.",
        "Texto dudoso.",
        identifier="page-3",
        target_marker="<!-- PZDOC PDF PAGE 3 -->",
    )
    dialog = RevisionReviewDialog(
        None,
        "book.md",
        initial_text="<!-- PZDOC PDF PAGE 3 -->\n\nTexto dudoso.",
        pdf_report=PdfQualityReport((3,), (3,), (issue,)),
        allow_pdf_retry=False,
    )
    qtbot.addWidget(dialog)

    assert dialog.retry_ocr_button.isVisibleTo(dialog)
    assert not dialog.retry_ocr_button.isEnabled()
    assert "ya se analizó" in dialog.retry_ocr_button.toolTip()


def test_unified_review_reflows_comparison_and_actions_at_320(qtbot) -> None:
    dialog = RevisionReviewDialog(_draft(), "book.md")
    qtbot.addWidget(dialog)
    dialog.resize(320, 720)
    dialog.show()
    qtbot.waitExposed(dialog)

    assert dialog.width() == 320
    assert dialog.comparison_splitter.orientation() is Qt.Orientation.Vertical
    assert dialog.original_choice_button.y() > dialog.original_title.y()
    assert dialog.proposal_choice_button.y() > dialog.proposal_title.y()
    later_position = dialog.actions_layout.getItemPosition(
        dialog.actions_layout.indexOf(dialog.later_button)
    )
    save_position = dialog.actions_layout.getItemPosition(
        dialog.actions_layout.indexOf(dialog.save_button)
    )
    assert later_position[0] > save_position[0]
