from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QBoxLayout, QDialog, QMenu

from parsezen.application.runtime_mapping import request_and_settings_from_job
from parsezen.domain.jobs import (
    AIProfileConfiguration,
    DocumentFormat,
    DocumentJob,
    DocumentSource,
    JobConfiguration,
    OutputConfiguration,
    PageRangeConfiguration,
    ProcessingPlan,
    TranslationConfiguration,
    TranslationMethod,
)
from parsezen.domain.stages import StageKind
from parsezen.glossary import GlossaryEntry
from parsezen.local_models import OllamaStatus
from parsezen.presentation.job_configuration_dialog import (
    JobConfigurationDialog,
    _GlossaryEditorDialog,
    _PageRangeDialog,
)


def _job(
    tmp_path: Path,
    *,
    suffix: str = ".pdf",
    configuration: JobConfiguration | None = None,
) -> DocumentJob:
    source = tmp_path / f"book{suffix}"
    source.write_text("Document", encoding="utf-8")
    return DocumentJob.create(
        DocumentSource.inspect(source),
        configuration or JobConfiguration(),
        order=0,
    )


def test_standard_configuration_uses_visual_format_and_compact_settings(
    qtbot,
    tmp_path: Path,
) -> None:
    dialog = JobConfigurationDialog(_job(tmp_path), embedded=True)
    qtbot.addWidget(dialog)

    assert dialog.output_markdown.isChecked()
    assert not dialog.output_epub.isChecked()
    assert dialog.markdown_card.property("selected") is True
    assert dialog.epub_card.property("selected") is False
    assert dialog.translate_row.value.text() == "No traducir"
    assert dialog.translator_row.isHidden()
    assert dialog.glossary_row.isHidden()
    assert not dialog.plan_reviewed.isChecked()
    assert dialog.pages_row.value.text() == "Todas"
    assert dialog.ocr_row.value.text() == "Automático"
    assert all(
        row.objectName() == "configurationOptionRow"
        for row in (
            dialog.translate_row,
            dialog.translator_row,
            dialog.glossary_row,
            dialog.pages_row,
            dialog.ocr_row,
        )
    )
    assert not hasattr(dialog, "save_button")
    assert not hasattr(dialog, "cancel_button")
    assert not hasattr(dialog, "scroll_area")
    assert not hasattr(dialog, "translation_enabled")


def test_unconfigured_markdown_defaults_to_epub_and_review(qtbot, tmp_path: Path) -> None:
    dialog = JobConfigurationDialog(
        _job(
            tmp_path,
            suffix=".md",
            configuration=JobConfiguration(output=OutputConfiguration(configured=False)),
        ),
        embedded=True,
    )
    qtbot.addWidget(dialog)

    assert dialog.output_epub.isChecked()
    assert dialog.plan_reviewed.isChecked()


def test_missing_ai_is_opened_from_the_relevant_choice(qtbot, tmp_path: Path) -> None:
    dialog = JobConfigurationDialog(
        _job(tmp_path),
        embedded=True,
        ollama_status=OllamaStatus.NOT_INSTALLED,
    )
    qtbot.addWidget(dialog)

    with qtbot.waitSignal(dialog.models_requested):
        dialog._set_review_enabled(True)  # noqa: SLF001

    assert dialog.plan_reviewed.isChecked()
    assert not dialog.validation_label.isHidden()


def test_translation_choice_reveals_only_translator_and_glossary(
    qtbot,
    tmp_path: Path,
) -> None:
    dialog = JobConfigurationDialog(_job(tmp_path), embedded=True, ollama_status=OllamaStatus.READY)
    qtbot.addWidget(dialog)

    dialog._set_translation_language("es")  # noqa: SLF001

    assert dialog.translate_row.value.text() == "Español"
    assert not dialog.translator_row.isHidden()
    assert dialog.translator_row.value.text() == "Argos · ligero"
    assert not dialog.glossary_row.isHidden()
    assert dialog.glossary_row.value.text() == "Ninguno"
    assert not hasattr(dialog, "translation_route")
    assert "verifica la traducción" in dialog.plan_reviewed.accessibleDescription()
    assert dialog.plan_reviewed.toolTip() == dialog.plan_reviewed.accessibleDescription()

    dialog._set_review_enabled(True)  # noqa: SLF001

    assert "verifica la traducción" in dialog.plan_reviewed.accessibleDescription()


def test_translation_menu_contains_no_translation_and_every_supported_language(
    qtbot,
    tmp_path: Path,
) -> None:
    dialog = JobConfigurationDialog(_job(tmp_path), embedded=True)
    qtbot.addWidget(dialog)
    labels = [label for label, _value in dialog._translation_choices()]  # noqa: SLF001

    assert labels[0] == "No traducir"
    assert "Español" in labels
    assert "Inglés" in labels


def test_page_interval_dialog_returns_one_inclusive_range(qtbot) -> None:
    selector = _PageRangeDialog(PageRangeConfiguration(25, 140))
    qtbot.addWidget(selector)

    assert selector.first_page.value() == 25
    assert selector.last_page.value() == 140
    assert selector.page_range() == PageRangeConfiguration(25, 140)


def test_glossary_editor_adds_validates_removes_and_accepts_terms(qtbot) -> None:
    editor = _GlossaryEditorDialog((GlossaryEntry("one", "uno"), GlossaryEntry("two", "dos")))
    qtbot.addWidget(editor)

    remove = editor.table.cellWidget(0, 2)
    assert remove is not None
    qtbot.mouseClick(remove, Qt.MouseButton.LeftButton)
    assert editor.entries() == (GlossaryEntry("two", "dos"),)

    qtbot.mouseClick(editor.add_button, Qt.MouseButton.LeftButton)
    row = editor.table.rowCount() - 1
    editor.table.item(row, 0).setText("term")
    editor._submit()  # noqa: SLF001
    assert not editor.validation_label.isHidden()

    editor.table.item(row, 1).setText("término")
    editor._submit()  # noqa: SLF001
    assert editor.result() == QDialog.DialogCode.Accepted


def test_configuration_cards_rows_focus_and_output_slots_are_operable(
    qtbot, tmp_path: Path
) -> None:
    dialog = JobConfigurationDialog(_job(tmp_path), embedded=False)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)
    dialog.translate_row.activated.disconnect(dialog._open_translation_menu)  # noqa: SLF001

    qtbot.mouseClick(dialog.epub_card, Qt.MouseButton.LeftButton)
    assert dialog.output_epub.isChecked()
    qtbot.mouseClick(dialog.translate_row, Qt.MouseButton.LeftButton, pos=QPoint(2, 2))
    qtbot.waitUntil(dialog.translate_row.hasFocus)
    qtbot.mouseClick(dialog.translate_row, Qt.MouseButton.RightButton, pos=QPoint(2, 2))
    qtbot.mouseClick(dialog.markdown_card, Qt.MouseButton.RightButton, pos=QPoint(2, 2))

    dialog._focus_stage(None)  # noqa: SLF001
    dialog._focus_stage(StageKind.TRANSLATE)  # noqa: SLF001
    assert dialog.translate_row.hasFocus()
    dialog._set_markdown_output_when_checked(False)  # noqa: SLF001
    dialog._set_epub_output_when_checked(False)  # noqa: SLF001


@pytest.mark.parametrize(
    ("output", "expects_structure"),
    ((DocumentFormat.MARKDOWN, False), (DocumentFormat.EPUB, True)),
)
def test_reviewed_plan_maps_to_the_fixed_runtime_phases(
    qtbot,
    tmp_path: Path,
    output: DocumentFormat,
    expects_structure: bool,
) -> None:
    dialog = JobConfigurationDialog(
        _job(tmp_path),
        embedded=True,
        default_ai_model="qwen3:4b",
        default_ai_context=8192,
        ollama_status=OllamaStatus.READY,
    )
    qtbot.addWidget(dialog)
    dialog._set_output_format(output)  # noqa: SLF001
    dialog._set_review_enabled(True)  # noqa: SLF001

    configured = dialog.configuration()
    job = dialog._job.with_configuration(configured)  # noqa: SLF001
    request, settings = request_and_settings_from_job(job)

    assert configured.plan is ProcessingPlan.LOCAL_AI_REVIEWED
    assert request.review_content
    assert request.review_structure is expects_structure
    assert settings.model == "qwen3:4b"
    assert settings.context_window == 8192


def test_translation_defaults_to_argos_and_glossary_is_optional(qtbot, tmp_path: Path) -> None:
    dialog = JobConfigurationDialog(_job(tmp_path), embedded=True)
    qtbot.addWidget(dialog)
    dialog._set_translation_language("es")  # noqa: SLF001
    dialog._append_glossary_entry(GlossaryEntry("term", "término"))  # noqa: SLF001

    configured = dialog.configuration()
    job = dialog._job.with_configuration(configured)  # noqa: SLF001
    request, _settings = request_and_settings_from_job(job)

    assert configured.translation.glossary == (("term", "término"),)
    assert configured.translation.method is TranslationMethod.OFFLINE
    assert tuple((item.source, item.target) for item in request.glossary) == (("term", "término"),)
    assert dialog.glossary_row.value.text() == "1 término"


def test_translation_can_use_the_global_local_ai_model(qtbot, tmp_path: Path) -> None:
    dialog = JobConfigurationDialog(
        _job(tmp_path),
        embedded=True,
        ollama_status=OllamaStatus.READY,
    )
    qtbot.addWidget(dialog)
    dialog.set_default_ai_profile("translategemma:4b", 8192)
    dialog._set_translation_language("es")  # noqa: SLF001
    dialog._set_translation_method(TranslationMethod.LOCAL_AI)  # noqa: SLF001

    configured = dialog.configuration()
    job = dialog._job.with_configuration(configured)  # noqa: SLF001
    request, _settings = request_and_settings_from_job(job)

    assert configured.translation == TranslationConfiguration(
        enabled=True,
        method=TranslationMethod.LOCAL_AI,
        target_language="es",
    )
    assert dialog.translator_row.value.text() == "IA local · contextual"
    assert "corrección se integra" in dialog.plan_reviewed.accessibleDescription()
    assert request.target_language == "es"
    assert request.offline_translation_language is None
    assert request.improvement_mode is not None

    dialog._set_review_enabled(True)  # noqa: SLF001

    assert "corrección se integra" in dialog.plan_reviewed.accessibleDescription()


def test_ai_translation_requires_the_global_model(qtbot, tmp_path: Path) -> None:
    dialog = JobConfigurationDialog(_job(tmp_path), embedded=True)
    qtbot.addWidget(dialog)
    dialog._set_translation_language("es")  # noqa: SLF001
    dialog._set_translation_method(TranslationMethod.LOCAL_AI)  # noqa: SLF001

    with pytest.raises(ValueError, match="modelo de IA local general"):
        dialog.configuration()


def test_turning_translation_off_discards_hidden_engine_and_glossary(qtbot, tmp_path: Path) -> None:
    dialog = JobConfigurationDialog(_job(tmp_path), embedded=True)
    qtbot.addWidget(dialog)
    dialog._set_translation_language("es")  # noqa: SLF001
    dialog._set_translation_method(TranslationMethod.LOCAL_AI)  # noqa: SLF001
    dialog._append_glossary_entry(GlossaryEntry("term", "término"))  # noqa: SLF001

    dialog._set_translation_language(None)  # noqa: SLF001

    assert dialog.configuration().translation == TranslationConfiguration()
    assert dialog.translator_row.isHidden()
    assert dialog.glossary_row.isHidden()


def test_global_destination_and_ai_are_inherited_but_not_overridable(qtbot, tmp_path: Path) -> None:
    destination = tmp_path / "results"
    dialog = JobConfigurationDialog(
        _job(tmp_path),
        embedded=True,
        default_output_directory=destination,
        default_ai_model="qwen3:4b",
        default_ai_context=4096,
        ollama_status=OllamaStatus.READY,
    )
    qtbot.addWidget(dialog)
    dialog._set_review_enabled(True)  # noqa: SLF001

    configured = dialog.configuration()

    assert configured.output.directory == destination
    assert configured.ai == AIProfileConfiguration(model="qwen3:4b", context_window=4096)
    assert not hasattr(dialog, "destination_summary")
    assert not hasattr(dialog, "ai_model")


def test_pdf_interval_and_ocr_are_values_not_permanent_controls(qtbot, tmp_path: Path) -> None:
    dialog = JobConfigurationDialog(_job(tmp_path), embedded=True)
    qtbot.addWidget(dialog)
    dialog._set_page_range(PageRangeConfiguration(25, 140))  # noqa: SLF001
    dialog._set_force_pdf_ocr(True)  # noqa: SLF001

    configured = dialog.configuration()

    assert configured.page_range == PageRangeConfiguration(25, 140)
    assert configured.force_pdf_ocr
    assert dialog.pages_row.value.text() == "25–140"
    assert dialog.ocr_row.value.text() == "Todas las páginas"
    assert not hasattr(dialog, "page_first")
    assert not hasattr(dialog, "page_last")
    assert not hasattr(dialog, "page_interval")


def test_non_pdf_hides_pdf_specific_rows_and_ignores_values(qtbot, tmp_path: Path) -> None:
    dialog = JobConfigurationDialog(_job(tmp_path, suffix=".txt"), embedded=True)
    qtbot.addWidget(dialog)
    dialog._set_page_range(PageRangeConfiguration(3, 8))  # noqa: SLF001
    dialog._set_force_pdf_ocr(True)  # noqa: SLF001

    configured = dialog.configuration()

    assert dialog.pages_row.isHidden()
    assert dialog.ocr_row.isHidden()
    assert configured.page_range is None
    assert not configured.force_pdf_ocr


def test_reviewed_plan_surfaces_missing_global_ai(qtbot, tmp_path: Path) -> None:
    dialog = JobConfigurationDialog(_job(tmp_path), embedded=True)
    qtbot.addWidget(dialog)
    dialog._set_review_enabled(True)  # noqa: SLF001

    with pytest.raises(ValueError, match="modelo de IA local general"):
        dialog.configuration()


def test_editor_has_no_scroll_or_footer_at_compact_width(qtbot, tmp_path: Path) -> None:
    dialog = JobConfigurationDialog(_job(tmp_path), embedded=False)
    qtbot.addWidget(dialog)
    dialog.resize(320, 520)
    dialog.show()
    qtbot.wait(10)

    assert dialog.width() == 320
    assert dialog.output_choices.direction() is QBoxLayout.Direction.TopToBottom
    assert dialog.content.width() <= dialog.contentsRect().width()
    assert dialog.content.sizeHint().height() <= dialog.height()
    assert not hasattr(dialog, "save_button")
    assert not hasattr(dialog, "cancel_button")
    assert dialog.grab().save(str(tmp_path / "configuration-compact.png"))


def test_escape_closes_without_discard_confirmation(qtbot, tmp_path: Path) -> None:
    dialog = JobConfigurationDialog(_job(tmp_path), embedded=False)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog._set_output_format(DocumentFormat.EPUB)  # noqa: SLF001

    qtbot.keyClick(dialog, Qt.Key.Key_Escape)

    assert not dialog.isVisible()


def test_option_row_is_keyboard_operable(qtbot, tmp_path: Path) -> None:
    dialog = JobConfigurationDialog(_job(tmp_path), embedded=False)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)
    dialog.translate_row.activated.disconnect(dialog._open_translation_menu)  # noqa: SLF001
    dialog.translate_row.setFocus()
    qtbot.waitUntil(dialog.translate_row.hasFocus)

    with qtbot.waitSignal(dialog.translate_row.activated):
        qtbot.keyClick(dialog.translate_row, Qt.Key.Key_Space)

    assert dialog.translate_row.hasFocus()


def test_choice_menu_is_anchored_to_the_value_side(qtbot, tmp_path: Path) -> None:
    dialog = JobConfigurationDialog(_job(tmp_path), embedded=False)
    qtbot.addWidget(dialog)
    dialog.resize(720, 560)
    dialog.show()
    qtbot.waitExposed(dialog)
    menu = QMenu(dialog)
    for label, _value in dialog._translation_choices():  # noqa: SLF001
        menu.addAction(label)

    anchor = dialog._menu_anchor(menu, dialog.translate_row)  # noqa: SLF001
    row_bottom_right = dialog.translate_row.mapToGlobal(
        QPoint(dialog.translate_row.width(), dialog.translate_row.height())
    )

    assert anchor.x() + menu.sizeHint().width() == row_bottom_right.x()
    assert anchor.y() == row_bottom_right.y()
