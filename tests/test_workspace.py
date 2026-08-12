from dataclasses import replace
from pathlib import Path

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
from parsezen.presentation.design_system import COLORS
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


def test_document_drop_area_uses_a_constant_two_pixel_primary_border(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)

    stylesheet = workspace.styleSheet()

    assert stylesheet.count(f"border: 2px dashed {COLORS.action_primary};") >= 2
    assert "border: 1px dashed" not in stylesheet


def test_workspace_header_prioritizes_real_reviews(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    workspace.set_jobs((make_review_job("one"),))

    assert workspace.primary_button.text() == "Revisar 1 pendiente"
    assert "1 requiere atención" in workspace.queue_summary.text()


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

    assert workspace.queue_summary.text() == "1 documento · aprox. 2 min–5 min"
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
        .startswith("Siguiente paso · Preparando")
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
    workspace.local_ai_button.setFocus()
    editor = QPushButton("Modelo específico")
    workspace.show_internal_view(editor, "Editor")
    editor.setFocus()

    manager = QLabel("Modelos")
    workspace.show_internal_view(manager, "IA local")
    workspace.close_internal_view(manager)

    qtbot.waitUntil(editor.hasFocus)
    workspace.close_internal_view(editor)
    qtbot.waitUntil(workspace.local_ai_button.hasFocus)


def test_workspace_local_ai_view_can_replace_the_global_header(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    manager = QLabel("Modelos")

    workspace.show_internal_view(manager, "IA local", replace_app_header=True)
    assert workspace.app_header.isHidden()

    workspace.close_internal_view(manager)
    assert not workspace.app_header.isHidden()


def test_workspace_exposes_local_ai_readiness_without_requiring_the_manager(
    qtbot,
) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)

    workspace.set_local_ai_status(OllamaStatus.READY, "qwen3:4b-instruct")

    assert workspace.local_ai_button.text() == "IA local · Lista"
    assert "qwen3:4b-instruct" in workspace.local_ai_button.toolTip()
    assert "Estado: Lista" in workspace.local_ai_button.accessibleName()


def test_workspace_exposes_the_global_destination_compactly(qtbot, tmp_path: Path) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)

    workspace.set_output_directory(tmp_path / "Resultados")

    assert workspace.output_directory_button.text() == "Destino: Resultados"
    assert str(tmp_path / "Resultados") == workspace.output_directory_button.toolTip()


def test_add_area_follows_visible_queue_rows_and_exposes_drop_action(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    workspace.set_jobs((make_job("one", 0), make_job("two", 1)))
    queue_layout = workspace.queue_pane.layout()

    assert queue_layout.indexOf(workspace.drop_area) == 1
    assert workspace.drop_area.secondary_label.text() == "TXT · MD · DOCX · PDF · EPUB"
    assert workspace.job_table.height() < 300
    assert workspace.drop_area.height() == 76
    assert "examínalos" in workspace.drop_area.primary_label.text()


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


def test_workspace_uses_the_full_width_without_a_redundant_inspector(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    workspace.resize(1380, 800)
    workspace.set_jobs((make_job("one", 0), make_job("two", 1)))
    workspace.show()
    qtbot.waitExposed(workspace)

    assert not hasattr(workspace, "inspector")
    assert workspace.queue_pane.layout().indexOf(workspace.table_panel) == 0
    assert workspace.table_panel.layout().indexOf(workspace.job_table) == 0
    assert workspace.table_panel._outline.geometry() == workspace.table_panel.rect()  # noqa: SLF001
    assert workspace.table_panel._outline.testAttribute(  # noqa: SLF001
        Qt.WidgetAttribute.WA_TransparentForMouseEvents
    )


def test_workspace_header_exposes_ai_and_theme_actions_without_trust_slogans(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    actions: list[str] = []
    workspace.local_ai_requested.connect(lambda: actions.append("ai"))
    workspace.theme_toggle_requested.connect(lambda: actions.append("theme"))

    workspace.local_ai_button.click()
    workspace.theme_button.click()

    assert actions == ["ai", "theme"]
    assert workspace.local_ai_button.focusPolicy() is Qt.FocusPolicy.TabFocus
    assert workspace.output_directory_button.focusPolicy() is Qt.FocusPolicy.TabFocus
    assert workspace.theme_button.focusPolicy() is Qt.FocusPolicy.TabFocus
    assert "Procesamiento local" not in tuple(
        label.text() for label in workspace.findChildren(QLabel)
    )
    assert not hasattr(workspace, "settings_button")


def test_workspace_reflows_at_320_without_horizontal_overflow(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    workspace.resize(320, 720)
    workspace.set_jobs((make_job("one", 0),))
    workspace.show()
    qtbot.waitExposed(workspace)

    assert workspace.width() == 320
    assert workspace._compact_layout is True  # noqa: SLF001
    assert workspace.local_ai_button.y() > workspace.logo.y()
    assert workspace.primary_button.minimumWidth() == 0
    assert workspace.job_table.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert workspace.drop_area.primary_label.width() > 0
    assert workspace.drop_area.secondary_label.width() > 0


def test_internal_activity_view_uses_vertical_scroll_without_horizontal_overflow(qtbot) -> None:
    workspace = ParsezenWorkspace()
    qtbot.addWidget(workspace)
    activity = ActivityView(())

    workspace.show_internal_view(activity, "Actividad reciente", scroll=True)

    page = workspace._internal_pages[activity][0]  # noqa: SLF001
    viewport = page.findChild(QScrollArea)
    assert viewport is not None
    assert viewport.horizontalScrollBarPolicy() is Qt.ScrollBarPolicy.ScrollBarAlwaysOff
