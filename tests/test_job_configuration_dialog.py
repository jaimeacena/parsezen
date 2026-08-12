from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from parsezen.application.runtime_mapping import request_and_settings_from_job
from parsezen.domain.jobs import (
    AIProfileConfiguration,
    DocumentFormat,
    DocumentJob,
    DocumentSource,
    JobConfiguration,
    OutputConfiguration,
    ProcessingPlan,
    TranslationConfiguration,
    TranslationMethod,
)
from parsezen.glossary import GlossaryEntry
from parsezen.presentation.job_configuration_dialog import JobConfigurationDialog


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


def test_editor_defaults_to_direct_processing_and_explains_the_route(
    qtbot,
    tmp_path: Path,
) -> None:
    dialog = JobConfigurationDialog(_job(tmp_path), embedded=True)
    qtbot.addWidget(dialog)

    assert not dialog.plan_reviewed.isChecked()
    assert dialog.output_markdown.isChecked()
    assert dialog.translation_target.currentData() is None
    assert not dialog.advanced_panel.isVisible()
    assert not dialog._dirty  # noqa: SLF001
    assert not dialog.ai_summary.isVisible()
    assert "Markdown" in dialog.markdown_card.radio.accessibleName()
    assert "solo los bloques afectados" in dialog.summary_detail.text()
    dialog.advanced_toggle.setChecked(True)
    assert not dialog._dirty  # noqa: SLF001
    assert "PDF → Markdown" in dialog.route_summary.text()
    assert "Sin pasadas de IA" in dialog.route_summary.text()


def test_unconfigured_markdown_source_defaults_to_a_useful_epub_result(
    qtbot,
    tmp_path: Path,
) -> None:
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


def test_editor_explains_argos_review_passes_without_calling_them_one_engine(
    qtbot,
    tmp_path: Path,
) -> None:
    dialog = JobConfigurationDialog(
        _job(tmp_path),
        embedded=True,
        default_ai_model="qwen3:4b-instruct",
    )
    qtbot.addWidget(dialog)
    dialog.translation_target.setCurrentIndex(dialog.translation_target.findData("es"))
    dialog.plan_reviewed.setChecked(True)

    assert "Argos" in dialog.route_summary.text()
    assert "revisión bilingüe" in dialog.route_summary.text().casefold()
    assert "2 pasadas" in dialog.route_summary.text()


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
    )
    qtbot.addWidget(dialog)
    (
        dialog.output_markdown if output is DocumentFormat.MARKDOWN else dialog.output_epub
    ).setChecked(True)
    dialog.plan_reviewed.setChecked(True)

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
    dialog.translation_target.setCurrentIndex(dialog.translation_target.findData("es"))
    dialog._append_glossary_entry(GlossaryEntry("term", "término"))  # noqa: SLF001

    configured = dialog.configuration()
    job = dialog._job.with_configuration(configured)  # noqa: SLF001
    request, _settings = request_and_settings_from_job(job)

    assert configured.translation.glossary == (("term", "término"),)
    assert configured.translation.method is TranslationMethod.OFFLINE
    assert tuple((item.source, item.target) for item in request.glossary) == (("term", "término"),)


def test_translation_can_use_the_global_local_ai_model(qtbot, tmp_path: Path) -> None:
    dialog = JobConfigurationDialog(_job(tmp_path), embedded=True)
    qtbot.addWidget(dialog)
    dialog.set_default_ai_profile("translategemma:4b", 8192)
    dialog.translation_target.setCurrentIndex(dialog.translation_target.findData("es"))
    dialog.translation_method.setCurrentIndex(
        dialog.translation_method.findData(TranslationMethod.LOCAL_AI)
    )

    configured = dialog.configuration()
    job = dialog._job.with_configuration(configured)  # noqa: SLF001
    request, _settings = request_and_settings_from_job(job)

    assert configured.translation == TranslationConfiguration(
        enabled=True,
        method=TranslationMethod.LOCAL_AI,
        target_language="es",
    )
    assert request.target_language == "es"
    assert request.offline_translation_language is None
    assert request.improvement_mode is not None
    assert not hasattr(dialog, "ai_model")


def test_ai_translation_requires_the_global_model(qtbot, tmp_path: Path) -> None:
    dialog = JobConfigurationDialog(_job(tmp_path), embedded=True)
    qtbot.addWidget(dialog)
    dialog.translation_target.setCurrentIndex(dialog.translation_target.findData("es"))
    dialog.translation_method.setCurrentIndex(
        dialog.translation_method.findData(TranslationMethod.LOCAL_AI)
    )

    with pytest.raises(ValueError, match="modelo de IA local general"):
        dialog.configuration()


def test_turning_translation_off_discards_hidden_engine_and_glossary(qtbot, tmp_path: Path) -> None:
    dialog = JobConfigurationDialog(_job(tmp_path), embedded=True)
    qtbot.addWidget(dialog)
    dialog.translation_target.setCurrentIndex(dialog.translation_target.findData("es"))
    dialog.translation_method.setCurrentIndex(
        dialog.translation_method.findData(TranslationMethod.LOCAL_AI)
    )
    dialog._append_glossary_entry(GlossaryEntry("term", "término"))  # noqa: SLF001

    dialog.translation_target.setCurrentIndex(dialog.translation_target.findData(None))
    configured = dialog.configuration()

    assert configured.translation == TranslationConfiguration()


def test_global_destination_and_ai_are_visible_but_not_overridable(qtbot, tmp_path: Path) -> None:
    destination = tmp_path / "results"
    dialog = JobConfigurationDialog(
        _job(tmp_path),
        embedded=True,
        default_output_directory=destination,
        default_ai_model="qwen3:4b",
        default_ai_context=4096,
        models=(("qwen3:4b", "Qwen 4B"),),
    )
    qtbot.addWidget(dialog)
    dialog.plan_reviewed.setChecked(True)

    configured = dialog.configuration()

    assert configured.output.directory == destination
    assert configured.ai == AIProfileConfiguration(model="qwen3:4b", context_window=4096)
    assert destination.name in dialog.destination_summary.text()
    assert str(destination) in dialog.destination_summary.toolTip()
    assert "Qwen 4B" in dialog.ai_summary.text()
    assert not hasattr(dialog, "output_directory")
    assert not hasattr(dialog, "ai_context")


def test_pdf_range_and_forced_ocr_live_under_additional_options(qtbot, tmp_path: Path) -> None:
    dialog = JobConfigurationDialog(_job(tmp_path), embedded=True)
    qtbot.addWidget(dialog)
    dialog.advanced_toggle.setChecked(True)
    dialog.page_range_enabled.setChecked(True)
    dialog.page_first.setValue(3)
    dialog.page_last.setValue(8)
    dialog.force_pdf_ocr.setChecked(True)

    configured = dialog.configuration()

    assert configured.page_range is not None
    assert (configured.page_range.first_page, configured.page_range.last_page) == (3, 8)
    assert configured.force_pdf_ocr
    assert not dialog.pdf_options.isHidden()


def test_non_pdf_ignores_pdf_specific_options(qtbot, tmp_path: Path) -> None:
    dialog = JobConfigurationDialog(_job(tmp_path, suffix=".txt"), embedded=True)
    qtbot.addWidget(dialog)
    dialog.advanced_toggle.setChecked(True)
    dialog.page_range_enabled.setChecked(True)
    dialog.force_pdf_ocr.setChecked(True)

    configured = dialog.configuration()

    assert not dialog.pdf_options.isVisible()
    assert configured.page_range is None
    assert not configured.force_pdf_ocr


def test_reviewed_plan_surfaces_missing_global_ai(qtbot, tmp_path: Path) -> None:
    dialog = JobConfigurationDialog(_job(tmp_path), embedded=True)
    qtbot.addWidget(dialog)
    dialog.plan_reviewed.setChecked(True)

    with pytest.raises(ValueError, match="modelo de IA local general"):
        dialog.configuration()


def test_editor_has_no_horizontal_scroll_at_compact_width(qtbot, tmp_path: Path) -> None:
    dialog = JobConfigurationDialog(_job(tmp_path), embedded=False)
    qtbot.addWidget(dialog)
    dialog.resize(320, 700)
    dialog.show()
    qtbot.wait(10)

    assert dialog.width() == 320
    assert dialog.scroll_area.horizontalScrollBarPolicy() is Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert dialog.scroll_area.horizontalScrollBar().maximum() == 0
    assert dialog.grab().save(str(tmp_path / "configuration-compact.png"))
    dialog.accept()


def test_escape_preserves_unsaved_choices_until_discard_is_confirmed(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    dialog = JobConfigurationDialog(_job(tmp_path), embedded=False)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.output_epub.setChecked(True)
    answers = iter(
        (
            QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Discard,
        )
    )
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: next(answers))

    qtbot.keyClick(dialog, Qt.Key.Key_Escape)
    assert dialog.isVisible()

    qtbot.keyClick(dialog, Qt.Key.Key_Escape)
    assert not dialog.isVisible()


def test_model_management_is_global_and_emits_one_action(qtbot, tmp_path: Path) -> None:
    dialog = JobConfigurationDialog(
        _job(tmp_path),
        embedded=True,
        default_ai_model="qwen3:4b",
    )
    qtbot.addWidget(dialog)
    dialog.plan_reviewed.setChecked(True)
    with qtbot.waitSignal(dialog.models_requested):
        dialog.manage_models_button.click()
