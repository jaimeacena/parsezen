from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QScrollArea

from parsezen.application.planner import activate_next_stage
from parsezen.application.preflight import DocumentPreflight, combine_preflights
from parsezen.domain.estimates import DurationEstimate
from parsezen.domain.jobs import (
    DocumentFormat,
    DocumentJob,
    DocumentSource,
    JobConfiguration,
    OutputConfiguration,
)
from parsezen.domain.stages import StageKind, StageStatus
from parsezen.failure_recovery import RecoveryAction, RecoveryPlan
from parsezen.final_integrity import FinalIntegrityReport, IntegrityLedger
from parsezen.local_models import OllamaStatus
from parsezen.presentation.activity_view import ActivityView
from parsezen.presentation.design_system import COLORS, SPACING
from parsezen.presentation.job_table import CELL_PRESENTATION_ROLE
from parsezen.presentation.workspace import ParsezenWorkspace


def make_job(identifier: str, order: int) -> DocumentJob:
    return activate_next_stage(
        DocumentJob.create(
            DocumentSource(
                Path(f"{identifier}.pdf"),
                DocumentFormat.PDF,
                100,
                1,
            ),
            JobConfiguration(output=OutputConfiguration(format=DocumentFormat.MARKDOWN)),
            order=order,
            job_id=identifier,
        )
    )


def make_review_job(identifier: str, order: int = 0) -> DocumentJob:
    job = make_job(identifier, order)
    return job.replace_stage(
        job.stage(StageKind.PREPARE)
        .transition(StageStatus.RUNNING)
        .transition(StageStatus.BLOCKED_FOR_REVIEW, review_id="review")
    )


def test_workspace_header_reflects_running_document(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    one = make_job("one", 0)
    running = one.replace_stage(
        one.stage(StageKind.PREPARE).transition(StageStatus.RUNNING).with_progress(63, 100)
    )
    workspace.set_jobs((running, make_job("two", 1)))

    assert workspace.queue_summary.text() == "2 documentos"
    assert workspace.primary_button.text() == "Pausar"
    assert not workspace.primary_button.icon().isNull()
    assert workspace.primary_button.isVisible() or not workspace.isVisible()
    assert not hasattr(workspace, "footer")

    modes: list[str] = []
    workspace.primary_requested.connect(modes.append)
    workspace.primary_button.click()

    assert modes == ["pause"]
    assert workspace.primary_button.text() == "Pausando…"
    assert not workspace.primary_button.isEnabled()


def test_document_drop_area_is_neutral_until_dragging(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)

    stylesheet = workspace.styleSheet()

    assert f"background-color: {COLORS.surface_raised};" in stylesheet
    assert f"border: 1px solid {COLORS.divider};" in stylesheet
    assert stylesheet.count(f"border: 2px dashed {COLORS.action_primary};") == 1


def test_workspace_header_prioritizes_real_reviews(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    workspace.set_jobs((make_review_job("one"),))

    assert workspace.primary_button.text() == "Revisar 1 pendiente"
    assert "1 por revisar" in workspace.queue_summary.text()


def test_wide_queue_summary_stays_as_a_content_heading(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    workspace.resize(1440, 800)
    workspace.set_jobs(tuple(make_review_job(f"review-{index}", index) for index in range(5)))
    workspace.show()
    qtbot.waitExposed(workspace)

    assert workspace.queue_summary.wordWrap() is False
    assert workspace.queue_summary.parent() is workspace.queue_pane
    assert workspace.queue_layout.indexOf(workspace.queue_summary) == 0
    assert workspace.queue_summary.text() == "5 documentos · 5 por revisar"


def test_workspace_emits_current_primary_mode(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    workspace.set_jobs((make_job("one", 0),))
    modes: list[str] = []
    workspace.primary_requested.connect(modes.append)

    workspace.primary_button.click()

    assert modes == ["process"]
    assert not workspace.primary_button.icon().isNull()


def test_workspace_summarizes_the_predicted_automatic_time(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    job = make_job("one", 0)
    forecast = DocumentPreflight(
        job.id,
        "20 páginas",
        DurationEstimate(120, 180, 300),
        (),
        (),
    )
    workspace.set_jobs((job,))
    workspace.set_preflight({job.id: forecast}, combine_preflights((forecast,)))

    assert workspace.queue_summary.text() == "1 documento · ~2 min–5 min"
    assert "Tiempo automático" in (
        workspace.job_table.job_model.index(0, 4).data(Qt.ItemDataRole.AccessibleTextRole)
    )
    workspace.resize(1380, 800)
    workspace.show()
    qtbot.waitExposed(workspace)
    assert workspace.queue_summary.width() >= 100
    assert workspace.queue_summary.isVisible()


def test_workspace_shows_preparing_during_execution_preflight(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    workspace.set_jobs((make_job("one", 0),))

    workspace.set_preparing_jobs(("one",))

    assert workspace.primary_button.text() == "Preparando…"
    assert not workspace.primary_button.isEnabled()
    assert (
        workspace.job_table.job_model.index(0, 4)
        .data(Qt.ItemDataRole.AccessibleTextRole)
        .startswith("Estado · Preparando")
    )


def test_long_early_check_keeps_pause_available_while_saying_preparing(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    workspace.set_jobs((make_job("one", 0),))

    workspace.set_preparing_jobs(
        ("one",),
        {"one": "Comprobando 1 de 3 páginas representativas"},
        can_pause=True,
    )

    status = workspace.job_table.job_model.index(0, 4).data(CELL_PRESENTATION_ROLE)
    assert workspace.primary_button.text() == "Pausar"
    assert workspace.primary_button.isEnabled()
    assert status.title == "Preparando"
    assert "1 de 3" in status.subtitle


def test_workspace_does_not_count_missing_output_as_review(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    job = DocumentJob.create(
        DocumentSource(Path("draft.pdf"), DocumentFormat.PDF, 100, 1),
        JobConfiguration(output=OutputConfiguration(configured=False)),
        order=0,
    )
    workspace.set_jobs((job,))

    assert workspace.queue_summary.text() == "1 documento"
    assert workspace.primary_button.isHidden()


def test_workspace_shows_and_clears_recovery_warning(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)

    workspace.set_recovery_warning("No se pudo guardar la recuperación automática.")

    assert not workspace.recovery_warning.isHidden()
    assert "recuperación automática" in workspace.recovery_warning.text()
    assert workspace.recovery_warning.toolTip() == workspace.recovery_warning.text()

    workspace.set_recovery_warning(None)

    assert not workspace.recovery_warning.isVisible()


def test_workspace_routes_contextual_recovery_actions(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    retries: list[str] = []
    configurations: list[tuple[str, object]] = []
    workspace.retry_requested.connect(retries.append)
    workspace.configure_requested.connect(
        lambda job_id, stage: configurations.append((job_id, stage))
    )
    workspace.show_job_error(
        "job",
        RecoveryPlan(
            "No se pudo publicar",
            "El archivo temporal cambió.",
            "El resultado anterior permanece intacto.",
            RecoveryAction.RETRY,
            "Reintentar",
            RecoveryAction.CONFIGURE,
            "Revisar configuración",
        ),
        StageKind.PUBLISH,
    )

    workspace.job_message.action.click()
    assert retries == ["job"]

    workspace.show_job_error(
        "job",
        RecoveryPlan(
            "No se pudo publicar",
            "El destino está bloqueado.",
            "El resultado anterior permanece intacto.",
            RecoveryAction.CONFIGURE,
            "Revisar destino",
        ),
        StageKind.PUBLISH,
    )
    workspace.job_message.action.click()

    assert configurations == [("job", StageKind.PUBLISH)]


def test_contextual_recovery_reflows_without_overlapping_at_320px(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    workspace.resize(320, 700)
    workspace.show()
    qtbot.waitExposed(workspace)
    workspace.show_job_error(
        "job",
        RecoveryPlan(
            "El control final detuvo la publicación",
            "El archivo temporal cambió.",
            "El resultado anterior permanece intacto.",
            RecoveryAction.RETRY,
            "Reintentar esta fase",
            RecoveryAction.CONFIGURE,
            "Revisar configuración",
        ),
        StageKind.PUBLISH,
    )
    QApplication.processEvents()

    assert workspace.job_message.action.geometry().bottom() < (
        workspace.job_message.secondary_action.geometry().top()
    )
    assert workspace.job_message.secondary_action.geometry().right() <= (
        workspace.job_message.contentsRect().right()
    )


def test_workspace_exposes_verified_final_integrity(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    job = make_job("verified", 0)
    completed = replace(
        job,
        stages=tuple(
            replace(stage, status=StageStatus.COMPLETED) if stage.participates else stage
            for stage in job.stages
        ),
        result_path=Path("verified.md"),
    )
    workspace.set_integrity_reports(
        {
            completed.id: FinalIntegrityReport(
                "Markdown",
                ("Contenido aprobado conservado",),
                IntegrityLedger(blocks=2, headings=1),
            )
        }
    )
    workspace.set_jobs((completed,))

    index = workspace.job_table.job_model.index(0, 4)

    assert "Integridad final comprobada" in index.data(Qt.ItemDataRole.AccessibleTextRole)
    assert "Contenido aprobado conservado" in index.data(Qt.ItemDataRole.ToolTipRole)


def test_workspace_internal_views_return_to_the_page_that_opened_them(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    editor = QLabel("Configuración")
    review = QLabel("Revisión")
    workspace.show_internal_view(editor, "Editor")

    workspace.show_internal_view(review, "Revisar")

    assert workspace.current_internal_widget is review
    assert workspace.current_internal_widget is review

    workspace.close_internal_view(review)

    assert workspace.content_stack.currentWidget() is workspace._internal_pages[editor][0]  # noqa: SLF001
    assert workspace.current_internal_widget is editor


def test_nested_internal_views_restore_focus_at_each_navigation_level(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    workspace.show()
    qtbot.waitExposed(workspace)
    workspace.output_directory_button.setFocus()
    editor = QPushButton("Modelo específico")
    workspace.show_internal_view(editor, "Editor")
    editor.setFocus()

    manager = QLabel("Modelos")
    workspace.show_internal_view(manager, "IA local")
    workspace.close_internal_view(manager)

    qtbot.waitUntil(editor.hasFocus)
    workspace.close_internal_view(editor)
    qtbot.waitUntil(workspace.output_directory_button.hasFocus)


def test_workspace_internal_view_replaces_the_global_header(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    manager = QLabel("Modelos")

    workspace.show_internal_view(manager, "IA local")
    assert workspace.app_header.isHidden()
    assert workspace.settings_anchor().parent().objectName() == "internalPageHeader"

    workspace.close_internal_view(manager)
    assert not workspace.app_header.isHidden()
    assert workspace.settings_anchor() is workspace.settings_button


def test_workspace_keeps_local_ai_readiness_without_a_permanent_header_action(
    qtbot,
) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)

    workspace.set_local_ai_status(OllamaStatus.READY, "qwen3:4b-instruct")

    assert workspace._local_ai_status is OllamaStatus.READY  # noqa: SLF001
    assert workspace._local_ai_model == "qwen3:4b-instruct"  # noqa: SLF001
    assert not hasattr(workspace, "local_ai_button")


def test_workspace_does_not_reserve_header_space_for_an_unknown_ai_state(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)

    workspace.set_local_ai_status(None, None)

    assert workspace._local_ai_status is None  # noqa: SLF001
    assert not hasattr(workspace, "local_ai_button")


def test_workspace_exposes_the_global_destination_compactly(qtbot, tmp_path: Path) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)

    workspace.set_output_directory(tmp_path / "Resultados")

    assert workspace.output_directory_button.text() == "Guardar en · Resultados"
    assert str(tmp_path / "Resultados") == workspace.output_directory_button.toolTip()


def test_populated_queue_exposes_add_action_in_the_single_header(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    workspace.set_jobs((make_job("one", 0), make_job("two", 1)))
    queue_layout = workspace.queue_pane.layout()
    additions: list[str] = []
    workspace.add_requested.connect(lambda: additions.append("add"))

    assert queue_layout.indexOf(workspace.queue_summary) == 0
    assert queue_layout.indexOf(workspace.table_panel) == 1
    assert queue_layout.indexOf(workspace.drop_area) == 2
    assert workspace.drop_area.secondary_label.text() == "TXT · MD · DOCX · PDF · EPUB"
    assert workspace.drop_area.isHidden()
    assert workspace.queue_summary.isVisible() or not workspace.isVisible()
    assert workspace.add_button.text() == "Añadir"
    assert workspace.add_button.parent() is workspace.app_header
    assert not workspace.add_button.icon().isNull()
    workspace.add_button.click()
    assert additions == ["add"]
    assert workspace.job_table.height() < 300
    assert workspace.table_panel.height() == workspace.job_table.height() + 2


def test_workspace_drop_emits_each_local_document(qtbot, tmp_path: Path) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    first = tmp_path / "one.pdf"
    second = tmp_path / "two.epub"
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(first)), QUrl.fromLocalFile(str(second))])
    event = QDropEvent(
        QPointF(10, 10),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    dropped: list[tuple[Path, ...]] = []
    workspace.files_dropped.connect(dropped.append)

    workspace.dropEvent(event)

    assert dropped == [(first, second)]


def test_workspace_uses_one_bounded_rail_without_a_redundant_inspector(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    workspace.resize(1380, 800)
    workspace.set_jobs((make_job("one", 0), make_job("two", 1)))
    workspace.show()
    qtbot.waitExposed(workspace)

    assert not hasattr(workspace, "inspector")
    assert workspace.queue_pane.layout().indexOf(workspace.queue_summary) == 0
    assert workspace.queue_pane.layout().indexOf(workspace.table_panel) == 1
    assert workspace.table_panel.layout().indexOf(workspace.job_table) == 0
    assert workspace.queue_summary.width() == workspace.table_panel.width()
    assert workspace.app_header.width() == workspace.table_panel.width()
    assert workspace.table_panel.width() <= 1280
    assert workspace.table_panel._outline.geometry() == workspace.table_panel.rect()  # noqa: SLF001
    assert workspace.table_panel._outline.testAttribute(  # noqa: SLF001
        Qt.WidgetAttribute.WA_TransparentForMouseEvents
    )


def test_internal_titles_share_the_centered_outer_rail(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    workspace.resize(2160, 1280)
    workspace.show()
    qtbot.waitExposed(workspace)
    internal = QLabel("Contenido")

    workspace.show_internal_view(internal, "Configurar documento")
    QApplication.processEvents()

    header = workspace.findChild(QLabel, "internalPageTitle").parentWidget()
    assert header.width() == workspace.app_header.width() == 1280
    header_center = header.mapTo(workspace, header.rect().center())
    assert abs(header_center.x() - workspace.rect().center().x()) <= 1


def test_contextual_messages_expand_and_disappear_with_their_document(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    workspace.resize(320, 700)
    workspace.set_jobs((make_job("one", 0),))
    workspace.show()
    qtbot.waitExposed(workspace)
    workspace.show_batch_summary(
        "Procesamiento detenido. El trabajo local se interrumpió antes de publicar el resultado.",
        tone="warning",
        job_ids=("one",),
    )
    QApplication.processEvents()

    message_bottom = (
        workspace.batch_message.message.geometry().bottom()
        + workspace.batch_message.layout().contentsMargins().bottom()
    )
    assert workspace.batch_message.height() >= message_bottom
    assert workspace.batch_message.height() > 38

    workspace.set_jobs(())

    assert workspace.batch_message.isHidden()


def test_empty_workspace_anchors_its_only_task_near_the_header(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    workspace.resize(2160, 1280)
    workspace.show()
    qtbot.waitExposed(workspace)

    assert not workspace.table_panel.isVisible()
    assert not workspace.queue_summary.isVisible()
    assert not workspace.add_button.isVisible()
    assert workspace.drop_area.isVisible()
    assert workspace.queue_layout.contentsMargins().top() == SPACING.xxxl
    assert workspace.drop_area.geometry().top() == SPACING.xxxl
    assert workspace.drop_area.geometry().center().y() < workspace.queue_pane.height() // 3
    assert workspace.drop_area.width() <= 620
    assert workspace.drop_area.primary_label.text() == "Arrastra documentos aquí"
    assert workspace.drop_area.browse_button.text() == "Seleccionar archivos"
    assert workspace.drop_area.focusPolicy() is Qt.FocusPolicy.NoFocus
    assert workspace.drop_area.browse_button.focusPolicy() is Qt.FocusPolicy.TabFocus
    assert (
        abs(workspace.drop_area.geometry().center().x() - workspace.queue_pane.rect().center().x())
        <= 1
    )


def test_empty_workspace_picker_uses_the_same_add_signal(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    additions: list[str] = []
    workspace.add_requested.connect(lambda: additions.append("add"))

    workspace.drop_area.browse_button.click()

    assert additions == ["add"]


@pytest.mark.parametrize("job_count", [1, 3, 8])
def test_wide_tall_workspace_hugs_one_or_many_queue_rows(qtbot, job_count: int) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    workspace.resize(2160, 1280)
    workspace.set_jobs(tuple(make_job(f"job-{index}", index) for index in range(job_count)))
    workspace.show()
    qtbot.waitExposed(workspace)

    assert workspace.job_table.job_model.rowCount() == job_count
    assert workspace.queue_summary.geometry().top() == 0
    assert (
        0
        < workspace.table_panel.geometry().top() - workspace.queue_summary.geometry().bottom()
        <= 12
    )
    assert workspace.table_panel.height() == workspace.job_table.height() + 2
    assert workspace.table_panel.width() <= 1280
    assert workspace.queue_summary.width() == workspace.table_panel.width()
    assert workspace.add_button.isVisible()
    assert workspace.add_button.x() < workspace.primary_button.x()
    assert workspace.drop_area.isHidden()


def test_workspace_header_exposes_queue_actions_and_one_global_menu(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    actions: list[str] = []
    workspace.add_requested.connect(lambda: actions.append("add"))
    workspace.settings_requested.connect(lambda: actions.append("settings"))

    workspace.set_jobs((make_job("one", 0),))
    workspace.add_button.click()
    workspace.settings_button.click()

    assert actions == ["add", "settings"]
    assert not hasattr(workspace, "local_ai_button")
    assert workspace.output_directory_button.focusPolicy() is Qt.FocusPolicy.TabFocus
    assert workspace.settings_button.focusPolicy() is Qt.FocusPolicy.TabFocus
    assert "Procesamiento local" not in tuple(
        label.text() for label in workspace.findChildren(QLabel)
    )
    assert workspace.queue_summary.parent() is workspace.queue_pane
    assert workspace.primary_button.parent() is workspace.app_header
    assert workspace.add_button.parent() is workspace.app_header
    assert not hasattr(workspace, "theme_button")


def test_global_menu_marks_only_actionable_document_attention(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)

    workspace.set_jobs((make_job("queued", 0),))
    assert "acciones pendientes" not in workspace.settings_button.accessibleName()

    workspace.set_jobs((make_review_job("review"),))
    assert "acciones pendientes" in workspace.settings_button.accessibleName()

    workspace.set_jobs(())
    assert "acciones pendientes" not in workspace.settings_button.accessibleName()


def test_workspace_reflows_at_320_without_horizontal_overflow(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    workspace.resize(320, 720)
    workspace.set_jobs((make_job("one", 0),))
    workspace.show()
    qtbot.waitExposed(workspace)

    assert workspace.width() == 320
    assert workspace._compact_layout is True  # noqa: SLF001
    assert workspace.output_directory_button.y() == workspace.logo.y()
    assert workspace.primary_button.minimumWidth() == 38
    assert workspace.output_directory_button.text() == ""
    assert workspace.add_button.text() == ""
    assert workspace.primary_button.text() == ""
    assert workspace.primary_button.accessibleName() == "Procesar 1 documento"
    assert workspace.job_table.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert workspace.queue_summary.mapTo(
        workspace, workspace.queue_summary.rect().topLeft()
    ).y() > (workspace.add_button.mapTo(workspace, workspace.add_button.rect().topLeft()).y())
    assert (
        abs(
            workspace.add_button.geometry().center().y()
            - workspace.primary_button.geometry().center().y()
        )
        <= 1
    )
    assert workspace.drop_area.isHidden()


def test_workspace_uses_intermediate_header_and_compact_table_at_768(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    workspace.resize(768, 720)
    workspace.set_jobs((make_job("one", 0),))
    workspace.show()
    qtbot.waitExposed(workspace)

    assert workspace._layout_mode == "medium"  # noqa: SLF001
    assert workspace._compact_layout is False  # noqa: SLF001
    assert workspace.job_table._compact_mode is True  # noqa: SLF001
    assert workspace.output_directory_button.y() == workspace.settings_button.y()
    assert workspace.add_button.y() == workspace.settings_button.y()
    assert workspace.primary_button.y() == workspace.settings_button.y()


def test_internal_view_hides_unrelated_global_primary_action(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    workspace.set_jobs((make_job("one", 0),))
    workspace.show()
    qtbot.waitExposed(workspace)
    assert workspace.primary_button.isVisible()

    internal = QLabel("Configuración")
    workspace.show_internal_view(internal, "Configurar")
    assert workspace.primary_button.isHidden()

    workspace.close_internal_view(internal)
    assert workspace.primary_button.isVisible()


def test_internal_activity_view_uses_vertical_scroll_without_horizontal_overflow(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    activity = ActivityView(())

    workspace.show_internal_view(activity, "Actividad reciente", scroll=True)

    page = workspace._internal_pages[activity][0]  # noqa: SLF001
    viewport = page.findChild(QScrollArea)
    assert viewport is not None
    assert viewport.horizontalScrollBarPolicy() is Qt.ScrollBarPolicy.ScrollBarAlwaysOff
