"""Rendered UI contracts for defects that structural widget tests cannot catch."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from parsezen.application.recovery import RecoveryAction, RecoveryPlan
from parsezen.domain.jobs import (
    DocumentJob,
    DocumentSource,
    JobConfiguration,
    OutputConfiguration,
)
from parsezen.domain.stages import StageKind
from parsezen.local_models import OllamaModel, OllamaStatus
from parsezen.model_manager import ModelManagerDialog
from parsezen.presentation.design_system import ThemeMode, apply_parsezen_theme
from parsezen.presentation.job_configuration_dialog import JobConfigurationDialog
from parsezen.presentation.workspace import InternalBackButton, ParsezenWorkspace


def _make_job(path: Path) -> DocumentJob:
    path.write_text("Original", encoding="utf-8")
    return DocumentJob.create(
        DocumentSource.inspect(path),
        JobConfiguration(output=OutputConfiguration(configured=True)),
        order=0,
    )


def _relative_rect(widget: QWidget, ancestor: QWidget) -> QRect:
    return QRect(widget.mapTo(ancestor, QPoint(0, 0)), widget.size())


def _assert_fully_visible(widget: QWidget, ancestor: QWidget) -> None:
    assert widget.isVisibleTo(ancestor)
    bounds = _relative_rect(widget, ancestor)
    assert bounds.left() >= 0
    assert bounds.top() >= 0
    assert bounds.right() < ancestor.width()
    assert bounds.bottom() < ancestor.height()


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
def test_configuration_navigation_renders_switch_inside_one_interaction_surface(
    qtbot,
    tmp_path: Path,
    theme: ThemeMode,
) -> None:
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    apply_parsezen_theme(application, theme)
    editor = JobConfigurationDialog(
        _make_job(tmp_path / f"{theme.value}.txt"),
        embedded=True,
    )
    qtbot.addWidget(editor)
    editor.resize(980, 680)
    editor.show()
    qtbot.waitExposed(editor)
    QApplication.processEvents()

    for row in (editor.translation_navigation, editor.refinement_navigation):
        switch = row.activation
        assert switch is not None
        switch_rect = _relative_rect(switch, row)
        button_rect = _relative_rect(row.button, row)
        assert row.rect().contains(switch_rect)
        assert row.rect().contains(button_rect)
        assert not button_rect.intersects(switch_rect)
        assert switch.height() >= switch.sizeHint().height()

        row.setProperty("hovered", True)
        row.style().unpolish(row)
        row.style().polish(row)
        snapshot = row.grab()
        assert not snapshot.isNull()
        assert snapshot.width() == row.width()
        assert snapshot.height() == row.height()

    editor._select_section(editor.refinement_tab_index)
    assert editor.refinement_navigation.property("selected") is True
    assert editor.refinement_navigation.button.styleSheet() == ""
    assert editor.refinement_navigation.button.autoDefault() is False


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

    label = editor.apply_compatible_row.findChild(QLabel)
    assert label is not None
    text_bounds = label.fontMetrics().boundingRect(
        QRect(0, 0, label.width(), 1_000),
        Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignLeft,
        label.text(),
    )

    assert label.height() >= text_bounds.height()
    assert editor.apply_compatible_separator.isVisible()
    assert (
        editor.apply_compatible_separator.geometry().bottom()
        < editor.apply_compatible_row.geometry().top()
    )
    assert editor.apply_compatible_section.grab().save(
        str(tmp_path / f"batch-configuration-{theme.value}.png")
    )


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
    editor._select_section(editor.translation_tab_index)
    switch = editor.translation_enabled
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
