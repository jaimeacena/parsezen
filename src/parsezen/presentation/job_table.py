"""Compact, state-driven table for independent document jobs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from PySide6.QtCore import (
    QAbstractTableModel,
    QEvent,
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QDropEvent,
    QFont,
    QFontMetrics,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHeaderView,
    QMenu,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QWidget,
)

from parsezen.application.preflight import (
    DocumentPreflight,
    RuntimeEstimate,
    format_duration_range,
)
from parsezen.application.processing_explanation import ProcessingFlow, processing_flow
from parsezen.domain.jobs import DocumentFormat, DocumentJob, JobStatus
from parsezen.domain.stages import StageKind
from parsezen.final_integrity import FinalIntegrityReport
from parsezen.presentation.design_system import COLORS, SPACING
from parsezen.presentation.job_view_model import focus_stage, next_step_view

CELL_PRESENTATION_ROLE = Qt.ItemDataRole.UserRole + 1
JOB_ID_ROLE = Qt.ItemDataRole.UserRole + 2
STAGE_KIND_ROLE = Qt.ItemDataRole.UserRole + 3
REMOVE_AVAILABLE_ROLE = Qt.ItemDataRole.UserRole + 4
CONFIGURABLE_ROLE = Qt.ItemDataRole.UserRole + 5
CONFIGURING_ROLE = Qt.ItemDataRole.UserRole + 6
JOB_RUNNING_ROLE = Qt.ItemDataRole.UserRole + 7
_INVALID_INDEX = QModelIndex()
ModelIndex = QModelIndex | QPersistentModelIndex


class JobColumn(StrEnum):
    DRAG = "drag"
    DOCUMENT = "document"
    FLOW = "flow"
    # Compatibility alias for integrations created before the compact flow column.
    CONFIGURATION = "flow"
    RESULT = "result"
    NEXT_STEP = "next_step"
    STATUS = "next_step"
    REMOVE = "remove"


COLUMNS = (
    JobColumn.DRAG,
    JobColumn.DOCUMENT,
    JobColumn.FLOW,
    JobColumn.RESULT,
    JobColumn.NEXT_STEP,
    JobColumn.REMOVE,
)
HEADERS = {
    JobColumn.DRAG: "",
    JobColumn.DOCUMENT: "Documento",
    JobColumn.FLOW: "Flujo",
    JobColumn.RESULT: "Salida",
    JobColumn.NEXT_STEP: "Estado",
    JobColumn.REMOVE: "",
}


@dataclass(frozen=True, slots=True)
class CellPresentation:
    title: str
    subtitle: str | None = None
    status: str | None = None
    tone: str = "pending"
    progress: float | None = None
    action: str | None = None
    document_format: DocumentFormat | None = None
    operations: tuple[str, ...] = ()


def _formatted_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB".replace(".", ",")


_STAGE_LABELS = {
    StageKind.PREPARE: "Preparación",
    StageKind.TRANSLATE: "Traducción",
    StageKind.REFINE: "Corrección",
    StageKind.STRUCTURE: "Estructura",
    StageKind.PUBLISH: "Resultado",
}


def _flow(job: DocumentJob) -> ProcessingFlow:
    return processing_flow(job.source.format, job.configuration)


def cell_presentation(
    job: DocumentJob,
    column: JobColumn,
    *,
    preparing: bool = False,
    preparation_detail: str | None = None,
    compact: bool = False,
    forecast: DocumentPreflight | None = None,
    runtime_estimate: RuntimeEstimate | None = None,
    integrity_report: FinalIntegrityReport | None = None,
) -> CellPresentation:
    if column in {JobColumn.DRAG, JobColumn.REMOVE}:
        return CellPresentation("")

    if column is JobColumn.DOCUMENT:
        pages = job.configuration.page_range
        page_text = f"Páginas {pages.first_page}–{pages.last_page}" if pages is not None else None
        details = (
            "Configura la salida para continuar"
            if not job.is_configured
            else " · ".join(
                value
                for value in (
                    job.source.format.value.upper(),
                    page_text,
                    _formatted_size(job.source.size_bytes),
                )
                if value
            )
        )
        if compact and job.is_configured:
            output = job.configuration.output
            route = _flow(job)
            flow = " → ".join(route.compact_steps)
            output_label = output.format.value.upper() if output.configured else "Sin salida"
            details = f"{details} · {flow} → {output_label}"
            if route.human_review_note:
                details = f"{details} · {route.human_review_note}"
        return CellPresentation(
            job.source.path.stem,
            details,
            document_format=job.source.format,
        )

    if column is JobColumn.FLOW:
        if not job.is_configured:
            return CellPresentation("", tone="disabled")
        route = _flow(job)
        return CellPresentation(
            "",
            route.human_review_note,
            operations=route.compact_steps,
        )

    if column is JobColumn.NEXT_STEP:
        if preparing:
            return CellPresentation("Preparando", preparation_detail, tone="running")
        next_step = next_step_view(job)
        estimate = (
            f"Tiempo automático aprox. {format_duration_range(forecast.estimate)}"
            if forecast is not None and next_step.tone == "pending" and job.is_configured
            else None
        )
        if next_step.tone == "running" and runtime_estimate is not None:
            estimate = runtime_estimate.label
        if next_step.tone == "completed" and integrity_report is not None:
            estimate = (
                "Integridad final comprobada"
                if integrity_report.verified
                else "Control final no disponible"
            )
        if job.status is JobStatus.COMPLETED and job.review_recommendation is not None:
            block_count = len(job.review_recommendation.block_positions)
            estimate = (
                f"{block_count} {'bloque señalado' if block_count == 1 else 'bloques señalados'} "
                "· el resultado ya está disponible"
            )
        return CellPresentation(
            next_step.label,
            estimate,
            tone=next_step.tone,
            progress=next_step.progress,
            action=next_step.action_label,
        )

    output = job.configuration.output
    if not output.configured:
        return CellPresentation("Sin salida", tone="disabled")
    subtitle = (
        "Junto al original"
        if output.directory is None
        else output.directory.name or str(output.directory)
    )
    return CellPresentation(
        output.format.value.upper(),
        subtitle,
    )


class JobTableModel(QAbstractTableModel):
    def __init__(
        self,
        jobs: tuple[DocumentJob, ...] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._jobs = tuple(sorted(jobs, key=lambda job: job.order))
        self._configuring: str | None = None
        self._preparing_job_ids: frozenset[str] = frozenset()
        self._preparing_details: dict[str, str] = {}
        self._forecasts: dict[str, DocumentPreflight] = {}
        self._runtime_estimates: dict[str, RuntimeEstimate] = {}
        self._integrity_reports: dict[str, FinalIntegrityReport] = {}
        self._compact = False

    @property
    def jobs(self) -> tuple[DocumentJob, ...]:
        return self._jobs

    def set_jobs(self, jobs: tuple[DocumentJob, ...]) -> None:
        self.beginResetModel()
        self._jobs = tuple(sorted(jobs, key=lambda job: job.order))
        self.endResetModel()

    def set_compact_mode(self, compact: bool) -> None:
        if compact == self._compact:
            return
        self.beginResetModel()
        self._compact = compact
        self.endResetModel()

    def set_configuring(self, job_id: str | None, stage: StageKind | None = None) -> None:
        del stage
        self._configuring = job_id
        if self._jobs:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._jobs) - 1, len(COLUMNS) - 1),
                [CONFIGURING_ROLE],
            )

    def set_preparing_jobs(
        self,
        job_ids: tuple[str, ...],
        details: dict[str, str] | None = None,
    ) -> None:
        preparing = frozenset(job_ids)
        normalized_details = {
            job_id: detail
            for job_id, detail in (details or {}).items()
            if job_id in preparing and detail
        }
        if preparing == self._preparing_job_ids and normalized_details == self._preparing_details:
            return
        self._preparing_job_ids = preparing
        self._preparing_details = normalized_details
        if self._jobs:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._jobs) - 1, len(COLUMNS) - 1),
            )

    def set_forecasts(self, forecasts: dict[str, DocumentPreflight]) -> None:
        normalized = dict(forecasts)
        if normalized == self._forecasts:
            return
        self._forecasts = normalized
        if self._jobs:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._jobs) - 1, len(COLUMNS) - 1),
            )

    def set_runtime_estimates(self, estimates: dict[str, RuntimeEstimate]) -> None:
        normalized = dict(estimates)
        if normalized == self._runtime_estimates:
            return
        self._runtime_estimates = normalized
        if self._jobs:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._jobs) - 1, len(COLUMNS) - 1),
            )

    def set_integrity_reports(
        self,
        reports: dict[str, FinalIntegrityReport],
    ) -> None:
        normalized = dict(reports)
        if normalized == self._integrity_reports:
            return
        self._integrity_reports = normalized
        if self._jobs:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._jobs) - 1, len(COLUMNS) - 1),
            )

    def rowCount(self, _parent: ModelIndex = _INVALID_INDEX) -> int:  # noqa: N802
        return len(self._jobs)

    def columnCount(self, _parent: ModelIndex = _INVALID_INDEX) -> int:  # noqa: N802
        return len(COLUMNS)

    def data(self, index: ModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._jobs):
            return None
        job = self._jobs[index.row()]
        column = COLUMNS[index.column()]
        preparing = job.id in self._preparing_job_ids
        preparation_detail = self._preparing_details.get(job.id)
        forecast = self._forecasts.get(job.id)
        runtime_estimate = self._runtime_estimates.get(job.id)
        integrity_report = self._integrity_reports.get(job.id)
        if role == CELL_PRESENTATION_ROLE:
            return cell_presentation(
                job,
                column,
                preparing=preparing,
                preparation_detail=preparation_detail,
                compact=self._compact,
                forecast=forecast,
                runtime_estimate=runtime_estimate,
                integrity_report=integrity_report,
            )
        if role == JOB_ID_ROLE:
            return job.id
        if role == STAGE_KIND_ROLE:
            if column is JobColumn.NEXT_STEP and preparing:
                return StageKind.PREPARE
            if column is JobColumn.NEXT_STEP and job.is_configured:
                return focus_stage(job).kind
            if column is JobColumn.RESULT:
                return StageKind.PUBLISH
            return None
        if role == REMOVE_AVAILABLE_ROLE:
            return (
                column is JobColumn.REMOVE and not preparing and job.status is not JobStatus.RUNNING
            )
        if role == CONFIGURABLE_ROLE:
            configurable_column = (
                column in {JobColumn.DOCUMENT, JobColumn.RESULT}
                if not job.is_configured
                else column in {JobColumn.DOCUMENT, JobColumn.FLOW, JobColumn.RESULT}
            )
            return (
                not preparing
                and configurable_column
                and job.status
                in {
                    JobStatus.QUEUED,
                    JobStatus.FAILED,
                    JobStatus.CANCELLED,
                }
            )
        if role == CONFIGURING_ROLE:
            return self._configuring == job.id and column in {
                JobColumn.FLOW,
                JobColumn.RESULT,
            }
        if role == JOB_RUNNING_ROLE:
            return preparing or job.status is JobStatus.RUNNING
        if role == Qt.ItemDataRole.ToolTipRole:
            if column is JobColumn.DRAG:
                return f"Arrastrar para reordenar {job.source.path.name}"
            if column is JobColumn.REMOVE:
                return f"Quitar {job.source.path.name} de la cola"
            stage_kind = self.data(index, STAGE_KIND_ROLE)
            if isinstance(stage_kind, StageKind):
                stage = job.stage(stage_kind)
                if stage.error_message:
                    return f"Error en {_STAGE_LABELS[stage_kind]}: {stage.error_message}"
            if not job.is_configured and column in {JobColumn.NEXT_STEP, JobColumn.RESULT}:
                return "Configura el documento primero."
            presentation = cell_presentation(
                job,
                column,
                preparing=preparing,
                preparation_detail=preparation_detail,
                compact=self._compact,
                forecast=forecast,
                runtime_estimate=runtime_estimate,
                integrity_report=integrity_report,
            )
            if column is JobColumn.DOCUMENT:
                return f"{job.source.path.name}\n{job.source.path.parent}"
            if presentation.operations:
                route = _flow(job)
                return "\n".join(
                    value
                    for value in (
                        " → ".join(route.detailed_steps),
                        route.human_review_note,
                    )
                    if value
                )
            if column is JobColumn.NEXT_STEP and runtime_estimate is not None:
                return "\n".join(
                    (
                        presentation.title,
                        presentation.subtitle or "",
                        runtime_estimate.explanation,
                    )
                )
            if column is JobColumn.NEXT_STEP and forecast is not None:
                details = [
                    presentation.title,
                    presentation.subtitle,
                    forecast.estimate.confidence_label,
                ]
                details.extend(
                    f"{finding.title}: {finding.detail}" for finding in forecast.findings
                )
                return "\n".join(value for value in details if value)
            if column is JobColumn.NEXT_STEP and integrity_report is not None:
                ledger = integrity_report.ledger
                inventory = (
                    f"{ledger.blocks} bloques · {ledger.headings} encabezados · "
                    f"{ledger.images} imágenes · {ledger.resources} recursos"
                )
                return "\n".join(
                    (
                        presentation.title,
                        presentation.subtitle or "",
                        *integrity_report.checks,
                        inventory,
                    )
                )
            return "\n".join(
                value
                for value in (
                    presentation.title,
                    presentation.subtitle,
                    presentation.action,
                )
                if value
            )
        if role == Qt.ItemDataRole.AccessibleTextRole:
            if column is JobColumn.DRAG:
                return f"Reordenar {job.source.path.name}"
            if column is JobColumn.REMOVE:
                return f"Quitar {job.source.path.name} de la cola"
            presentation = cell_presentation(
                job,
                column,
                preparing=preparing,
                preparation_detail=preparation_detail,
                compact=self._compact,
                forecast=forecast,
                runtime_estimate=runtime_estimate,
                integrity_report=integrity_report,
            )
            return " · ".join(
                value
                for value in (
                    HEADERS[column],
                    presentation.title,
                    ", ".join(presentation.operations) if presentation.operations else None,
                    presentation.subtitle,
                    presentation.status,
                    presentation.action if presentation.action != presentation.status else None,
                )
                if value
            )
        return None

    def headerData(  # noqa: N802
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if (
            orientation is Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
            and 0 <= section < len(COLUMNS)
        ):
            column = COLUMNS[section]
            if self._compact and column is JobColumn.DOCUMENT:
                return "Documento y flujo"
            return HEADERS[column]
        return None

    def job_at(self, row: int) -> DocumentJob:
        return self._jobs[row]

    def flags(self, index: ModelIndex) -> Qt.ItemFlag:
        flags = super().flags(index)
        if index.isValid():
            return flags | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled
        return flags | Qt.ItemFlag.ItemIsDropEnabled

    def supportedDropActions(self) -> Qt.DropAction:  # noqa: N802
        return Qt.DropAction.MoveAction


class JobHeaderView(QHeaderView):
    """Paint one calm header without artificial column partitions."""

    HEIGHT = 44
    FONT_SIZE = 10.0

    def paintSection(  # noqa: N802
        self,
        painter: QPainter,
        rect: QRect,
        logical_index: int,
    ) -> None:
        if not rect.isValid():
            return
        painter.save()
        painter.fillRect(rect, QColor(COLORS.table_header))
        painter.setPen(QColor(COLORS.text_primary))
        font = QFont(self.font())
        font.setWeight(QFont.Weight.DemiBold)
        font.setPointSizeF(self.FONT_SIZE)
        painter.setFont(font)
        label = self.model().headerData(
            logical_index,
            self.orientation(),
            Qt.ItemDataRole.DisplayRole,
        )
        text_rect = rect.adjusted(16, 0, -12, 0)
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            str(label or ""),
        )
        painter.setPen(QPen(QColor(COLORS.table_separator), 1))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        painter.restore()


class JobCellDelegate(QStyledItemDelegate):
    ROW_HEIGHT = 80

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: ModelIndex,
    ) -> None:
        presentation = index.data(CELL_PRESENTATION_ROLE)
        if not isinstance(presentation, CellPresentation):
            return
        painter.save()
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        cell_hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        table = self.parent()
        row_hovered = isinstance(table, JobTableView) and table.hovered_row == index.row()
        selection_emphasized = selected and isinstance(table, JobTableView) and table.hasFocus()
        configuring = bool(index.data(CONFIGURING_ROLE))
        job_running = bool(index.data(JOB_RUNNING_ROLE))
        column = COLUMNS[index.column()]
        background = (
            COLORS.table_selected
            if selection_emphasized
            else COLORS.table_hover
            if row_hovered
            else COLORS.table_surface
        )
        painter.fillRect(option.rect, QColor(background))

        if column is JobColumn.DRAG:
            self._paint_drag_handle(painter, option)
            if selection_emphasized or job_running:
                painter.fillRect(
                    QRectF(option.rect.left(), option.rect.top(), 3, option.rect.height()),
                    QColor(COLORS.action_primary),
                )
        elif column is JobColumn.REMOVE:
            self._paint_remove_action(
                painter,
                option,
                index,
                hovered=cell_hovered or bool(option.state & QStyle.StateFlag.State_HasFocus),
            )

        if configuring:
            self._paint_configuring_action(painter, option)
            self._paint_cell_boundaries(painter, option, index)
            painter.restore()
            return
        if column is JobColumn.DOCUMENT:
            self._paint_document(painter, option, presentation)
        elif column is JobColumn.FLOW and presentation.operations:
            self._paint_flow(painter, option, presentation)
        elif column is JobColumn.NEXT_STEP:
            self._paint_next_step(painter, option, presentation, hovered=cell_hovered)
        elif column not in {JobColumn.DRAG, JobColumn.REMOVE}:
            self._paint_text_cell(painter, option, presentation)

        self._paint_cell_boundaries(painter, option, index)
        painter.restore()

    @classmethod
    def _paint_document(
        cls,
        painter: QPainter,
        option: QStyleOptionViewItem,
        presentation: CellPresentation,
    ) -> None:
        icon_rect = QRectF(option.rect.left() + 14, option.rect.center().y() - 17, 28, 34)
        cls._paint_document_icon(painter, icon_rect, presentation.document_format)
        left = icon_rect.right() + 14
        width = max(0, option.rect.right() - left - 12)
        title_font = QFont(option.font)
        title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor(COLORS.text_primary))
        cls._draw_elided(
            painter,
            QRectF(left, option.rect.center().y() - 21, width, 21),
            presentation.title,
        )
        painter.setFont(option.font)
        painter.setPen(QColor(COLORS.text_secondary))
        cls._draw_elided(
            painter,
            QRectF(left, option.rect.center().y() + 3, width, 19),
            presentation.subtitle or "",
        )

    @classmethod
    def _paint_text_cell(
        cls,
        painter: QPainter,
        option: QStyleOptionViewItem,
        presentation: CellPresentation,
    ) -> None:
        left = option.rect.left() + 14
        width = max(0, option.rect.width() - 26)
        line_count = 1 + int(bool(presentation.subtitle))
        top = option.rect.center().y() - (21 if line_count == 2 else 10)
        title_font = QFont(option.font)
        title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(
            QColor(COLORS.text_muted if presentation.tone == "disabled" else COLORS.text_primary)
        )
        cls._draw_elided(painter, QRectF(left, top, width, 21), presentation.title)
        if presentation.subtitle:
            painter.setFont(option.font)
            painter.setPen(QColor(COLORS.text_secondary))
            cls._draw_elided(
                painter,
                QRectF(left, top + 24, width, 19),
                presentation.subtitle,
            )

    @classmethod
    def _paint_next_step(
        cls,
        painter: QPainter,
        option: QStyleOptionViewItem,
        presentation: CellPresentation,
        *,
        hovered: bool,
    ) -> None:
        left = option.rect.left() + 14
        width = max(0, option.rect.width() - 28)
        tone_color = {
            "running": COLORS.action_primary,
            "completed": COLORS.success,
            "review": COLORS.warning,
            "warning": COLORS.warning,
            "error": COLORS.error,
        }.get(presentation.tone, COLORS.text_primary)
        label_font = QFont(option.font)
        label_font.setWeight(
            QFont.Weight.DemiBold
            if presentation.tone in {"completed", "review", "error"}
            else QFont.Weight.Normal
        )
        painter.setFont(label_font)
        painter.setPen(QColor(tone_color))
        has_secondary_line = bool(presentation.subtitle)
        label_top = option.rect.center().y() - (
            29 if presentation.action else 20 if has_secondary_line else 10
        )
        cls._draw_elided(
            painter,
            QRectF(left, label_top, width, 21),
            presentation.title,
        )
        if presentation.subtitle:
            painter.setFont(option.font)
            painter.setPen(QColor(COLORS.text_secondary))
            cls._draw_elided(
                painter,
                QRectF(left, label_top + 21, width, 19),
                presentation.subtitle,
            )
        if presentation.action:
            metrics = QFontMetrics(option.font)
            button_width = min(width, metrics.horizontalAdvance(presentation.action) + 28)
            button_rect = QRectF(left, label_top + 26, button_width, 32)
            border = {
                "completed": COLORS.success,
                "review": COLORS.warning,
                "error": COLORS.error,
            }.get(presentation.tone, COLORS.action_primary)
            fill = {
                "completed": COLORS.success_soft,
                "review": COLORS.warning_soft,
                "error": COLORS.error_soft,
            }.get(presentation.tone, COLORS.action_primary_soft)
            painter.setBrush(QColor(fill if not hovered else COLORS.surface_hover))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(button_rect, 6, 6)
            painter.setPen(QColor(border))
            painter.drawText(button_rect, Qt.AlignmentFlag.AlignCenter, presentation.action)
        if presentation.progress is not None and presentation.tone == "running":
            track = cls._progress_track_rect(option, presentation)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(COLORS.progress_track))
            painter.drawRoundedRect(track, 2.5, 2.5)
            painter.setBrush(QColor(COLORS.action_primary))
            painter.drawRoundedRect(
                QRectF(track.left(), track.top(), track.width() * presentation.progress, 5),
                2.5,
                2.5,
            )

    @classmethod
    def _progress_track_rect(
        cls,
        option: QStyleOptionViewItem,
        presentation: CellPresentation,
    ) -> QRectF:
        """Place the running track below the remaining-time line in the row."""

        left = option.rect.left() + 14
        width = max(0, option.rect.width() - 28)
        has_secondary_line = bool(presentation.subtitle)
        label_top = option.rect.center().y() - (
            29 if presentation.action else 20 if has_secondary_line else 10
        )
        line_bottom = label_top + (40 if has_secondary_line else 21)
        track_top = min(
            line_bottom + SPACING.xs,
            option.rect.bottom() - 5,
        )
        return QRectF(left, track_top, min(190, width), 5)

    @classmethod
    def _paint_flow(
        cls,
        painter: QPainter,
        option: QStyleOptionViewItem,
        presentation: CellPresentation,
    ) -> None:
        font = QFont(option.font)
        font.setPointSizeF(max(8.5, font.pointSizeF() - 0.25))
        painter.setFont(font)
        painter.setPen(QColor(COLORS.text_secondary))
        available = QRectF(option.rect.adjusted(14, 0, -12, 0))
        has_review_note = bool(presentation.subtitle)
        primary_top = option.rect.center().y() - (20 if has_review_note else 10)
        cls._draw_elided(
            painter,
            QRectF(available.left(), primary_top, available.width(), 21),
            "  →  ".join(presentation.operations),
        )
        if presentation.subtitle:
            note_font = QFont(option.font)
            note_font.setPointSizeF(max(8.0, note_font.pointSizeF() - 0.75))
            painter.setFont(note_font)
            painter.setPen(QColor(COLORS.text_muted))
            cls._draw_elided(
                painter,
                QRectF(available.left(), primary_top + 23, available.width(), 19),
                presentation.subtitle,
            )

    @staticmethod
    def _draw_elided(
        painter: QPainter,
        rect: QRectF,
        text: str,
        *,
        alignment: Qt.AlignmentFlag = (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
    ) -> None:
        text = QFontMetrics(painter.font()).elidedText(
            text,
            Qt.TextElideMode.ElideRight,
            max(0, int(rect.width())),
        )
        painter.drawText(rect, alignment, text)

    @staticmethod
    def _paint_configuring_action(painter: QPainter, option: QStyleOptionViewItem) -> None:
        rect = option.rect.adjusted(12, 21, -12, -21)
        painter.setBrush(QColor(COLORS.action_primary_soft))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(QRectF(rect), 6, 6)
        font = QFont(option.font)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor(COLORS.action_primary_hover))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "Configurando…")

    @staticmethod
    def _paint_required_action(
        painter: QPainter,
        option: QStyleOptionViewItem,
        *,
        hovered: bool,
    ) -> None:
        available = option.rect.adjusted(12, 0, -12, 0)
        button_width = min(220, max(120, available.width() - 20))
        button_rect = QRectF(
            available.center().x() - button_width / 2,
            option.rect.center().y() - 19,
            button_width,
            38,
        )
        painter.setBrush(QColor(COLORS.surface_hover if hovered else COLORS.action_primary_soft))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(button_rect, 6, 6)
        font = QFont(option.font)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor(COLORS.action_primary_hover))
        painter.drawText(button_rect, Qt.AlignmentFlag.AlignCenter, "Configurar")

    @staticmethod
    def _paint_cell_boundaries(
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: ModelIndex,
    ) -> None:
        if index.row() >= index.model().rowCount() - 1:
            return
        painter.setPen(QPen(QColor(COLORS.table_separator), 1))
        painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())

    @staticmethod
    def _paint_remove_action(
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: ModelIndex,
        *,
        hovered: bool,
    ) -> None:
        available = bool(index.data(REMOVE_AVAILABLE_ROLE))
        color = COLORS.error if available and hovered else COLORS.text_muted
        if not available:
            color = COLORS.disabled
        center_x = option.rect.center().x()
        center_y = option.rect.center().y()
        pen = QPen(QColor(color), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(center_x - 7, center_y - 6, 14, 15), 2, 2)
        painter.drawLine(center_x - 9, center_y - 10, center_x + 9, center_y - 10)
        painter.drawLine(center_x - 4, center_y - 13, center_x + 4, center_y - 13)
        painter.drawLine(center_x - 3, center_y - 2, center_x - 3, center_y + 5)
        painter.drawLine(center_x + 3, center_y - 2, center_x + 3, center_y + 5)

    @staticmethod
    def _paint_drag_handle(
        painter: QPainter,
        option: QStyleOptionViewItem,
    ) -> None:
        center_y = option.rect.center().y()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(COLORS.text_muted))
        for column in range(2):
            for row in range(3):
                painter.drawEllipse(
                    int(option.rect.center().x() - 4 + column * 6),
                    int(center_y - 7 + row * 7),
                    2,
                    2,
                )

    @staticmethod
    def _paint_document_icon(
        painter: QPainter,
        icon_rect: QRectF,
        document_format: DocumentFormat | None,
    ) -> None:
        icon_color = (
            COLORS.action_primary
            if document_format is None
            else {
                DocumentFormat.PDF: COLORS.format_pdf,
                DocumentFormat.EPUB: COLORS.format_epub,
                DocumentFormat.DOCX: COLORS.format_docx,
                DocumentFormat.MARKDOWN: COLORS.action_primary,
                DocumentFormat.TEXT: COLORS.text_muted,
            }.get(document_format, COLORS.action_primary)
        )
        painter.setBrush(QColor(COLORS.surface_subtle))
        painter.setPen(QPen(QColor(icon_color), 1.4))
        path = QPainterPath()
        path.moveTo(icon_rect.left(), icon_rect.top())
        path.lineTo(icon_rect.right() - 8, icon_rect.top())
        path.lineTo(icon_rect.right(), icon_rect.top() + 8)
        path.lineTo(icon_rect.right(), icon_rect.bottom())
        path.lineTo(icon_rect.left(), icon_rect.bottom())
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawLine(
            QPointF(icon_rect.right() - 8, icon_rect.top()),
            QPointF(icon_rect.right() - 8, icon_rect.top() + 8),
        )
        painter.drawLine(
            QPointF(icon_rect.right() - 8, icon_rect.top() + 8),
            QPointF(icon_rect.right(), icon_rect.top() + 8),
        )
        labels = {
            DocumentFormat.PDF: "PDF",
            DocumentFormat.EPUB: "EPUB",
            DocumentFormat.DOCX: "DOCX",
            DocumentFormat.MARKDOWN: "MD",
            DocumentFormat.TEXT: "TXT",
        }
        label_font = QFont(painter.font())
        label_font.setPointSizeF(6.5)
        label_font.setWeight(QFont.Weight.Bold)
        painter.setFont(label_font)
        painter.setPen(QColor(icon_color))
        format_label = "" if document_format is None else labels.get(document_format, "")
        painter.drawText(
            icon_rect.adjusted(1, 12, -1, -3),
            Qt.AlignmentFlag.AlignCenter,
            format_label,
        )

    def sizeHint(  # noqa: N802
        self,
        option: QStyleOptionViewItem,
        index: ModelIndex,
    ) -> QSize:
        size = super().sizeHint(option, index)
        size.setHeight(self.ROW_HEIGHT)
        return size


class JobTableView(QTableView):
    MAX_VISIBLE_ROWS = 6
    configure_requested = Signal(str, object)
    review_requested = Signal(str, object)
    ai_review_requested = Signal(str)
    error_requested = Signal(str, object)
    open_result_requested = Signal(str)
    open_folder_requested = Signal(str)
    result_summary_requested = Signal(str)
    remove_requested = Signal(str)
    move_requested = Signal(str, int)
    job_selected = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("jobTable")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setModel(JobTableModel(parent=self))
        self.setItemDelegate(JobCellDelegate(self))
        header = JobHeaderView(Qt.Orientation.Horizontal, self)
        header.setObjectName("jobTableHeader")
        header.setFixedHeight(JobHeaderView.HEIGHT)
        self.setHorizontalHeader(header)
        self.verticalHeader().hide()
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.horizontalHeader().setStretchLastSection(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setShowGrid(False)
        self.setAlternatingRowColors(False)
        self.setMouseTracking(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        self.setMinimumHeight(0)
        self._drag_source_row: int | None = None
        self._hovered_row = -1
        self.clicked.connect(self._cell_clicked)
        self.doubleClicked.connect(self._cell_double_clicked)
        self.selectionModel().currentRowChanged.connect(self._current_row_changed)
        self._compact_mode = False

    @property
    def job_model(self) -> JobTableModel:
        model = self.model()
        if not isinstance(model, JobTableModel):
            raise RuntimeError("The Parsezen job table model was replaced.")
        return model

    @property
    def hovered_row(self) -> int:
        return self._hovered_row

    def set_jobs(self, jobs: tuple[DocumentJob, ...]) -> None:
        selected_id = (
            str(self.currentIndex().data(JOB_ID_ROLE)) if self.currentIndex().isValid() else None
        )
        self.job_model.set_jobs(jobs)
        for row in range(len(jobs)):
            self.setRowHeight(row, JobCellDelegate.ROW_HEIGHT)
        visible_rows = min(len(jobs), self.MAX_VISIBLE_ROWS)
        content_height = (
            JobHeaderView.HEIGHT + visible_rows * JobCellDelegate.ROW_HEIGHT + self.frameWidth() * 2
        )
        self.setFixedHeight(content_height)
        self._resize_columns()
        if jobs:
            selected_row = next(
                (row for row, job in enumerate(self.job_model.jobs) if job.id == selected_id),
                0,
            )
            self.selectRow(selected_row)
            self.setCurrentIndex(self.job_model.index(selected_row, 0))

    def set_configuring(self, job_id: str | None, stage: StageKind | None) -> None:
        self.job_model.set_configuring(job_id, stage)

    def set_preparing_jobs(
        self,
        job_ids: tuple[str, ...],
        details: dict[str, str] | None = None,
    ) -> None:
        self.job_model.set_preparing_jobs(job_ids, details)

    def set_forecasts(self, forecasts: dict[str, DocumentPreflight]) -> None:
        self.job_model.set_forecasts(forecasts)
        self.viewport().update()

    def set_runtime_estimates(self, estimates: dict[str, RuntimeEstimate]) -> None:
        self.job_model.set_runtime_estimates(estimates)
        self.viewport().update()

    def set_integrity_reports(
        self,
        reports: dict[str, FinalIntegrityReport],
    ) -> None:
        self.job_model.set_integrity_reports(reports)
        self.viewport().update()

    def set_compact_mode(self, compact: bool) -> None:
        if compact == self._compact_mode:
            return
        self._compact_mode = compact
        self.job_model.set_compact_mode(compact)
        self.setColumnHidden(COLUMNS.index(JobColumn.FLOW), compact)
        self.setColumnHidden(COLUMNS.index(JobColumn.RESULT), compact)
        self._resize_columns()

    def selected_job_ids(self) -> tuple[str, ...]:
        rows = sorted({index.row() for index in self.selectionModel().selectedRows()})
        return tuple(self.job_model.job_at(row).id for row in rows)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._resize_columns()

    def _cell_clicked(self, index: QModelIndex) -> None:
        presentation = index.data(CELL_PRESENTATION_ROLE)
        if isinstance(presentation, CellPresentation) and presentation.action == "Revisar":
            self.review_requested.emit(str(index.data(JOB_ID_ROLE)), index.data(STAGE_KIND_ROLE))
            return
        if isinstance(presentation, CellPresentation) and presentation.action == "Revisar con IA":
            self.ai_review_requested.emit(str(index.data(JOB_ID_ROLE)))
            return
        if isinstance(presentation, CellPresentation) and presentation.action == "Ver error":
            self.error_requested.emit(str(index.data(JOB_ID_ROLE)), index.data(STAGE_KIND_ROLE))
            return
        if isinstance(presentation, CellPresentation) and presentation.action == "Abrir resultado":
            self.open_result_requested.emit(str(index.data(JOB_ID_ROLE)))
            return
        if isinstance(presentation, CellPresentation) and presentation.action == "Configurar":
            self.configure_requested.emit(str(index.data(JOB_ID_ROLE)), StageKind.PUBLISH)
            return
        if bool(index.data(CONFIGURABLE_ROLE)):
            stage = StageKind.PUBLISH if COLUMNS[index.column()] is JobColumn.RESULT else None
            self.configure_requested.emit(str(index.data(JOB_ID_ROLE)), stage)

    def _current_row_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if current.isValid():
            self.job_selected.emit(str(current.data(JOB_ID_ROLE)))

    def _cell_double_clicked(self, index: QModelIndex) -> None:
        self._cell_clicked(index)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        source_row = self._drag_source_row
        self._drag_source_row = None
        if source_row is None:
            event.ignore()
            return
        target_index = self.indexAt(event.position().toPoint())
        target_row = target_index.row() if target_index.isValid() else self.model().rowCount()
        if target_row > source_row:
            target_row -= 1
        target_row = max(0, min(target_row, max(0, self.model().rowCount() - 1)))
        if target_row == source_row:
            event.ignore()
            return
        self.move_requested.emit(str(self.job_model.job_at(source_row).id), target_row)
        event.acceptProposedAction()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        index = self.indexAt(event.position().toPoint())
        if event.button() is Qt.MouseButton.LeftButton and index.isValid():
            if self._remove_hit(index, event.position().toPoint()):
                self._drag_source_row = None
                event.accept()
                return
            if COLUMNS[index.column()] is JobColumn.DRAG:
                self._drag_source_row = index.row()
                self.setCurrentIndex(index)
            else:
                self._drag_source_row = None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        position = event.position().toPoint()
        index = self.indexAt(position)
        hovered_row = index.row() if index.isValid() else -1
        if hovered_row != self._hovered_row:
            self._hovered_row = hovered_row
            self.viewport().update()
        cursor = Qt.CursorShape.ArrowCursor
        if self._remove_hit(index, position):
            cursor = Qt.CursorShape.PointingHandCursor
        elif index.isValid() and COLUMNS[index.column()] is JobColumn.DRAG:
            cursor = Qt.CursorShape.OpenHandCursor
        elif self._is_interactive(index):
            cursor = Qt.CursorShape.PointingHandCursor
        self.viewport().setCursor(cursor)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: QEvent) -> None:  # noqa: N802
        if self._hovered_row != -1:
            self._hovered_row = -1
            self.viewport().update()
        self.viewport().unsetCursor()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() is Qt.MouseButton.LeftButton:
            index = self.indexAt(event.position().toPoint())
            if self._remove_hit(index, event.position().toPoint()):
                self._drag_source_row = None
                self.remove_requested.emit(str(index.data(JOB_ID_ROLE)))
                event.accept()
                return
        super().mouseReleaseEvent(event)

    def startDrag(self, supported_actions: Qt.DropAction) -> None:  # noqa: N802
        if self._drag_source_row is not None:
            super().startDrag(supported_actions & Qt.DropAction.MoveAction)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in {Qt.Key.Key_Delete, Qt.Key.Key_Backspace}:
            index = self.currentIndex()
            remove_index = (
                self.job_model.index(index.row(), COLUMNS.index(JobColumn.REMOVE))
                if index.isValid()
                else QModelIndex()
            )
            if remove_index.isValid() and bool(remove_index.data(REMOVE_AVAILABLE_ROLE)):
                self.remove_requested.emit(str(remove_index.data(JOB_ID_ROLE)))
                event.accept()
                return
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space}:
            index = self.currentIndex()
            if index.isValid():
                self._cell_clicked(index)
                event.accept()
                return
        super().keyPressEvent(event)

    def _show_context_menu(self, position: QPoint) -> None:
        index = self.indexAt(position)
        if not index.isValid():
            return
        job_id = str(index.data(JOB_ID_ROLE))
        job = self.job_model.job_at(index.row())
        menu = QMenu(self)
        configure: QAction | None = None
        review: QAction | None = None
        error_details: QAction | None = None
        open_result: QAction | None = None
        open_folder: QAction | None = None
        result_summary: QAction | None = None
        if job.result_path is not None and job.status is JobStatus.COMPLETED:
            open_result = menu.addAction("Abrir resultado")
            open_folder = menu.addAction("Abrir carpeta")
            result_summary = menu.addAction("Ver resumen")
            targeted_review = (
                menu.addAction("Revisar señales con IA")
                if job.review_recommendation is not None
                else None
            )
            menu.addSeparator()
        else:
            targeted_review = None
        if job.status is JobStatus.WAITING_REVIEW:
            review = menu.addAction("Revisar")
            menu.addSeparator()
        elif job.status in {JobStatus.QUEUED, JobStatus.FAILED, JobStatus.CANCELLED}:
            if job.status is JobStatus.FAILED:
                error_details = menu.addAction("Ver detalles del error")
                menu.addSeparator()
            configure = menu.addAction("Configurar documento")
        remove: QAction | None = None
        if job.status is not JobStatus.RUNNING:
            remove = menu.addAction("Quitar de la cola")
        if not menu.actions():
            return
        selected = menu.exec(self.viewport().mapToGlobal(position))
        stage = focus_stage(job).kind if job.is_configured else None
        if configure is not None and selected is configure:
            self.configure_requested.emit(job_id, None)
        elif review is not None and selected is review:
            self.review_requested.emit(job_id, stage)
        elif error_details is not None and selected is error_details:
            self.error_requested.emit(job_id, stage)
        elif open_result is not None and selected is open_result:
            self.open_result_requested.emit(job_id)
        elif open_folder is not None and selected is open_folder:
            self.open_folder_requested.emit(job_id)
        elif result_summary is not None and selected is result_summary:
            self.result_summary_requested.emit(job_id)
        elif targeted_review is not None and selected is targeted_review:
            self.ai_review_requested.emit(job_id)
        elif remove is not None and selected is remove:
            self.remove_requested.emit(job_id)

    def _remove_hit(self, index: QModelIndex, position: QPoint) -> bool:
        if (
            not index.isValid()
            or COLUMNS[index.column()] is not JobColumn.REMOVE
            or not bool(index.data(REMOVE_AVAILABLE_ROLE))
        ):
            return False
        rect = self.visualRect(index)
        return rect.adjusted(4, 18, -4, -18).contains(position)

    @staticmethod
    def _is_interactive(index: QModelIndex) -> bool:
        if not index.isValid():
            return False
        presentation = index.data(CELL_PRESENTATION_ROLE)
        return bool(index.data(CONFIGURABLE_ROLE)) or (
            isinstance(presentation, CellPresentation)
            and presentation.action
            in {"Revisar", "Revisar con IA", "Ver error", "Abrir resultado", "Configurar"}
        )

    def _resize_columns(self) -> None:
        available = max(1, self.viewport().width())
        if self._compact_mode:
            drag_width = 32
            remove_width = 36
            action_width = min(116, max(92, int(available * 0.34)))
            document_width = max(1, available - drag_width - remove_width - action_width - 2)
            widths = {
                JobColumn.DRAG: drag_width,
                JobColumn.DOCUMENT: document_width,
                JobColumn.NEXT_STEP: action_width,
                JobColumn.REMOVE: remove_width,
            }
            for index, column in enumerate(COLUMNS):
                if not self.isColumnHidden(index):
                    self.setColumnWidth(index, widths[column])
            return
        utility_widths = {
            JobColumn.DRAG: 44,
            JobColumn.REMOVE: 48,
        }
        semantic_available = available - sum(utility_widths.values())
        weights = {
            JobColumn.DOCUMENT: 33,
            JobColumn.FLOW: 29,
            JobColumn.RESULT: 16,
            JobColumn.NEXT_STEP: 22,
        }
        total = sum(weights.values())
        used = 0
        semantic_columns = tuple(weights)
        for index, column in enumerate(COLUMNS):
            if column in utility_widths:
                self.setColumnWidth(index, utility_widths[column])
                continue
            weight = weights[column]
            width = (
                semantic_available - used
                if column is semantic_columns[-1]
                else max(135, int(semantic_available * weight / total))
            )
            self.setColumnWidth(index, width)
            used += width
