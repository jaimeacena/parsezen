from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QMimeData, QPointF, QRect, Qt
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QStyleOptionViewItem

import parsezen.presentation.job_table as job_table_module
from parsezen.application.planner import activate_next_stage
from parsezen.application.preflight import DocumentPreflight, RuntimeEstimate
from parsezen.domain.estimates import DurationEstimate
from parsezen.domain.jobs import (
    DocumentFormat,
    DocumentJob,
    DocumentSource,
    JobConfiguration,
    OutputConfiguration,
    ProcessingPlan,
    ReviewRecommendation,
    ReviewSignal,
    TranslationConfiguration,
)
from parsezen.domain.stages import StageKind, StageStatus
from parsezen.presentation.job_table import (
    CELL_PRESENTATION_ROLE,
    COLUMNS,
    CONFIGURABLE_ROLE,
    CONFIGURING_ROLE,
    JOB_RUNNING_ROLE,
    REMOVE_AVAILABLE_ROLE,
    STAGE_KIND_ROLE,
    JobCellDelegate,
    JobColumn,
    JobHeaderView,
    JobTableModel,
    JobTableView,
)


class FakeMenu:
    selected_text = ""

    def __init__(self, _parent=None) -> None:
        self._actions: list[FakeAction] = []

    def addAction(self, text: str):
        action = FakeAction(text)
        self._actions.append(action)
        return action

    def addSeparator(self) -> None:
        self._actions.append(FakeAction(""))

    def actions(self):
        return self._actions

    def exec(self, _position):
        return next(action for action in self._actions if action.text() == self.selected_text)


class FakeAction:
    def __init__(self, text: str) -> None:
        self._text = text

    def text(self) -> str:
        return self._text


def make_job() -> DocumentJob:
    return DocumentJob.create(
        DocumentSource(Path("manual.pdf"), DocumentFormat.PDF, 2_400_000, 1),
        JobConfiguration(
            output=OutputConfiguration(format=DocumentFormat.EPUB),
            translation=TranslationConfiguration(enabled=True, target_language="Español"),
            plan=ProcessingPlan.LOCAL_AI_REVIEWED,
        ),
        order=0,
        job_id="manual",
    )


def make_completed_job() -> DocumentJob:
    job = make_job()
    stages = tuple(
        replace(stage, status=StageStatus.COMPLETED) if stage.participates else stage
        for stage in job.stages
    )
    return replace(job, stages=stages, result_path=Path("manual.epub"))


def make_review_job() -> DocumentJob:
    job = make_job()
    stages = []
    for stage in job.stages:
        if stage.kind in {StageKind.PREPARE, StageKind.TRANSLATE}:
            stages.append(replace(stage, status=StageStatus.COMPLETED))
        elif stage.kind is StageKind.REFINE:
            stages.append(
                replace(
                    stage,
                    status=StageStatus.BLOCKED_FOR_REVIEW,
                    review_id="review",
                )
            )
        else:
            stages.append(stage)
    return replace(job, stages=tuple(stages))


def test_table_uses_four_semantic_columns_and_two_utility_tracks() -> None:
    assert COLUMNS == (
        JobColumn.DRAG,
        JobColumn.DOCUMENT,
        JobColumn.FLOW,
        JobColumn.RESULT,
        JobColumn.NEXT_STEP,
        JobColumn.REMOVE,
    )


def test_running_progress_lives_in_status_without_replacing_document_data() -> None:
    job = activate_next_stage(make_job())
    running = job.replace_stage(
        job.stage(StageKind.PREPARE)
        .transition(StageStatus.RUNNING)
        .with_progress(63, 100, "Extrayendo")
    )
    model = JobTableModel((running,))

    document = model.index(0, COLUMNS.index(JobColumn.DOCUMENT)).data(CELL_PRESENTATION_ROLE)
    status = model.index(0, COLUMNS.index(JobColumn.STATUS)).data(CELL_PRESENTATION_ROLE)

    assert document.title == "manual"
    assert document.subtitle == "PDF · 2,3 MB"
    assert document.status is None
    assert status.title == "Preparando · 63 %"
    assert status.subtitle is None
    assert status.progress == 0.63


def test_running_progress_explains_the_remaining_time() -> None:
    job = activate_next_stage(make_job())
    running = job.replace_stage(
        job.stage(StageKind.PREPARE)
        .transition(StageStatus.RUNNING)
        .with_progress(25, 100, "Extrayendo")
    )
    model = JobTableModel((running,))
    model.set_runtime_estimates(
        {
            "manual": RuntimeEstimate(
                "Quedan aprox. 12 min–20 min",
                "Actualizada con el avance real de la etapa actual.",
            )
        }
    )
    status = model.index(0, COLUMNS.index(JobColumn.NEXT_STEP))

    assert status.data(CELL_PRESENTATION_ROLE).subtitle == "Quedan aprox. 12 min–20 min"
    assert "avance real" in status.data(Qt.ItemDataRole.ToolTipRole)


def test_running_progress_track_sits_below_remaining_time_inside_the_row() -> None:
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 260, JobCellDelegate.ROW_HEIGHT)
    presentation = job_table_module.CellPresentation(
        "Traduciendo · 25 %",
        "Quedan aprox. 12 min–20 min",
        tone="running",
        progress=0.25,
    )

    track = JobCellDelegate._progress_track_rect(option, presentation)

    line_bottom = option.rect.center().y() - 20 + 40
    assert track.top() - line_bottom >= job_table_module.SPACING.xs
    assert track.bottom() <= option.rect.bottom()


def test_configuration_summary_remains_visible_and_accessible() -> None:
    model = JobTableModel((make_job(),))
    index = model.index(0, COLUMNS.index(JobColumn.CONFIGURATION))
    presentation = index.data(CELL_PRESENTATION_ROLE)

    assert presentation.title == ""
    assert presentation.operations == ("Traducir", "Revisar con IA")
    assert index.data(Qt.ItemDataRole.AccessibleTextRole).startswith("Flujo")


def test_execution_preflight_is_presented_as_preparing() -> None:
    model = JobTableModel((make_job(),))
    model.set_preparing_jobs(
        ("manual",),
        {"manual": "Comprobando 2 de 3 páginas representativas"},
    )
    status = model.index(0, COLUMNS.index(JobColumn.NEXT_STEP))
    remove = model.index(0, COLUMNS.index(JobColumn.REMOVE))
    flow = model.index(0, COLUMNS.index(JobColumn.FLOW))

    assert status.data(CELL_PRESENTATION_ROLE).title == "Preparando"
    assert (
        status.data(CELL_PRESENTATION_ROLE).subtitle == "Comprobando 2 de 3 páginas representativas"
    )
    assert status.data(CELL_PRESENTATION_ROLE).tone == "running"
    assert status.data(STAGE_KIND_ROLE) == StageKind.PREPARE
    assert status.data(JOB_RUNNING_ROLE)
    assert not remove.data(REMOVE_AVAILABLE_ROLE)
    assert not flow.data(CONFIGURABLE_ROLE)


def test_ready_job_exposes_automatic_time_without_hiding_its_next_step() -> None:
    model = JobTableModel((make_job(),))
    model.set_forecasts(
        {
            "manual": DocumentPreflight(
                "manual",
                "20 páginas",
                DurationEstimate(120, 180, 300, 4),
                ("traducción",),
                (),
            )
        }
    )
    status = model.index(0, COLUMNS.index(JobColumn.NEXT_STEP))

    presentation = status.data(CELL_PRESENTATION_ROLE)
    assert presentation.title == "En cola"
    assert presentation.subtitle == "Tiempo automático aprox. 2 min–5 min"
    assert "Basada en trabajos similares" in status.data(Qt.ItemDataRole.ToolTipRole)
    assert "Tiempo automático" in status.data(Qt.ItemDataRole.AccessibleTextRole)


def test_completed_result_exposes_open_action() -> None:
    model = JobTableModel((make_completed_job(),))
    index = model.index(0, COLUMNS.index(JobColumn.NEXT_STEP))
    presentation = index.data(CELL_PRESENTATION_ROLE)

    assert presentation.action == "Abrir resultado"
    assert presentation.title == "Listo"
    assert "Estado · Listo" in index.data(Qt.ItemDataRole.AccessibleTextRole)


def test_completed_result_with_evidence_offers_targeted_ai_review(qtbot) -> None:
    recommended = replace(
        make_completed_job(),
        review_recommendation=ReviewRecommendation(
            ((ReviewSignal.SOURCE_TEXT_RESIDUE, 1),),
            (2,),
        ),
    )
    table = JobTableView()
    qtbot.addWidget(table)
    table.set_jobs((recommended,))
    requested: list[str] = []
    table.ai_review_requested.connect(requested.append)
    index = table.job_model.index(0, COLUMNS.index(JobColumn.NEXT_STEP))

    presentation = index.data(CELL_PRESENTATION_ROLE)
    table._cell_clicked(index)

    assert presentation.title == "Revisión sugerida"
    assert presentation.action == "Revisar con IA"
    assert presentation.subtitle == "1 bloque señalado · el resultado ya está disponible"
    assert requested == ["manual"]


def test_unconfigured_job_has_one_clear_configuration_gate() -> None:
    job = DocumentJob.create(
        DocumentSource(Path("draft.pdf"), DocumentFormat.PDF, 100, 1),
        JobConfiguration(
            output=OutputConfiguration(
                configured=False,
                format=DocumentFormat.MARKDOWN,
            )
        ),
        order=0,
    )
    model = JobTableModel((job,))
    configuration = model.index(0, COLUMNS.index(JobColumn.CONFIGURATION)).data(
        CELL_PRESENTATION_ROLE
    )
    status = model.index(0, COLUMNS.index(JobColumn.STATUS)).data(CELL_PRESENTATION_ROLE)

    assert configuration.title == ""
    assert configuration.action is None
    assert configuration.tone == "disabled"
    result = model.index(0, COLUMNS.index(JobColumn.RESULT)).data(CELL_PRESENTATION_ROLE)
    assert result.title == "Sin salida"
    assert result.action is None
    assert result.tone == "disabled"
    assert status.title == "Pendiente"
    assert status.action == "Configurar"


def test_configuration_click_opens_the_complete_editor(qtbot) -> None:
    table = JobTableView()
    qtbot.addWidget(table)
    table.set_jobs((make_job(),))
    captured: list[tuple[str, object]] = []
    table.configure_requested.connect(lambda job_id, stage: captured.append((job_id, stage)))

    index = table.job_model.index(0, COLUMNS.index(JobColumn.CONFIGURATION))
    table._cell_clicked(index)

    assert captured == [("manual", None)]


def test_table_marks_the_document_being_configured() -> None:
    model = JobTableModel((make_job(),))
    model.set_configuring("manual", None)

    configuration = model.index(0, COLUMNS.index(JobColumn.CONFIGURATION))
    status = model.index(0, COLUMNS.index(JobColumn.STATUS))
    assert configuration.data(CONFIGURING_ROLE) is True
    assert status.data(CONFIGURING_ROLE) is False


def test_failed_phase_exposes_exact_error_in_status(qtbot) -> None:
    job = make_job()
    failed = replace(
        job,
        stages=tuple(
            replace(
                stage,
                status=StageStatus.FAILED,
                error_message="Ollama no respondió.",
            )
            if stage.kind is StageKind.TRANSLATE
            else stage
            for stage in job.stages
        ),
    )
    table = JobTableView()
    qtbot.addWidget(table)
    table.set_jobs((failed,))
    captured: list[tuple[str, object]] = []
    table.error_requested.connect(lambda job_id, stage: captured.append((job_id, stage)))
    index = table.job_model.index(0, COLUMNS.index(JobColumn.STATUS))

    presentation = index.data(CELL_PRESENTATION_ROLE)
    table._cell_clicked(index)

    assert presentation.action == "Ver error"
    assert "Ollama no respondió" in index.data(Qt.ItemDataRole.ToolTipRole)
    assert captured == [("manual", StageKind.TRANSLATE)]


def test_completed_result_click_opens_its_document(qtbot) -> None:
    table = JobTableView()
    qtbot.addWidget(table)
    table.set_jobs((make_completed_job(),))
    captured: list[str] = []
    table.open_result_requested.connect(captured.append)

    table._cell_clicked(table.job_model.index(0, COLUMNS.index(JobColumn.NEXT_STEP)))

    assert captured == ["manual"]


def test_completed_result_context_menu_routes_file_folder_and_remove(
    qtbot,
    monkeypatch,
) -> None:
    table = JobTableView()
    qtbot.addWidget(table)
    table.resize(1200, 300)
    table.set_jobs((make_completed_job(),))
    table.show()
    qtbot.waitExposed(table)
    index = table.job_model.index(0, COLUMNS.index(JobColumn.RESULT))
    position = table.visualRect(index).center()
    captured: list[tuple[str, str]] = []
    table.open_result_requested.connect(lambda job_id: captured.append(("result", job_id)))
    table.open_folder_requested.connect(lambda job_id: captured.append(("folder", job_id)))
    table.result_summary_requested.connect(lambda job_id: captured.append(("summary", job_id)))
    table.remove_requested.connect(lambda job_id: captured.append(("remove", job_id)))
    monkeypatch.setattr(job_table_module, "QMenu", FakeMenu)
    for action_text in (
        "Abrir resultado",
        "Abrir carpeta",
        "Ver resumen",
        "Quitar de la cola",
    ):
        FakeMenu.selected_text = action_text
        table._show_context_menu(position)

    assert captured == [
        ("result", "manual"),
        ("folder", "manual"),
        ("summary", "manual"),
        ("remove", "manual"),
    ]


def test_review_context_menu_routes_the_blocking_phase(qtbot, monkeypatch) -> None:
    table = JobTableView()
    qtbot.addWidget(table)
    table.resize(1200, 300)
    table.set_jobs((make_review_job(),))
    table.show()
    qtbot.waitExposed(table)
    index = table.job_model.index(0, COLUMNS.index(JobColumn.STATUS))
    captured: list[tuple[str, object]] = []
    table.review_requested.connect(lambda job_id, stage: captured.append((job_id, stage)))

    FakeMenu.selected_text = "Revisar"
    monkeypatch.setattr(job_table_module, "QMenu", FakeMenu)
    table._show_context_menu(table.visualRect(index).center())

    assert captured == [("manual", StageKind.REFINE)]


def test_table_renders_all_state_cells(qtbot) -> None:
    running = activate_next_stage(make_job())
    running = running.replace_stage(
        running.stage(StageKind.PREPARE).transition(StageStatus.RUNNING).with_progress(63, 100)
    )
    table = JobTableView()
    qtbot.addWidget(table)
    table.resize(1300, 520)
    table.set_jobs((running, make_review_job()))
    table.show()
    qtbot.waitExposed(table)

    image = table.grab().toImage()

    assert not image.isNull()
    assert image.width() == 1300
    assert 80 <= table.sizeHintForRow(0) <= 96
    assert table.horizontalHeader().count() == 6
    assert table.horizontalHeader().height() == JobHeaderView.HEIGHT
    assert 9 <= JobHeaderView.FONT_SIZE <= 11
    assert table.mask().isEmpty()


def test_table_height_keeps_every_row_visible_before_the_six_row_limit(qtbot) -> None:
    table = JobTableView()
    qtbot.addWidget(table)
    jobs = tuple(replace(make_job(), id=f"job-{index}", order=index) for index in range(4))
    table.resize(1300, 600)
    table.set_jobs(jobs)
    table.show()
    qtbot.waitExposed(table)

    assert table.height() == (
        JobHeaderView.HEIGHT + 4 * table.rowHeight(0) + table.frameWidth() * 2
    )
    assert table.verticalScrollBar().maximum() == 0


def test_drag_reordering_uses_the_row_where_the_gesture_started(qtbot) -> None:
    table = JobTableView()
    qtbot.addWidget(table)
    jobs = tuple(replace(make_job(), id=f"job-{index}", order=index) for index in range(3))
    table.resize(1200, 520)
    table.set_jobs(jobs)
    table.show()
    qtbot.waitExposed(table)
    captured: list[tuple[str, int]] = []
    table.move_requested.connect(lambda job_id, row: captured.append((job_id, row)))
    table._drag_source_row = 0
    target = table.visualRect(table.job_model.index(2, 0)).center()
    event = QDropEvent(
        QPointF(target),
        Qt.DropAction.MoveAction,
        QMimeData(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )

    table.dropEvent(event)

    assert captured == [("job-0", 1)]


def test_row_trash_removes_the_exact_document(qtbot) -> None:
    table = JobTableView()
    qtbot.addWidget(table)
    table.resize(1200, 300)
    table.set_jobs((make_job(),))
    table.show()
    qtbot.waitExposed(table)
    captured: list[str] = []
    table.remove_requested.connect(captured.append)
    remove_index = table.job_model.index(0, COLUMNS.index(JobColumn.REMOVE))
    trash_position = table.visualRect(remove_index).center()

    qtbot.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=trash_position)

    assert captured == ["manual"]


def test_paused_job_keeps_remove_but_not_configuration_available() -> None:
    job = activate_next_stage(make_job())
    paused = job.replace_stage(
        job.stage(StageKind.PREPARE).transition(StageStatus.RUNNING).transition(StageStatus.PAUSED)
    )
    model = JobTableModel((paused,))
    remove = model.index(0, COLUMNS.index(JobColumn.REMOVE))
    configuration = model.index(0, COLUMNS.index(JobColumn.CONFIGURATION))

    assert remove.data(REMOVE_AVAILABLE_ROLE) is True
    assert configuration.data(CONFIGURABLE_ROLE) is False


def test_review_pending_job_can_be_removed() -> None:
    model = JobTableModel((make_review_job(),))
    remove = model.index(0, COLUMNS.index(JobColumn.REMOVE))

    assert remove.data(REMOVE_AVAILABLE_ROLE) is True


def test_configurable_cell_uses_a_pointing_cursor_on_hover(qtbot) -> None:
    table = JobTableView()
    qtbot.addWidget(table)
    table.resize(1200, 300)
    table.set_jobs((make_job(),))
    table.show()
    qtbot.waitExposed(table)
    index = table.job_model.index(0, COLUMNS.index(JobColumn.CONFIGURATION))

    qtbot.mouseMove(table.viewport(), pos=table.visualRect(index).center())

    assert table.viewport().cursor().shape() is Qt.CursorShape.PointingHandCursor
    assert table.hovered_row == 0


def test_compact_table_preserves_flow_and_actions_without_scrolling(qtbot) -> None:
    table = JobTableView()
    qtbot.addWidget(table)
    table.resize(300, 260)
    table.set_jobs((make_job(),))
    table.set_compact_mode(True)
    table.show()
    qtbot.waitExposed(table)

    assert table.isColumnHidden(COLUMNS.index(JobColumn.FLOW))
    assert table.isColumnHidden(COLUMNS.index(JobColumn.RESULT))
    document = table.job_model.index(0, COLUMNS.index(JobColumn.DOCUMENT))
    assert "Traducir" in str(document.data(Qt.ItemDataRole.AccessibleTextRole))
    assert document.data(CONFIGURABLE_ROLE) is True
    assert table.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff


def test_wide_table_prioritizes_document_and_names_the_live_state(qtbot) -> None:
    table = JobTableView()
    qtbot.addWidget(table)
    table.resize(1280, 300)
    table.set_jobs((make_job(),))
    table.show()
    qtbot.waitExposed(table)

    semantic_columns = (
        JobColumn.DOCUMENT,
        JobColumn.FLOW,
        JobColumn.RESULT,
        JobColumn.NEXT_STEP,
    )
    widths = {column: table.columnWidth(COLUMNS.index(column)) for column in semantic_columns}
    semantic_width = sum(widths.values())

    assert (
        table.job_model.headerData(
            COLUMNS.index(JobColumn.NEXT_STEP),
            Qt.Orientation.Horizontal,
        )
        == "Estado"
    )
    assert abs(widths[JobColumn.DOCUMENT] / semantic_width - 0.33) < 0.02
    assert abs(widths[JobColumn.FLOW] / semantic_width - 0.29) < 0.02
    assert abs(widths[JobColumn.RESULT] / semantic_width - 0.16) < 0.02
    assert abs(widths[JobColumn.NEXT_STEP] / semantic_width - 0.22) < 0.02
    assert table.horizontalHeader().height() == 44
    assert table.rowHeight(0) == 80
