from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, QSize
from PySide6.QtWidgets import QApplication

import parsezen.presentation.job_configuration_dialog as dialog_module
from parsezen.domain.jobs import (
    AIProfileConfiguration,
    CoverStrategy,
    DocumentFormat,
    DocumentJob,
    DocumentSource,
    JobConfiguration,
    OutputConfiguration,
    TranslationMethod,
)
from parsezen.domain.stages import StageKind
from parsezen.presentation.job_configuration_dialog import JobConfigurationDialog


def make_job(
    *,
    source_format: DocumentFormat = DocumentFormat.PDF,
    output_format: DocumentFormat = DocumentFormat.MARKDOWN,
) -> DocumentJob:
    return DocumentJob.create(
        DocumentSource(Path(f"book.{source_format.value}"), source_format, 100, 1),
        JobConfiguration(output=OutputConfiguration(format=output_format)),
        order=0,
    )


def test_dialog_builds_one_complete_independent_configuration(qtbot) -> None:
    dialog = JobConfigurationDialog(
        make_job(),
        models=(("qwen3:4b", "Qwen3 4B"),),
    )
    qtbot.addWidget(dialog)
    dialog.translation_enabled.setChecked(True)
    dialog.translation_method.setCurrentIndex(1)
    dialog.target_language.setCurrentText("Español")
    dialog.translation_model.setCurrentIndex(1)
    dialog.refinement_enabled.setChecked(True)
    dialog.output_format.setCurrentIndex(dialog.output_format.findData(DocumentFormat.EPUB))
    dialog.structure_enabled.setChecked(True)
    configuration = dialog.configuration()

    assert configuration.translation.enabled
    assert configuration.translation.method is TranslationMethod.LOCAL_AI
    assert configuration.ai.model == "qwen3:4b"
    assert configuration.translation.model is None
    assert configuration.refinement.enabled
    assert configuration.refinement.model is None
    assert configuration.structure.enabled
    assert configuration.output.format is DocumentFormat.EPUB
    assert configuration.output.cover_strategy is CoverStrategy.NONE
    assert dialog.book_title.isHidden()
    assert dialog.book_author.isHidden()
    assert dialog.cover_strategy.isHidden()


def test_non_epub_output_disables_optional_chapter_organization(qtbot) -> None:
    dialog = JobConfigurationDialog(make_job())
    qtbot.addWidget(dialog)
    dialog.structure_enabled.setChecked(True)

    assert not dialog.structure_enabled.isEnabled()
    assert not dialog.structure_enabled.isChecked()


def test_manual_review_is_a_safe_fixed_step(qtbot) -> None:
    dialog = JobConfigurationDialog(
        make_job(),
        models=(("qwen3:4b", "Qwen3 4B"),),
    )
    qtbot.addWidget(dialog)
    dialog.translation_enabled.setChecked(True)
    dialog.refinement_enabled.setChecked(True)
    dialog.refinement_model.setCurrentIndex(1)
    dialog.output_format.setCurrentIndex(dialog.output_format.findData(DocumentFormat.EPUB))
    dialog.structure_enabled.setChecked(True)

    configuration = dialog.configuration()

    assert configuration.translation.manual_review
    assert configuration.refinement.manual_review
    assert configuration.structure.manual_review
    assert dialog.translation_review.isHidden()
    assert dialog.refinement_review.isHidden()
    assert dialog.structure_review.isHidden()


def test_direct_docx_and_epub_outputs_offer_style_preservation(qtbot) -> None:
    pdf_dialog = JobConfigurationDialog(make_job())
    qtbot.addWidget(pdf_dialog)
    pdf_dialog.output_format.setCurrentIndex(pdf_dialog.output_format.findData(DocumentFormat.EPUB))

    epub_dialog = JobConfigurationDialog(
        make_job(
            source_format=DocumentFormat.EPUB,
            output_format=DocumentFormat.EPUB,
        )
    )
    docx_dialog = JobConfigurationDialog(
        make_job(
            source_format=DocumentFormat.DOCX,
            output_format=DocumentFormat.DOCX,
        )
    )
    qtbot.addWidget(epub_dialog)
    qtbot.addWidget(docx_dialog)

    assert pdf_dialog.preserve_styles_row.isHidden()
    assert not epub_dialog.preserve_styles_row.isHidden()
    assert not docx_dialog.preserve_styles_row.isHidden()
    assert epub_dialog.preserve_styles.objectName() == "processSwitch"
    assert epub_dialog.include_images.objectName() == "processSwitch"
    assert epub_dialog.preserve_styles.size() == QSize(48, 32)
    assert epub_dialog.include_images.size() == QSize(48, 32)


def test_epub_and_docx_embed_images_without_a_resource_folder(qtbot) -> None:
    for output_format in (DocumentFormat.EPUB, DocumentFormat.DOCX):
        dialog = JobConfigurationDialog(
            make_job(
                source_format=(
                    DocumentFormat.PDF
                    if output_format is DocumentFormat.EPUB
                    else DocumentFormat.DOCX
                ),
                output_format=output_format,
            )
        )
        qtbot.addWidget(dialog)
        dialog.output_format.setCurrentIndex(dialog.output_format.findData(output_format))
        dialog.image_directory.setText("stale-folder")

        if output_format is DocumentFormat.DOCX:
            dialog.translation_enabled.setChecked(True)
        configuration = dialog.configuration()

        assert not dialog.include_images.isHidden()
        assert dialog.image_buttons.isHidden()
        assert configuration.output.image_directory is None


def test_markdown_images_can_use_a_stable_external_folder(qtbot, tmp_path: Path) -> None:
    dialog = JobConfigurationDialog(make_job())
    qtbot.addWidget(dialog)
    dialog.image_directory.setText(str(tmp_path / "Obsidian assets"))

    configuration = dialog.configuration()

    assert not dialog.image_buttons.isHidden()
    assert configuration.output.image_directory == tmp_path / "Obsidian assets"


def test_global_destination_is_inherited_until_a_document_override_is_enabled(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    inherited = tmp_path / "Resultados"
    custom = tmp_path / "Especial"
    job = make_job()
    dialog = JobConfigurationDialog(job, default_output_directory=inherited)
    qtbot.addWidget(dialog)

    assert not dialog.custom_output_enabled.isChecked()
    assert not dialog.output_destination_button.isHidden()
    assert dialog.output_destination_button.text() == str(inherited)
    assert dialog.configuration().output.directory == inherited

    monkeypatch.setattr(
        dialog_module.QFileDialog,
        "getExistingDirectory",
        lambda *_args, **_kwargs: str(custom),
    )
    dialog.output_destination_button.click()

    assert dialog.configuration().output.directory == custom
    assert dialog.output_destination_button.text() == str(custom)

    dialog.custom_output_enabled.setChecked(False)

    assert dialog.configuration().output.directory == inherited


def test_same_format_requires_a_useful_transformation(qtbot) -> None:
    dialog = JobConfigurationDialog(
        make_job(
            source_format=DocumentFormat.MARKDOWN,
            output_format=DocumentFormat.MARKDOWN,
        )
    )
    qtbot.addWidget(dialog)

    with pytest.raises(ValueError, match="Activa al menos una mejora"):
        dialog.configuration()
    assert not dialog.validation_label.isHidden()

    dialog.translation_enabled.setChecked(True)

    assert dialog.configuration().translation.enabled
    assert dialog.validation_label.isHidden()


def test_embedded_editor_autosaves_with_back_without_duplicate_footer(qtbot) -> None:
    editor = JobConfigurationDialog(
        make_job(),
        embedded=True,
        compatible_job_count=2,
    )
    qtbot.addWidget(editor)
    actions: list[str] = []
    editor.save_requested.connect(lambda: actions.append("save"))
    editor.cancel_requested.connect(lambda: actions.append("cancel"))

    editor.translation_enabled.setChecked(True)
    closed_directly = editor.request_close()

    assert not editor.isModal()
    assert editor.heading.isHidden()
    assert [editor.tabs.tabText(index) for index in range(editor.tabs.count())] == [
        "Resultado",
        "Traducir",
        "Corregir",
        "Personalizar",
        "IA local",
    ]
    assert not editor.tabs.isTabVisible(editor.personalization_tab_index)
    assert not editor.tabs.isTabVisible(editor.ai_tab_index)
    assert editor.ai_navigation.isHidden()
    assert not editor.apply_compatible_row.isHidden()
    assert (
        editor.apply_compatible_row.findChild(dialog_module.QLabel).text()
        == "Aplicar misma configuración al resto de documentos"
    )
    assert editor.apply_compatible_separator.isVisible() or not editor.isVisible()
    assert closed_directly is False
    assert actions == ["save"]


def test_embedded_glossary_is_edited_inline(qtbot) -> None:
    editor = JobConfigurationDialog(make_job(), embedded=True)
    qtbot.addWidget(editor)

    editor._edit_glossary()

    assert editor.glossary_table.rowCount() == 1
    assert editor.glossary_editor.isVisible() or not editor.isVisible()
    assert editor.glossary_add_button.text() == "Añadir término"
    assert editor.glossary_table.cellWidget(0, 2) is not None
    assert editor.glossary_table.horizontalHeaderItem(1).text() == "Sustituir por"
    assert editor.glossary_editor.metaObject().className() == "QWidget"
    assert editor.glossary_table.maximumHeight() == 174
    assert editor.target_language.height() == 34


def test_disabled_translation_ignores_incomplete_glossary_and_disables_its_options(
    qtbot,
) -> None:
    editor = JobConfigurationDialog(make_job(), embedded=True)
    qtbot.addWidget(editor)
    editor._add_glossary_row()
    editor.glossary_table.item(0, 0).setText("Parsezen")

    configuration = editor.configuration()

    assert not configuration.translation.enabled
    assert configuration.translation.glossary == ()
    assert not editor.translation_method_selector.isEnabled()
    assert not editor.target_language.isEnabled()
    assert not editor.glossary_editor.isEnabled()

    editor.translation_enabled.setChecked(True)
    with pytest.raises(ValueError, match="glosario"):
        editor.configuration()


def test_translation_method_is_one_binary_segmented_selector(qtbot) -> None:
    editor = JobConfigurationDialog(make_job(), embedded=True)
    qtbot.addWidget(editor)
    editor.translation_enabled.setChecked(True)

    assert editor.translation_method_selector.objectName() == "translationMethodSelector"
    assert editor.method_group.exclusive()
    assert editor.offline_method_button.isChecked()
    editor.ai_method_button.click()
    assert editor.ai_method_button.isChecked()
    assert not editor.offline_method_button.isChecked()


def test_back_explains_why_an_invalid_draft_cannot_be_saved(
    qtbot,
) -> None:
    editor = JobConfigurationDialog(make_job(), embedded=True)
    qtbot.addWidget(editor)
    editor.show()
    qtbot.waitExposed(editor)
    editor.custom_output_enabled.setChecked(True)

    closed_directly = editor.request_close()

    assert not closed_directly
    assert not editor.validation_label.isHidden()
    assert "carpeta" in editor.validation_label.text().casefold()
    qtbot.waitUntil(editor.output_destination_button.hasFocus)


def test_first_page_cover_is_only_offered_for_pdf_sources(qtbot) -> None:
    epub_job = make_job(source_format=DocumentFormat.EPUB)
    editor = JobConfigurationDialog(epub_job, embedded=True)
    qtbot.addWidget(editor)

    assert editor.cover_strategy.findData(CoverStrategy.FIRST_PAGE) == -1


def test_epub_cover_and_metadata_are_deferred_to_final_personalization(qtbot) -> None:
    editor = JobConfigurationDialog(
        make_job(output_format=DocumentFormat.EPUB),
        embedded=True,
    )
    qtbot.addWidget(editor)
    editor.include_images.setChecked(False)
    configuration = editor.configuration()

    assert not configuration.output.include_images
    assert configuration.output.title is None
    assert configuration.output.author is None
    assert configuration.output.cover_strategy is CoverStrategy.NONE
    assert configuration.output.cover_path is None
    assert editor.book_title.isHidden()
    assert editor.book_author.isHidden()
    assert editor.cover_strategy.isHidden()


def test_personalization_configuration_only_exposes_preorganization(qtbot) -> None:
    editor = JobConfigurationDialog(
        make_job(output_format=DocumentFormat.EPUB),
        embedded=True,
    )
    qtbot.addWidget(editor)
    assert editor.tabs.isTabVisible(editor.personalization_tab_index)
    assert not editor.structure_enabled.isHidden()
    assert editor.structure_summary.text() == "Pre-organizar capítulos y jerarquías con IA"
    assert editor.book_title.isHidden()
    assert editor.book_author.isHidden()
    assert editor.cover_strategy.isHidden()


def test_epub_personalization_loads_and_restores_original_book_information(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "book.epub"
    source.write_bytes(b"inspected through a test double")
    monkeypatch.setattr(
        dialog_module,
        "inspect_epub_package",
        lambda _path: SimpleNamespace(
            title="Título original",
            authors=("Autora original",),
        ),
    )
    job = DocumentJob.create(
        DocumentSource(source, DocumentFormat.EPUB, source.stat().st_size, 1),
        JobConfiguration(
            output=OutputConfiguration(
                configured=False,
                format=DocumentFormat.EPUB,
                title=source.stem,
            )
        ),
        order=0,
    )
    editor = JobConfigurationDialog(job, embedded=True)
    qtbot.addWidget(editor)

    assert editor.book_title.text() == "Título original"
    assert editor.book_author.text() == "Autora original"

    editor.book_title.setText("Otro título")
    editor.book_author.clear()
    editor.restore_book_information.click()

    assert editor.book_title.text() == "Título original"
    assert editor.book_author.text() == "Autora original"


def test_empty_model_selector_opens_manager_and_accepts_discovered_models(qtbot) -> None:
    editor = JobConfigurationDialog(make_job(), embedded=True)
    qtbot.addWidget(editor)
    requested: list[bool] = []
    editor.models_requested.connect(lambda: requested.append(True))

    editor.refinement_model.activated.emit(0)
    editor.set_models((("qwen3:4b", "Qwen3 4B"),))

    assert requested == [True]
    assert editor.refinement_model.findData("qwen3:4b") >= 0
    assert editor.refinement_model.findText("Gestionar modelos de IA…") >= 0
    assert editor.select_model("qwen3:4b")
    assert editor.refinement_model.currentData() == "qwen3:4b"


def test_requested_phase_opens_its_exact_configuration_tab(qtbot) -> None:
    editor = JobConfigurationDialog(make_job(), stage=StageKind.TRANSLATE)
    qtbot.addWidget(editor)

    assert editor.tabs.currentIndex() == editor.translation_tab_index


def test_compact_configuration_uses_selectable_navigation_and_wrapped_forms(qtbot) -> None:
    editor = JobConfigurationDialog(make_job(), embedded=True)
    qtbot.addWidget(editor)
    editor.resize(300, 620)
    editor.set_compact_mode(True)
    editor.show()
    qtbot.waitExposed(editor)

    assert editor.navigation_panel.isVisible()
    assert editor.detail_panel.isHidden()
    assert editor.tabs.tabBar().isHidden()
    editor.translation_navigation.button.click()
    assert editor.tabs.currentIndex() == editor.translation_tab_index
    assert editor.navigation_panel.isHidden()
    assert editor.detail_panel.isVisible()
    assert editor.compact_detail_back.isVisible()
    editor.translation_enabled.setChecked(True)
    assert editor.translation_enabled.isChecked()

    editor.compact_detail_back.click()

    assert editor.navigation_panel.isVisible()
    assert editor.detail_panel.isHidden()


def test_navigation_row_uses_background_without_selection_outlines(qtbot) -> None:
    editor = JobConfigurationDialog(make_job(), embedded=True)
    qtbot.addWidget(editor)
    row = editor.refinement_navigation
    assert row.activation is editor.refinement_enabled

    QApplication.sendEvent(row.activation, QEvent(QEvent.Type.Enter))
    row.set_selected(True)

    assert row.property("hovered") is True
    assert row.property("selected") is True
    assert not row.button.isCheckable()
    assert not row.button.isChecked()
    assert "border-left" not in editor.styleSheet()


def test_pdf_page_range_uses_the_shared_switch_and_keeps_its_fields(qtbot) -> None:
    editor = JobConfigurationDialog(make_job(), embedded=True)
    qtbot.addWidget(editor)

    assert isinstance(editor.page_range_enabled, dialog_module.Switch)
    assert editor.page_range_enabled.accessibleName() == "Procesar solo un intervalo"
    assert (
        editor.page_range_enabled_row.findChild(dialog_module.QLabel).text()
        == "Procesar solo un intervalo"
    )

    editor.page_range_enabled.setChecked(True)

    assert not editor.page_range_row.isHidden()


def test_ai_profile_can_inherit_or_override_the_application_default(qtbot) -> None:
    editor = JobConfigurationDialog(
        make_job(),
        embedded=True,
        models=(
            ("qwen3:4b-instruct", "Qwen3 4B Instruct"),
            ("gemma3:4b", "Gemma 3 4B"),
        ),
        default_ai_model="qwen3:4b-instruct",
        default_ai_context=8192,
    )
    qtbot.addWidget(editor)
    editor.refinement_enabled.setChecked(True)

    inherited = editor.configuration()

    assert not inherited.ai.is_custom
    assert inherited.ai.model == "qwen3:4b-instruct"
    assert editor.ai_editor.isHidden()
    assert editor.ai_status_label.text().startswith("Estado:")

    editor.ai_use_default.setChecked(False)
    assert not editor.ai_editor.isHidden()
    editor.ai_model.setCurrentIndex(editor.ai_model.findData("gemma3:4b"))
    editor.ai_context.setValue(4096)
    custom = editor.configuration()

    assert custom.ai.is_custom
    assert custom.ai.model == "gemma3:4b"
    assert custom.ai.context_window == 4096


def test_inherited_profile_uses_the_current_default_instead_of_its_saved_snapshot(
    qtbot,
) -> None:
    job = make_job()
    job = job.with_configuration(
        replace(
            job.configuration,
            ai=AIProfileConfiguration(
                model="old-model",
                context_window=4096,
                is_custom=False,
            ),
        )
    )
    editor = JobConfigurationDialog(
        job,
        models=(("old-model", "Modelo anterior"), ("new-model", "Modelo nuevo")),
        default_ai_model="new-model",
        default_ai_context=8192,
    )
    qtbot.addWidget(editor)

    configuration = editor.configuration()

    assert editor.ai_model.currentData() == "new-model"
    assert configuration.ai.model == "new-model"
    assert configuration.ai.context_window == 8192
    assert not configuration.ai.is_custom


def test_batch_scope_is_an_explicit_change_that_saves_on_back(qtbot) -> None:
    editor = JobConfigurationDialog(
        make_job(),
        embedded=True,
        compatible_job_count=3,
    )
    qtbot.addWidget(editor)
    saved: list[bool] = []
    editor.save_requested.connect(lambda: saved.append(True))

    editor.apply_compatible.setChecked(True)

    assert not editor.request_close()
    assert saved == [True]
