"""Rendered UI contracts for defects that structural widget tests cannot catch."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QApplication, QWidget

from parsezen.application.book_editor import create_book_from_markdown
from parsezen.application.planner import activate_next_stage
from parsezen.domain.attempt_activity import (
    AttemptEvent,
    AttemptEventStatus,
    AttemptPhase,
    AttemptTimeline,
    FailureSnapshot,
    ReusableWork,
)
from parsezen.domain.jobs import (
    DocumentJob,
    DocumentSource,
    JobConfiguration,
    OutputConfiguration,
)
from parsezen.domain.reviews import ReviewKind, ReviewSession, ReviewUnit
from parsezen.domain.stages import StageKind, StageStatus
from parsezen.epub_builder import EpubBookMetadata
from parsezen.failure_recovery import RecoveryAction, RecoveryPlan
from parsezen.infrastructure.artifact_store import ArtifactStore
from parsezen.local_models import OllamaModel, OllamaStatus
from parsezen.presentation.activity_view import ActivityView
from parsezen.presentation.book_editor_dialog import BookEditorDialog
from parsezen.presentation.design_system import ThemeMode, apply_parsezen_theme
from parsezen.presentation.job_configuration_dialog import JobConfigurationDialog
from parsezen.presentation.model_manager import ModelManagerDialog
from parsezen.presentation.phase_review_dialog import PhaseReviewDialog
from parsezen.presentation.workspace import InternalBackButton, ParsezenWorkspace
from parsezen.recent_activity import RecentJob, RecentJobStatus


def _make_job(path: Path) -> DocumentJob:
    path.write_text("Original", encoding="utf-8")
    return DocumentJob.create(
        DocumentSource.inspect(path),
        JobConfiguration(output=OutputConfiguration(configured=True)),
        order=0,
    )


def _reversible(payload: bytes) -> bytes:
    return bytes(value ^ 0x31 for value in payload)


def _relative_rect(widget: QWidget, ancestor: QWidget) -> QRect:
    return QRect(widget.mapTo(ancestor, QPoint(0, 0)), widget.size())


def _assert_fully_visible(widget: QWidget, ancestor: QWidget) -> None:
    assert widget.isVisibleTo(ancestor)
    bounds = _relative_rect(widget, ancestor)
    assert bounds.left() >= 0
    assert bounds.top() >= 0
    assert bounds.right() < ancestor.width()
    assert bounds.bottom() < ancestor.height()


def _failed_recent_job(path: Path) -> RecentJob:
    timestamp = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
    return RecentJob(
        path,
        RecentJobStatus.FAILED,
        timestamp,
        attempt_id="attempt-visual",
        timeline=AttemptTimeline(
            (
                AttemptEvent(
                    AttemptPhase.PREPARATION,
                    AttemptEventStatus.STARTED,
                    timestamp=timestamp,
                ),
                AttemptEvent(
                    AttemptPhase.PREPARATION,
                    AttemptEventStatus.COMPLETED,
                    timestamp=timestamp,
                ),
                AttemptEvent(
                    AttemptPhase.TRANSLATION,
                    AttemptEventStatus.STARTED,
                    timestamp=timestamp,
                ),
                AttemptEvent(
                    AttemptPhase.TRANSLATION,
                    AttemptEventStatus.FAILED,
                    timestamp=timestamp,
                ),
            )
        ),
        failure=FailureSnapshot(
            AttemptPhase.TRANSLATION,
            "transformation",
            "La traducción local se detuvo.",
            diagnostic_reference="visual-ref",
            reusable_work=ReusableWork.PREVIOUS_PHASES,
        ),
    )


@pytest.mark.parametrize("theme", [ThemeMode.LIGHT, ThemeMode.DARK])
@pytest.mark.parametrize("width", [320, 768])
def test_activity_render_matrix_keeps_failed_and_completed_states_inside_viewport(
    qtbot,
    tmp_path: Path,
    theme: ThemeMode,
    width: int,
) -> None:
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    apply_parsezen_theme(application, theme)
    completed_source = tmp_path / f"completed-{theme.value}-{width}.txt"
    completed_source.write_text("Original", encoding="utf-8")
    completed_result = tmp_path / f"completed-{theme.value}-{width}.md"
    completed_result.write_text("Resultado", encoding="utf-8")
    failed_current_source = tmp_path / f"failed-current-{theme.value}-{width}.txt"
    failed_current_source.write_text("Original", encoding="utf-8")
    failed_historical_source = tmp_path / f"failed-historical-{theme.value}-{width}.txt"
    failed_historical_source.write_text("Original", encoding="utf-8")
    cancelled_source = tmp_path / f"cancelled-{theme.value}-{width}.txt"
    cancelled_source.write_text("Original", encoding="utf-8")
    jobs = (
        RecentJob(
            completed_source,
            RecentJobStatus.COMPLETED,
            datetime(2026, 8, 3, 9, 0, tzinfo=UTC),
            completed_result,
        ),
        _failed_recent_job(failed_current_source),
        _failed_recent_job(failed_historical_source),
        RecentJob(
            cancelled_source,
            RecentJobStatus.CANCELLED,
            datetime(2026, 8, 3, 9, 30, tzinfo=UTC),
        ),
    )
    view = ActivityView(
        jobs,
        current_job_ids={failed_current_source: "current-job"},
    )
    qtbot.addWidget(view)
    view.resize(width, 720)
    view.show()
    qtbot.waitExposed(view)
    QApplication.processEvents()

    assert "Completado" in view.jobs_list.item(0).text()
    assert "Error en traducción" in view.jobs_list.item(1).text()
    assert "Error en traducción" in view.jobs_list.item(2).text()
    assert "Cancelado" in view.jobs_list.item(3).text()
    assert view._compact is (width <= 760)  # noqa: SLF001
    for row in range(len(jobs)):
        view.jobs_list.setCurrentRow(row)
        QApplication.processEvents()
        snapshot = view.grab()
        assert snapshot.size() == QSize(width, 720)
        assert snapshot.save(str(tmp_path / f"activity-{theme.value}-{width}-{row}.png"))
        assert view.jobs_list.horizontalScrollBar().maximum() == 0
        for widget in (view.jobs_list, view.details):
            bounds = _relative_rect(widget, view)
            assert bounds.left() >= 0
            assert bounds.right() < view.width()


@pytest.mark.parametrize("theme", [ThemeMode.LIGHT, ThemeMode.DARK])
@pytest.mark.parametrize("width", [320, 768, 1440])
def test_workspace_render_matrix_keeps_core_actions_inside_the_viewport(
    qtbot,
    tmp_path: Path,
    theme: ThemeMode,
    width: int,
) -> None:
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    apply_parsezen_theme(application, theme)
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    workspace.set_jobs((_make_job(tmp_path / f"workspace-{theme.value}-{width}.txt"),))
    workspace.resize(width, 760)
    workspace.show()
    qtbot.waitExposed(workspace)
    QApplication.processEvents()

    snapshot = workspace.grab()
    snapshot_path = tmp_path / f"workspace-{theme.value}-{width}.png"
    assert snapshot.save(str(snapshot_path))
    assert snapshot.size() == QSize(width, 760)
    assert snapshot.toImage().pixelColor(width // 2, 10).alpha() == 255

    for control in (
        workspace.theme_button,
        workspace.local_ai_button,
        workspace.output_directory_button,
        workspace.primary_button,
        workspace.drop_area,
        workspace.table_panel,
        workspace.content_stack,
    ):
        _assert_fully_visible(control, workspace)

    theme_rect = _relative_rect(workspace.theme_button, workspace)
    ai_rect = _relative_rect(workspace.local_ai_button, workspace)
    output_rect = _relative_rect(workspace.output_directory_button, workspace)
    if width <= 760:
        assert ai_rect.top() > theme_rect.top()
        assert output_rect.top() == ai_rect.top()
    else:
        assert abs(ai_rect.center().y() - theme_rect.center().y()) <= 2
        assert abs(output_rect.center().y() - theme_rect.center().y()) <= 2


@pytest.mark.parametrize("theme", [ThemeMode.LIGHT, ThemeMode.DARK])
@pytest.mark.parametrize("width", [320, 768, 1440])
def test_running_row_render_matrix_keeps_progress_inside_the_row(
    qtbot,
    tmp_path: Path,
    theme: ThemeMode,
    width: int,
) -> None:
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    apply_parsezen_theme(application, theme)
    job = _make_job(tmp_path / f"running-{theme.value}-{width}.txt")
    ready = activate_next_stage(job)
    running = ready.replace_stage(
        ready.stage(StageKind.PREPARE)
        .transition(StageStatus.RUNNING)
        .with_progress(25, 100, "Extrayendo")
    )
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    workspace.set_jobs((running,))
    workspace.resize(width, 760)
    workspace.show()
    qtbot.waitExposed(workspace)
    QApplication.processEvents()

    snapshot = workspace.grab()
    assert snapshot.size() == QSize(width, 760)
    assert snapshot.save(str(tmp_path / f"running-{theme.value}-{width}.png"))
    assert workspace.job_table.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff


@pytest.mark.parametrize("theme", [ThemeMode.LIGHT, ThemeMode.DARK])
@pytest.mark.parametrize("width", [320, 768, 1440])
def test_phase_review_render_matrix_keeps_current_session_progress_and_actions_visible(
    qtbot,
    tmp_path: Path,
    theme: ThemeMode,
    width: int,
) -> None:
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    apply_parsezen_theme(application, theme)
    store = ArtifactStore(
        tmp_path / f"phase-artifacts-{theme.value}-{width}",
        protect=_reversible,
        unprotect=_reversible,
    )
    original = store.put_text(job_id="job", text="Texto actual")
    proposed = store.put_text(job_id="job", text="Texto propuesto")
    review = ReviewSession.create(
        job_id="job",
        stage=StageKind.REFINE,
        kind=ReviewKind.REFINEMENT,
        input_artifact_id=original.id,
        input_version=1,
        units=(ReviewUnit("unit", original.id, proposed.id),),
    )
    dialog = PhaseReviewDialog(review, store)
    qtbot.addWidget(dialog)
    dialog.resize(width, 760)
    dialog.show()
    qtbot.waitExposed(dialog)
    QApplication.processEvents()

    snapshot = dialog.grab()
    assert snapshot.size() == QSize(width, 760)
    assert snapshot.save(str(tmp_path / f"phase-{theme.value}-{width}.png"))
    assert "0 de 1 decisiones" in dialog.progress_indicator.accessibleDescription()
    assert dialog.next_button.isVisible()
    assert dialog.save_later_button.isVisible()
    assert dialog.proposed_pane.editor.accessibleName()


@pytest.mark.parametrize("theme", [ThemeMode.LIGHT, ThemeMode.DARK])
@pytest.mark.parametrize("width", [320, 768, 1440])
def test_epub_editor_render_matrix_keeps_safe_exit_and_helper_visible(
    qtbot,
    tmp_path: Path,
    theme: ThemeMode,
    width: int,
) -> None:
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    apply_parsezen_theme(application, theme)
    store = ArtifactStore(
        tmp_path / f"epub-artifacts-{theme.value}-{width}",
        protect=_reversible,
        unprotect=_reversible,
    )
    book = create_book_from_markdown(
        "# Chapter\n\nReadable content.",
        (),
        EpubBookMetadata("Book", "en"),
        store,
        job_id="job",
    )
    dialog = BookEditorDialog(book, store, job_id="job", destination=None)
    qtbot.addWidget(dialog)
    dialog.resize(width, 760)
    dialog.show()
    qtbot.waitExposed(dialog)
    QApplication.processEvents()

    snapshot = dialog.grab()
    assert snapshot.size() == QSize(width, 760)
    assert snapshot.save(str(tmp_path / f"epub-{theme.value}-{width}.png"))
    assert dialog.dialog_title.text() == "Revisión final del EPUB"
    assert dialog.review_helper.isVisible()
    assert dialog.save_later_button.text() == "Guardar y salir"
    assert dialog.cancel_button.text() == "Descartar cambios"


@pytest.mark.parametrize("theme", [ThemeMode.LIGHT, ThemeMode.DARK])
@pytest.mark.parametrize("width", [320, 768])
def test_recovery_actions_remain_visible_without_overlap(
    qtbot,
    tmp_path: Path,
    theme: ThemeMode,
    width: int,
) -> None:
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    apply_parsezen_theme(application, theme)
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    workspace.resize(width, 760)
    workspace.show()
    qtbot.waitExposed(workspace)
    workspace.show_job_error(
        "job",
        RecoveryPlan(
            "El control final detuvo la publicación",
            "El temporal no coincide con el contenido validado.",
            "El resultado anterior permanece intacto.",
            RecoveryAction.RETRY,
            "Reintentar esta fase",
            RecoveryAction.CONFIGURE,
            "Revisar configuración",
        ),
        StageKind.PUBLISH,
    )
    QApplication.processEvents()

    snapshot = workspace.grab()
    assert snapshot.save(str(tmp_path / f"recovery-{theme.value}-{width}.png"))
    _assert_fully_visible(workspace.job_message, workspace)
    _assert_fully_visible(workspace.job_message.action, workspace)
    _assert_fully_visible(workspace.job_message.secondary_action, workspace)
    assert not workspace.job_message.action.geometry().intersects(
        workspace.job_message.secondary_action.geometry()
    )


@pytest.mark.parametrize("theme", [ThemeMode.LIGHT, ThemeMode.DARK])
def test_configuration_result_cards_render_as_one_progressive_choice(
    qtbot,
    tmp_path: Path,
    theme: ThemeMode,
) -> None:
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    apply_parsezen_theme(application, theme)
    editor = JobConfigurationDialog(
        _make_job(tmp_path / f"{theme.value}.txt"),
        embedded=False,
    )
    qtbot.addWidget(editor)
    editor.resize(680, 520)
    editor.show()
    qtbot.waitExposed(editor)
    QApplication.processEvents()

    for card in (editor.markdown_card, editor.epub_card):
        snapshot = card.grab()
        assert not snapshot.isNull()
        assert snapshot.width() == card.width()
        assert snapshot.height() == card.height()

    assert editor.markdown_card.property("selected") is True
    assert not editor.advanced_panel.isVisible()
    editor.output_epub.setChecked(True)
    assert editor.epub_card.property("selected") is True
    assert not editor.output_markdown.isChecked()
    assert editor.grab().save(str(tmp_path / f"configuration-sheet-{theme.value}.png"))
    editor.accept()


@pytest.mark.parametrize("theme", [ThemeMode.LIGHT, ThemeMode.DARK])
def test_configuration_advanced_exceptions_stay_in_one_vertical_disclosure(
    qtbot,
    tmp_path: Path,
    theme: ThemeMode,
) -> None:
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    apply_parsezen_theme(application, theme)
    editor = JobConfigurationDialog(
        _make_job(tmp_path / f"advanced-{theme.value}.pdf"),
        embedded=False,
        default_ai_model="qwen3:4b",
        models=(("qwen3:4b", "Qwen 3 4B"),),
        ollama_status=OllamaStatus.READY,
    )
    qtbot.addWidget(editor)
    editor.resize(680, 620)
    editor.translation_target.setCurrentIndex(editor.translation_target.findData("es"))
    editor.advanced_toggle.setChecked(True)
    editor.show()
    qtbot.waitExposed(editor)
    QApplication.processEvents()

    assert editor.advanced_panel.isVisible()
    assert editor.translation_method_row.isVisible()
    assert editor.pdf_options.isVisible()
    assert editor.scroll_area.horizontalScrollBar().maximum() == 0
    assert editor.grab().save(str(tmp_path / f"configuration-advanced-{theme.value}.png"))
    editor.accept()


@pytest.mark.parametrize("theme", [ThemeMode.LIGHT, ThemeMode.DARK])
def test_batch_configuration_label_wraps_without_clipping_below_its_separator(
    qtbot,
    tmp_path: Path,
    theme: ThemeMode,
) -> None:
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    apply_parsezen_theme(application, theme)
    editor = JobConfigurationDialog(
        _make_job(tmp_path / f"batch-{theme.value}.txt"),
        embedded=True,
        compatible_job_count=2,
    )
    qtbot.addWidget(editor)
    editor.resize(980, 680)
    editor.show()
    qtbot.waitExposed(editor)
    QApplication.processEvents()

    label = editor.apply_compatible_label
    text_bounds = label.fontMetrics().boundingRect(
        QRect(0, 0, label.width(), 1_000),
        Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignLeft,
        label.text(),
    )

    assert label.height() >= text_bounds.height()
    assert editor.apply_compatible.isVisible()
    assert editor.grab().save(str(tmp_path / f"batch-configuration-{theme.value}.png"))


def test_internal_back_action_has_icon_only_hover_growth(qtbot) -> None:
    button = InternalBackButton()
    qtbot.addWidget(button)
    button.resize(38, 34)
    button.show()
    qtbot.waitExposed(button)
    assert button.text() == ""
    assert button.iconSize() == QSize(20, 20)

    QApplication.sendEvent(button, QEvent(QEvent.Type.Enter))
    assert button.iconSize() == QSize(24, 24)

    QApplication.sendEvent(button, QEvent(QEvent.Type.Leave))
    assert button.iconSize() == QSize(20, 20)


def test_switch_remains_keyboard_operable_without_clipping(qtbot, tmp_path: Path) -> None:
    editor = JobConfigurationDialog(
        _make_job(tmp_path / "keyboard.txt"),
        embedded=True,
    )
    qtbot.addWidget(editor)
    editor.resize(980, 680)
    editor.show()
    qtbot.waitExposed(editor)
    editor.advanced_toggle.setChecked(True)
    switch = editor.plan_reviewed
    switch.setFocus()
    QApplication.processEvents()
    assert switch.hasFocus()
    before = switch.isChecked()

    qtbot.keyClick(switch, Qt.Key.Key_Space)

    assert switch.isChecked() is not before
    image = switch.grab().toImage()
    assert image.size() == switch.size()
    assert image.pixelColor(43, switch.height() // 2).alpha() >= 240


@pytest.mark.parametrize("theme", [ThemeMode.LIGHT, ThemeMode.DARK])
def test_model_manager_reflows_its_complete_row_at_320(
    qtbot,
    tmp_path: Path,
    theme: ThemeMode,
) -> None:
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    apply_parsezen_theme(application, theme)
    model = OllamaModel(
        "qwen3:4b-instruct",
        "Qwen3 4B Instruct Q4",
        size_bytes=2_500_000_000,
    )
    manager = ModelManagerDialog(
        (model,),
        model.model_id,
        context_window=None,
        ollama_status=OllamaStatus.READY,
        ollama_message="Ollama listo",
    )
    qtbot.addWidget(manager)
    manager.resize(320, 720)
    manager.show()
    qtbot.waitExposed(manager)
    QApplication.processEvents()

    snapshot = manager.grab()
    snapshot_path = tmp_path / f"models-{theme.value}-320.png"
    assert snapshot.save(str(snapshot_path))
    assert snapshot.size() == QSize(320, 720)
    assert manager._compact is True  # noqa: SLF001
    assert not manager.models_scroll.horizontalScrollBar().isVisible()
    for control in (
        manager.context_window,
        manager.filter_buttons["installed"],
        manager.filter_buttons["recommended"],
        manager.installed_select_buttons[model.model_id],
        manager.model_menu_buttons[model.model_id],
    ):
        _assert_fully_visible(control, manager)
