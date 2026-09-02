"""Main Parsezen workspace for the document queue and internal workflows."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDropEvent,
    QIcon,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
    QResizeEvent,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QBoxLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from parsezen import APP_DISPLAY_NAME
from parsezen.application.preflight import (
    DocumentPreflight,
    QueuePreflight,
    RuntimeEstimate,
    format_duration_range,
)
from parsezen.branding import BRAND_DARK_LOGO_PATH, BRAND_LOGO_PATH
from parsezen.domain.jobs import DocumentJob, JobStatus
from parsezen.domain.stages import StageKind
from parsezen.failure_recovery import RecoveryAction, RecoveryPlan
from parsezen.final_integrity import FinalIntegrityReport
from parsezen.local_models import OllamaStatus
from parsezen.presentation.components import StatusMessage
from parsezen.presentation.design_system import (
    BREAKPOINTS,
    COLORS,
    SPACING,
    ThemeMode,
    add_documents_icon,
    back_icon,
    current_theme_mode,
    folder_icon,
    pause_icon,
    play_icon,
    settings_icon,
)
from parsezen.presentation.job_table import JobTableView
from parsezen.presentation.job_view_model import queue_header_view

_CONTENT_RAIL_MAX_WIDTH = 1280
_EMPTY_DROP_MAX_WIDTH = 620


class _RoundedTableOverlay(QWidget):
    """Cover rectangular child corners and draw one antialiased table outline."""

    RADIUS = 10.0
    BORDER_WIDTH = 1.0

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds = QRectF(self.rect())
        rounded_bounds = bounds.adjusted(
            self.BORDER_WIDTH / 2,
            self.BORDER_WIDTH / 2,
            -self.BORDER_WIDTH / 2,
            -self.BORDER_WIDTH / 2,
        )

        outside = QPainterPath()
        outside.setFillRule(Qt.FillRule.OddEvenFill)
        outside.addRect(bounds)
        outside.addRoundedRect(rounded_bounds, self.RADIUS, self.RADIUS)
        painter.fillPath(outside, QColor(COLORS.canvas))

        pen = QPen(QColor(COLORS.divider), self.BORDER_WIDTH)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rounded_bounds, self.RADIUS, self.RADIUS)


class RoundedTablePanel(QFrame):
    """Table host whose foreground outline remains crisp in either theme."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._outline = _RoundedTableOverlay(self)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._outline.setGeometry(self.rect())
        self._outline.raise_()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802
        super().showEvent(event)
        self._outline.setGeometry(self.rect())
        self._outline.raise_()


class InternalBackButton(QPushButton):
    """Borderless back action that grows instead of drawing a hover box."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._hovered = False
        self.setObjectName("internalBack")
        self.setFlat(True)
        self.setIconSize(QSize(20, 20))

    def event(self, event: QEvent) -> bool:
        handled = super().event(event)
        if event.type() is QEvent.Type.Enter:
            self._hovered = True
        elif event.type() is QEvent.Type.Leave:
            self._hovered = False
        if event.type() in {
            QEvent.Type.Enter,
            QEvent.Type.Leave,
            QEvent.Type.FocusIn,
            QEvent.Type.FocusOut,
        }:
            self.setIconSize(QSize(24, 24) if self._hovered or self.hasFocus() else QSize(20, 20))
        return handled


class DocumentDropArea(QFrame):
    """Visible local drop target with one conventional file-picker button."""

    activated = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("documentDropArea")
        self.setAccessibleName("Añadir documentos TXT, Markdown, Word, PDF o EPUB")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(92)

        layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self.content_layout = layout
        layout.setContentsMargins(18, 8, 18, 8)
        layout.setSpacing(12)
        layout.addStretch(1)
        self.icon_label = QLabel(self)
        self.icon_label.setObjectName("dropAreaIcon")
        self.icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignVCenter)

        self.text_host = QWidget(self)
        self.text_host.setMinimumWidth(260)
        text_layout = QVBoxLayout(self.text_host)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(3)
        self.primary_label = QLabel(self.text_host)
        self.primary_label.setObjectName("dropAreaPrimary")
        self.primary_label.setWordWrap(True)
        self.primary_label.setMinimumWidth(0)
        self.primary_label.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Preferred,
        )
        self.primary_label.setTextFormat(Qt.TextFormat.PlainText)
        self.primary_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.secondary_label = QLabel(
            "TXT · MD · DOCX · PDF · EPUB",
            self.text_host,
        )
        self.secondary_label.setObjectName("dropAreaSecondary")
        self.secondary_label.setWordWrap(True)
        self.secondary_label.setMinimumWidth(0)
        self.secondary_label.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Preferred,
        )
        self.secondary_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        text_layout.addWidget(self.primary_label)
        text_layout.addWidget(self.secondary_label)
        layout.addWidget(self.text_host, 0, Qt.AlignmentFlag.AlignVCenter)
        self.browse_button = QPushButton("Seleccionar archivos", self)
        self.browse_button.setObjectName("dropBrowseButton")
        self.browse_button.setAccessibleName("Seleccionar documentos para añadir")
        self.browse_button.setMinimumWidth(156)
        self.browse_button.setMaximumWidth(180)
        self.browse_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.browse_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.browse_button.clicked.connect(self.activated)
        layout.addWidget(self.browse_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addStretch(1)
        self.refresh_theme()

    def refresh_theme(self) -> None:
        self.icon_label.setPixmap(add_documents_icon().pixmap(30, 30))
        self.primary_label.setText("Arrastra documentos aquí")

    def set_display_mode(self, *, compact: bool) -> None:
        """Reflow the empty-state action without changing its meaning."""

        if compact:
            self.setFixedHeight(140)
            self.content_layout.setDirection(QBoxLayout.Direction.TopToBottom)
            self.content_layout.setContentsMargins(12, 10, 12, 10)
            self.content_layout.setSpacing(6)
            self.text_host.setMinimumWidth(0)
            self.primary_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.secondary_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.content_layout.setAlignment(
                self.browse_button,
                Qt.AlignmentFlag.AlignHCenter,
            )
        else:
            self.setFixedHeight(92)
            self.content_layout.setDirection(QBoxLayout.Direction.LeftToRight)
            self.content_layout.setContentsMargins(18, 10, 18, 10)
            self.content_layout.setSpacing(14)
            self.text_host.setMinimumWidth(260)
            alignment = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            self.primary_label.setAlignment(alignment)
            self.secondary_label.setAlignment(alignment)
            self.content_layout.setAlignment(
                self.browse_button,
                Qt.AlignmentFlag.AlignVCenter,
            )
        self.refresh_theme()
        self.style().unpolish(self)
        self.style().polish(self)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() is Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.activated.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


def _stage_label(stage: StageKind) -> str:
    return {
        StageKind.PREPARE: "preparación",
        StageKind.TRANSLATE: "traducción",
        StageKind.REFINE: "corrección",
        StageKind.STRUCTURE: "personalización",
        StageKind.PUBLISH: "publicación",
    }[stage]


class ParsezenWorkspace(QWidget):
    add_requested = Signal()
    files_dropped = Signal(object)
    settings_requested = Signal()
    local_ai_requested = Signal()
    configure_requested = Signal(str, object)
    source_requested = Signal(str)
    review_requested = Signal(str, object)
    ai_review_requested = Signal(str)
    error_requested = Signal(str, object)
    open_result_requested = Signal(str)
    open_folder_requested = Signal(str)
    result_summary_requested = Signal(str)
    remove_requested = Signal(str)
    move_requested = Signal(str, int)
    primary_requested = Signal(str)
    output_directory_requested = Signal()
    output_directory_reset_requested = Signal()
    internal_back_requested = Signal(object)
    retry_requested = Signal(str)
    activity_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("parsezenPage")
        self.setAcceptDrops(True)
        self._jobs: tuple[DocumentJob, ...] = ()
        self._preparing_job_ids: tuple[str, ...] = ()
        self._preparing_can_pause = False
        self._forecasts: dict[str, DocumentPreflight] = {}
        self._runtime_estimates: dict[str, RuntimeEstimate] = {}
        self._integrity_reports: dict[str, FinalIntegrityReport] = {}
        self._queue_preflight: QueuePreflight | None = None
        self._output_directory: Path | None = None
        self._local_ai_status: OllamaStatus | None = None
        self._local_ai_model: str | None = None
        self._primary_mode: str | None = None
        self._pause_feedback_pending = False
        self._message_job_id: str | None = None
        self._message_stage: StageKind | None = None
        self._message_primary_action: RecoveryAction | None = None
        self._message_secondary_action: RecoveryAction | None = None
        self._batch_message_job_ids: frozenset[str] = frozenset()
        self._compact_layout: bool | None = None
        self._layout_mode: str | None = None

        layout = QVBoxLayout(self)
        self.root_layout = layout
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        layout.setContentsMargins(24, 16, 24, 20)
        layout.setSpacing(16)

        self.app_header = QWidget(self)
        self.app_header.setObjectName("appHeader")
        self.app_header.setMaximumWidth(_CONTENT_RAIL_MAX_WIDTH)
        self.app_header.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        header_layout = QGridLayout(self.app_header)
        self.header_layout = header_layout
        header_layout.setContentsMargins(8, 2, 0, 2)
        header_layout.setSpacing(8)
        self.logo = QLabel(self.app_header)
        self.logo.setObjectName("brandLogo")
        self.logo.setAccessibleName(APP_DISPLAY_NAME)
        self._apply_logo()
        header_layout.addWidget(self.logo, 0, 0)
        header_layout.setColumnStretch(1, 1)

        self.output_directory_button = QPushButton(
            "Guardar en · Junto al original", self.app_header
        )
        self.output_directory_button.setObjectName("globalOutputDirectory")
        self.output_directory_button.setAccessibleName("Cambiar la carpeta de destino")
        self.output_directory_button.setIcon(folder_icon())
        self.output_directory_button.setIconSize(QSize(19, 19))
        self.output_directory_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.output_directory_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.output_directory_button.clicked.connect(self._show_output_directory_menu)
        header_layout.addWidget(self.output_directory_button, 0, 2)

        self.add_button = QPushButton("Añadir", self.app_header)
        self.add_button.setObjectName("queueAddAction")
        self.add_button.setAccessibleName("Añadir más documentos")
        self.add_button.setToolTip("Añadir documentos")
        self.add_button.setIcon(add_documents_icon())
        self.add_button.setIconSize(QSize(18, 18))
        self.add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.add_button.clicked.connect(self.add_requested)
        header_layout.addWidget(self.add_button, 0, 3)

        self.primary_button = QPushButton(self.app_header)
        self.primary_button.setObjectName("primaryAction")
        self.primary_button.setMinimumWidth(156)
        self.primary_button.setIconSize(QSize(18, 18))
        self.primary_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.primary_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.primary_button.clicked.connect(self._emit_primary_action)
        self.primary_button.hide()
        header_layout.addWidget(self.primary_button, 0, 4)

        self.settings_button = QPushButton(self.app_header)
        self.settings_button.setObjectName("globalMenu")
        self.settings_button.setAccessibleName("Abrir ajustes y actividad")
        self.settings_button.setToolTip("Ajustes y actividad")
        self.settings_button.setIcon(settings_icon())
        self.settings_button.setIconSize(QSize(20, 20))
        self.settings_button.setFixedSize(42, 42)
        self.settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.settings_button.clicked.connect(self.settings_requested)
        header_layout.addWidget(self.settings_button, 0, 5)
        layout.addWidget(self.app_header, 0, Qt.AlignmentFlag.AlignHCenter)

        self.content_stack = QStackedWidget(self)
        self.content_stack.setObjectName("workspacePages")

        self.queue_pane = QWidget(self.content_stack)
        queue_layout = QVBoxLayout(self.queue_pane)
        self.queue_layout = queue_layout
        queue_layout.setContentsMargins(0, 0, 0, 0)
        queue_layout.setSpacing(10)
        self.queue_summary = QLabel("0 documentos", self.queue_pane)
        self.queue_summary.setObjectName("queueSummary")
        self.queue_summary.setWordWrap(False)
        self.queue_summary.setMinimumWidth(0)
        self.queue_summary.setMaximumWidth(_CONTENT_RAIL_MAX_WIDTH)
        self.queue_summary.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        queue_layout.addWidget(self.queue_summary)
        queue_layout.setAlignment(self.queue_summary, Qt.AlignmentFlag.AlignHCenter)
        self.table_panel = RoundedTablePanel(self.queue_pane)
        self.table_panel.setObjectName("jobTablePanel")
        self.table_panel.setMaximumWidth(_CONTENT_RAIL_MAX_WIDTH)
        self.table_panel.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        table_layout = QVBoxLayout(self.table_panel)
        table_layout.setContentsMargins(1, 1, 1, 1)
        table_layout.setSpacing(0)
        self.job_table = JobTableView(self.table_panel)
        self.job_table.configure_requested.connect(self.configure_requested)
        self.job_table.source_requested.connect(self.source_requested)
        self.job_table.review_requested.connect(self.review_requested)
        self.job_table.ai_review_requested.connect(self.ai_review_requested)
        self.job_table.error_requested.connect(self.error_requested)
        self.job_table.open_result_requested.connect(self.open_result_requested)
        self.job_table.open_folder_requested.connect(self.open_folder_requested)
        self.job_table.result_summary_requested.connect(self.result_summary_requested)
        self.job_table.remove_requested.connect(self.remove_requested)
        self.job_table.move_requested.connect(self.move_requested)
        table_layout.addWidget(self.job_table)
        queue_layout.addWidget(self.table_panel)
        queue_layout.setAlignment(self.table_panel, Qt.AlignmentFlag.AlignHCenter)
        self.drop_area = DocumentDropArea(self.queue_pane)
        self.drop_area.activated.connect(self.add_requested)
        queue_layout.addWidget(self.drop_area)
        queue_layout.setAlignment(self.drop_area, Qt.AlignmentFlag.AlignHCenter)
        self.recovery_warning = QLabel(self.queue_pane)
        self.recovery_warning.setObjectName("recoveryWarning")
        self.recovery_warning.setWordWrap(True)
        self.recovery_warning.hide()
        queue_layout.addWidget(self.recovery_warning)
        self.batch_message = StatusMessage(self.queue_pane)
        self.batch_message.actionRequested.connect(self.activity_requested)
        queue_layout.addWidget(self.batch_message)
        self.job_message = StatusMessage(self.queue_pane)
        self.job_message.actionRequested.connect(self._run_primary_recovery)
        self.job_message.secondaryActionRequested.connect(self._run_secondary_recovery)
        queue_layout.addWidget(self.job_message)
        for message in (self.recovery_warning, self.batch_message, self.job_message):
            message.setMaximumWidth(_CONTENT_RAIL_MAX_WIDTH)
            queue_layout.setAlignment(message, Qt.AlignmentFlag.AlignHCenter)
        queue_layout.addStretch(1)
        self.content_stack.addWidget(self.queue_pane)

        self.content_stack.setCurrentWidget(self.queue_pane)
        self._internal_pages: dict[
            QWidget,
            tuple[QWidget, QWidget, QWidget | None],
        ] = {}
        self._internal_settings_buttons: dict[QWidget, QPushButton] = {}
        self.current_internal_widget: QWidget | None = None
        layout.addWidget(self.content_stack, 1)

        self._apply_local_styles()
        self.set_jobs(())

    def apply_theme(self) -> None:
        """Refresh controls whose colors or assets are created at runtime."""

        self._apply_logo()
        self.output_directory_button.setIcon(folder_icon())
        self._refresh_settings_attention()
        self.add_button.setIcon(add_documents_icon())
        self.drop_area.refresh_theme()
        self._apply_local_styles()
        self._refresh_queue_commands()
        self.job_table.viewport().update()
        self.update()

    def _apply_logo(self) -> None:
        dark_mode = current_theme_mode() is ThemeMode.DARK
        logo_path = BRAND_DARK_LOGO_PATH if dark_mode else BRAND_LOGO_PATH
        logo = QPixmap(str(logo_path))
        if logo.isNull():
            self.logo.setPixmap(QPixmap())
            self.logo.setText(APP_DISPLAY_NAME)
        else:
            self.logo.setText("")
            self.logo.setPixmap(logo.scaledToWidth(132, Qt.TransformationMode.SmoothTransformation))
        target_width = (
            80 if self._layout_mode == "compact" else 108 if self._layout_mode == "medium" else 132
        )
        self.logo.setStyleSheet("background: transparent; padding: 4px 2px;")
        if not logo.isNull():
            self.logo.setPixmap(
                logo.scaledToWidth(
                    target_width,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

    @property
    def jobs(self) -> tuple[DocumentJob, ...]:
        return self._jobs

    def set_jobs(self, jobs: tuple[DocumentJob, ...]) -> None:
        self._jobs = tuple(sorted(jobs, key=lambda job: job.order))
        self._refresh_settings_attention()
        self._reconcile_contextual_messages()
        self.job_table.set_jobs(self._jobs)
        self.table_panel.setVisible(bool(self._jobs))
        self.queue_summary.setVisible(bool(self._jobs))
        self.add_button.setVisible(bool(self._jobs))
        self.drop_area.setVisible(not self._jobs)
        self.table_panel.setFixedHeight(self.job_table.height() + 2)
        self._apply_queue_state_layout()
        self._refresh_queue_commands()

    def _refresh_settings_attention(self) -> None:
        attention = any(
            job.status in {JobStatus.FAILED, JobStatus.PAUSED, JobStatus.WAITING_REVIEW}
            or job.review_recommendation is not None
            for job in self._jobs
        )
        for button in (self.settings_button, *self._internal_settings_buttons.values()):
            button.setIcon(settings_icon(attention=attention))
            button.setAccessibleName(
                "Abrir ajustes y actividad. Hay acciones pendientes."
                if attention
                else "Abrir ajustes y actividad"
            )
            button.setToolTip(
                "Ajustes y actividad · Hay acciones pendientes"
                if attention
                else "Ajustes y actividad"
            )

    def _apply_queue_state_layout(self) -> None:
        empty = not self._jobs
        compact = self._layout_mode == "compact"
        top_margin = SPACING.xl if empty and compact else SPACING.xxxl if empty else 0
        self.queue_layout.setContentsMargins(0, top_margin, 0, 0)
        root_margins = self.root_layout.contentsMargins()
        available_width = max(
            1,
            self.width() - root_margins.left() - root_margins.right(),
        )
        rail_width = min(_CONTENT_RAIL_MAX_WIDTH, available_width)
        self.app_header.setFixedWidth(rail_width)
        self.queue_summary.setFixedWidth(rail_width)
        self.table_panel.setFixedWidth(rail_width)
        self.recovery_warning.setFixedWidth(rail_width)
        self.batch_message.setFixedWidth(rail_width)
        self.job_message.setFixedWidth(rail_width)
        for header in self.findChildren(QFrame, "internalPageHeader"):
            header.setFixedWidth(rail_width)
        self.drop_area.setFixedWidth(min(_EMPTY_DROP_MAX_WIDTH, available_width))
        self.drop_area.set_display_mode(compact=compact)
        self.queue_layout.invalidate()

    def set_preparing_jobs(
        self,
        job_ids: tuple[str, ...],
        details: dict[str, str] | None = None,
        *,
        can_pause: bool = False,
    ) -> None:
        self._preparing_job_ids = tuple(job_ids)
        self._preparing_can_pause = bool(job_ids) and can_pause
        self.job_table.set_preparing_jobs(self._preparing_job_ids, details)
        self._refresh_queue_commands()

    def set_preflight(
        self,
        forecasts: dict[str, DocumentPreflight],
        queue_preflight: QueuePreflight | None,
    ) -> None:
        """Expose a content-free prediction without changing queue state."""

        self._forecasts = dict(forecasts)
        self._queue_preflight = queue_preflight
        self.job_table.set_forecasts(self._forecasts)
        self._refresh_queue_commands()

    def set_runtime_estimates(
        self,
        estimates: dict[str, RuntimeEstimate],
    ) -> None:
        self._runtime_estimates = dict(estimates)
        self.job_table.set_runtime_estimates(self._runtime_estimates)

    def set_integrity_reports(
        self,
        reports: dict[str, FinalIntegrityReport],
    ) -> None:
        self._integrity_reports = dict(reports)
        self.job_table.set_integrity_reports(self._integrity_reports)

    def set_output_directory(self, directory: Path | None) -> None:
        """Show the remembered global destination without exposing a full path."""

        self._output_directory = directory
        if directory is None:
            label = "Junto al original"
            tooltip = "Cada resultado se guardará junto a su documento original."
        else:
            label = directory.name or str(directory)
            tooltip = str(directory)
        mode = self._layout_mode or "wide"
        if mode == "compact":
            text = ""
            self.output_directory_button.setFixedSize(38, 38)
            self.output_directory_button.setMaximumWidth(38)
        else:
            text = (
                self.output_directory_button.fontMetrics().elidedText(
                    label,
                    Qt.TextElideMode.ElideMiddle,
                    150,
                )
                if mode == "medium"
                else f"Guardar en · {label}"
            )
            self.output_directory_button.setMinimumWidth(0)
            self.output_directory_button.setMaximumWidth(180 if mode == "medium" else 300)
            self.output_directory_button.setMinimumHeight(38)
            self.output_directory_button.setMaximumHeight(38)
        self.output_directory_button.setText(text)
        self.output_directory_button.setToolTip(tooltip)
        self.output_directory_button.setAccessibleName(
            f"Cambiar dónde se guardan los resultados. {tooltip}"
        )

    def set_local_ai_status(
        self,
        status: OllamaStatus | None,
        model: str | None,
    ) -> None:
        """Expose readiness without treating an unused IA setup as an error."""

        self._local_ai_status = status
        self._local_ai_model = model

    def _show_output_directory_menu(self) -> None:
        menu = QMenu(self)
        choose = menu.addAction("Elegir otra carpeta…")
        reset = (
            menu.addAction("Guardar junto al documento original")
            if self._output_directory is not None
            else None
        )
        position = self.output_directory_button.mapToGlobal(
            self.output_directory_button.rect().bottomLeft()
        )
        selected = menu.exec(position)
        if selected is choose:
            self.output_directory_requested.emit()
        elif reset is not None and selected is reset:
            self.output_directory_reset_requested.emit()

    def set_recovery_warning(self, message: str | None) -> None:
        self.recovery_warning.setText(message or "")
        self.recovery_warning.setToolTip(message or "")
        self.recovery_warning.setAccessibleName(message or "Recuperación automática disponible")
        self.recovery_warning.setVisible(bool(message))

    def show_batch_summary(
        self,
        message: str,
        *,
        tone: str,
        activity_available: bool = True,
        job_ids: tuple[str, ...] = (),
    ) -> None:
        self._batch_message_job_ids = frozenset(job_ids)
        self.batch_message.show_message(
            message,
            tone=tone,
            action_label="Ver actividad" if activity_available else None,
        )

    def clear_batch_summary(self) -> None:
        self._batch_message_job_ids = frozenset()
        self.batch_message.hide()

    def clear_job_error(self, job_id: str | None = None) -> None:
        if job_id is not None and self._message_job_id != job_id:
            return
        self._message_job_id = None
        self._message_stage = None
        self._message_primary_action = None
        self._message_secondary_action = None
        self.job_message.hide()

    def _reconcile_contextual_messages(self) -> None:
        valid_job_ids = frozenset(job.id for job in self._jobs)
        if self._batch_message_job_ids and not self._batch_message_job_ids.issubset(valid_job_ids):
            self.clear_batch_summary()
        if self._message_job_id is not None and self._message_job_id not in valid_job_ids:
            self.clear_job_error()
        if not valid_job_ids:
            self.clear_batch_summary()
            self.clear_job_error()

    def show_job_error(
        self,
        job_id: str,
        plan: RecoveryPlan,
        stage: StageKind,
    ) -> None:
        self._message_job_id = job_id
        self._message_stage = stage
        self._message_primary_action = plan.primary_action
        self._message_secondary_action = plan.secondary_action
        self.job_message.show_message(
            f"Error en {_stage_label(stage)}. {plan.title}. "
            f"{plan.explanation} {plan.preserved_work}",
            tone="error",
            action_label=plan.primary_label,
            secondary_action_label=plan.secondary_label,
        )
        self.job_message.setFocus(Qt.FocusReason.OtherFocusReason)

    def _run_primary_recovery(self) -> None:
        self._run_recovery(self._message_primary_action)

    def _run_secondary_recovery(self) -> None:
        self._run_recovery(self._message_secondary_action)

    def _run_recovery(self, action: RecoveryAction | None) -> None:
        if self._message_job_id is None:
            return
        job_id = self._message_job_id
        stage = self._message_stage
        self.clear_job_error()
        if action is RecoveryAction.RETRY:
            self.retry_requested.emit(job_id)
        elif action is RecoveryAction.LOCAL_AI:
            self.local_ai_requested.emit()
        elif action is RecoveryAction.CONFIGURE:
            self.configure_requested.emit(job_id, stage)

    def show_internal_view(
        self,
        widget: QWidget,
        title: str,
        *,
        scroll: bool = False,
    ) -> None:
        """Present a workflow inside the main window."""

        previous = self.content_stack.currentWidget()
        focus_target = self.focusWidget()
        page = QWidget(self.content_stack)
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)
        header = QFrame(page)
        header.setObjectName("internalPageHeader")
        header.setMaximumWidth(_CONTENT_RAIL_MAX_WIDTH)
        header.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(8, 6, 8, 10)
        back = InternalBackButton(header)
        back.setAccessibleName("Volver")
        back.setToolTip("Volver")
        back.setIcon(back_icon())
        back.setCursor(Qt.CursorShape.PointingHandCursor)
        back.clicked.connect(lambda: self.internal_back_requested.emit(widget))
        header_layout.addWidget(back)
        heading = QLabel(title, header)
        heading.setObjectName("internalPageTitle")
        header_layout.addWidget(heading)
        header_layout.addStretch(1)
        settings = QPushButton(header)
        settings.setObjectName("globalMenu")
        settings.setAccessibleName("Abrir ajustes y actividad")
        settings.setToolTip("Ajustes y actividad")
        settings.setIcon(settings_icon())
        settings.setIconSize(QSize(20, 20))
        settings.setFixedSize(42, 42)
        settings.setCursor(Qt.CursorShape.PointingHandCursor)
        settings.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        settings.clicked.connect(self.settings_requested)
        header_layout.addWidget(settings)
        root_margins = self.root_layout.contentsMargins()
        header.setFixedWidth(
            min(
                _CONTENT_RAIL_MAX_WIDTH,
                max(1, self.width() - root_margins.left() - root_margins.right()),
            )
        )
        page_layout.addWidget(header, 0, Qt.AlignmentFlag.AlignHCenter)
        if scroll:
            viewport = QScrollArea(page)
            viewport.setWidgetResizable(True)
            viewport.setFrameShape(QFrame.Shape.NoFrame)
            viewport.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            widget.setParent(viewport)
            viewport.setWidget(widget)
            page_layout.addWidget(viewport, 1)
        else:
            widget.setParent(page)
            page_layout.addWidget(widget, 1)
        self.content_stack.addWidget(page)
        self._internal_pages[widget] = (
            page,
            previous,
            focus_target,
        )
        self._internal_settings_buttons[widget] = settings
        self._refresh_settings_attention()
        self.current_internal_widget = widget
        self.content_stack.setCurrentWidget(page)
        self.app_header.hide()
        self.primary_button.hide()
        widget.show()
        if hasattr(widget, "set_compact_mode"):
            widget.set_compact_mode(bool(self._compact_layout))
        QTimer.singleShot(0, back.setFocus)

    def close_internal_view(self, widget: QWidget) -> None:
        """Return from one internal workflow to the page that opened it."""

        record = self._internal_pages.pop(widget, None)
        if record is None:
            return
        page, previous, focus_target = record
        self._internal_settings_buttons.pop(widget, None)
        if self.content_stack.currentWidget() is page:
            self.content_stack.setCurrentWidget(previous)
        self.content_stack.removeWidget(page)
        widget.hide()
        widget.setParent(self)
        page.deleteLater()
        if previous is self.queue_pane:
            self.app_header.show()
        else:
            self.app_header.hide()
        if previous is self.queue_pane:
            self._refresh_queue_commands()
        else:
            self.primary_button.hide()
        previous_internal = next(
            (
                candidate
                for candidate, (candidate_page, *_rest) in self._internal_pages.items()
                if candidate_page is previous
            ),
            None,
        )
        self.current_internal_widget = previous_internal
        self._restore_workflow_focus(focus_target)

    def settings_anchor(self) -> QPushButton:
        """Return the settings control visible in the current page header."""

        current = self.current_internal_widget
        if current is not None:
            button = self._internal_settings_buttons.get(current)
            if button is not None:
                return button
        return self.settings_button

    def _restore_workflow_focus(self, target: QWidget | None) -> None:
        if target is None or not target.isVisible():
            target = self.job_table if self._jobs else self.drop_area
        QTimer.singleShot(0, target.setFocus)

    def set_configuring(self, job_id: str | None, stage: StageKind | None) -> None:
        self.job_table.set_configuring(job_id, stage)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        mime = event.mimeData()
        if mime.hasUrls() and any(url.isLocalFile() for url in mime.urls()):
            self._set_drag_active(True)
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:  # noqa: N802
        self._set_drag_active(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        paths = tuple(
            Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()
        )
        self._set_drag_active(False)
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()

    def _set_drag_active(self, active: bool) -> None:
        self.drop_area.setProperty("dragActive", active)
        self.drop_area.style().unpolish(self.drop_area)
        self.drop_area.style().polish(self.drop_area)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(1380, 800)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply_responsive_layout(event.size().width())
        self._apply_queue_state_layout()

    def _apply_responsive_layout(self, width: int) -> None:
        mode = (
            "compact"
            if width <= BREAKPOINTS.compact
            else "medium"
            if width <= BREAKPOINTS.medium
            else "wide"
        )
        if self._layout_mode == mode:
            return
        self._layout_mode = mode
        compact = mode == "compact"
        medium = mode == "medium"
        self._compact_layout = compact
        for widget in (
            self.logo,
            self.output_directory_button,
            self.add_button,
            self.primary_button,
            self.settings_button,
        ):
            self.header_layout.removeWidget(widget)
        for column in range(7):
            self.header_layout.setColumnStretch(column, 0)
        if compact:
            self.root_layout.setContentsMargins(
                SPACING.md,
                SPACING.sm,
                SPACING.md,
                SPACING.md,
            )
            self.header_layout.setContentsMargins(0, SPACING.xs, 0, SPACING.xs)
            self.header_layout.setHorizontalSpacing(SPACING.xs)
            self.header_layout.setVerticalSpacing(0)
            self.header_layout.addWidget(self.logo, 0, 0)
            self.header_layout.setColumnStretch(1, 1)
            self.header_layout.addWidget(self.output_directory_button, 0, 2)
            self.header_layout.addWidget(self.add_button, 0, 3)
            self.header_layout.addWidget(self.primary_button, 0, 4)
            self.header_layout.addWidget(self.settings_button, 0, 5)
        elif medium:
            self.root_layout.setContentsMargins(
                SPACING.lg,
                SPACING.sm,
                SPACING.lg,
                SPACING.md,
            )
            self.header_layout.setContentsMargins(0, SPACING.xs, 0, SPACING.xs)
            self.header_layout.setHorizontalSpacing(SPACING.sm)
            self.header_layout.setVerticalSpacing(0)
            self.header_layout.addWidget(self.logo, 0, 0)
            self.header_layout.setColumnStretch(1, 1)
            self.header_layout.addWidget(self.output_directory_button, 0, 2)
            self.header_layout.addWidget(self.add_button, 0, 3)
            self.header_layout.addWidget(self.primary_button, 0, 4)
            self.header_layout.addWidget(self.settings_button, 0, 5)
        else:
            self.root_layout.setContentsMargins(24, 16, 24, 20)
            self.header_layout.setContentsMargins(8, 2, 0, 2)
            self.header_layout.setSpacing(8)
            self.header_layout.addWidget(self.logo, 0, 0)
            self.header_layout.addWidget(self.output_directory_button, 0, 2)
            self.header_layout.setColumnStretch(1, 1)
            self.header_layout.addWidget(self.add_button, 0, 3)
            self.header_layout.addWidget(self.primary_button, 0, 4)
            self.header_layout.addWidget(self.settings_button, 0, 5)
        self._apply_logo()
        self.set_output_directory(self._output_directory)
        self._apply_header_action_layout(compact=compact)
        self._refresh_queue_commands()
        self.job_table.set_compact_mode(compact or medium)
        self._apply_queue_state_layout()
        self.batch_message.set_compact_mode(compact)
        self.job_message.set_compact_mode(compact)
        current = self.current_internal_widget
        if current is not None and hasattr(current, "set_compact_mode"):
            current.set_compact_mode(compact)

    def _apply_header_action_layout(self, *, compact: bool) -> None:
        if compact:
            self.add_button.setText("")
            self.primary_button.setText("")
            self.add_button.setFixedSize(38, 38)
            self.primary_button.setFixedSize(38, 38)
            return
        self.add_button.setText("Añadir")
        self.add_button.setMinimumWidth(0)
        self.add_button.setMaximumWidth(16777215)
        self.add_button.setFixedHeight(38)
        self.primary_button.setMinimumWidth(156)
        self.primary_button.setMaximumWidth(16777215)
        self.primary_button.setFixedHeight(38)
        self.primary_button.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Fixed,
        )

    def _refresh_queue_commands(self) -> None:
        view = queue_header_view(self._jobs)
        if view.review_count:
            self.queue_summary.setText(view.summary)
            self.queue_summary.setTextFormat(Qt.TextFormat.PlainText)
        else:
            summary = view.summary
            if view.primary_mode == "process" and self._queue_preflight is not None:
                summary = f"{summary} · ~{format_duration_range(self._queue_preflight.estimate)}"
            self.queue_summary.setText(summary)
            self.queue_summary.setTextFormat(Qt.TextFormat.PlainText)
        if self._preparing_job_ids:
            self._primary_mode = "pause" if self._preparing_can_pause else None
            self._pause_feedback_pending = False
            self.primary_button.setVisible(True)
            self.primary_button.setEnabled(self._preparing_can_pause)
            label = "Pausar" if self._preparing_can_pause else "Preparando…"
            self.primary_button.setText("" if self._compact_layout else label)
            self.primary_button.setToolTip(label)
            self.primary_button.setAccessibleName(label)
            self.primary_button.setIcon(pause_icon() if self._preparing_can_pause else QIcon())
            return
        self._primary_mode = view.primary_mode
        self.primary_button.setVisible(view.primary_mode is not None)
        if view.primary_mode != "pause":
            self._pause_feedback_pending = False
        self.primary_button.setEnabled(
            view.primary_mode is not None and not self._pause_feedback_pending
        )
        label = (
            "Pausando…"
            if view.primary_mode == "pause" and self._pause_feedback_pending
            else view.primary_label or ""
        )
        self.primary_button.setText("" if self._compact_layout else label)
        self.primary_button.setToolTip(label)
        self.primary_button.setAccessibleName(label)
        self.primary_button.setIcon(
            play_icon()
            if view.primary_mode == "process"
            else pause_icon()
            if view.primary_mode == "pause"
            else QIcon()
        )

    def _emit_primary_action(self) -> None:
        if self._primary_mode is not None:
            if self._primary_mode == "pause":
                self._pause_feedback_pending = True
                self.primary_button.setEnabled(False)
                self.primary_button.setText("" if self._compact_layout else "Pausando…")
                self.primary_button.setToolTip("Pausando…")
                self.primary_button.setAccessibleName("Pausando…")
                QTimer.singleShot(2500, self._clear_pause_feedback)
            self.primary_requested.emit(self._primary_mode)

    def _clear_pause_feedback(self) -> None:
        if self._pause_feedback_pending:
            self._pause_feedback_pending = False
            self._refresh_queue_commands()

    def _apply_local_styles(self) -> None:
        self.setStyleSheet(
            f"""
            QWidget#appHeader {{
                background-color: transparent;
            }}
            QLabel#queueSummary {{
                color: {COLORS.text_secondary};
                font-size: 10pt;
            }}
            QFrame#documentDropArea {{
                color: {COLORS.text_primary};
                background-color: {COLORS.surface_raised};
                border: 1px solid {COLORS.divider};
                border-radius: 8px;
            }}
            QFrame#documentDropArea:hover {{
                border: 1px dashed {COLORS.action_primary};
                background-color: {COLORS.action_primary_soft};
            }}
            QFrame#documentDropArea[dragActive="true"] {{
                border: 2px dashed {COLORS.action_primary};
                background-color: {COLORS.action_primary_soft};
            }}
            QFrame#jobTablePanel {{
                background-color: {COLORS.surface};
                border: none;
                border-radius: 11px;
            }}
            QLabel#dropAreaPrimary {{
                color: {COLORS.text_primary};
                font-size: 10.5pt;
                font-weight: 550;
            }}
            QLabel#dropAreaSecondary {{
                color: {COLORS.text_secondary};
                font-size: 9pt;
            }}
            QPushButton#dropBrowseButton,
            QPushButton#queueAddAction {{
                min-height: 38px;
                max-height: 38px;
                color: {COLORS.text_primary};
                background-color: transparent;
                border: 1px solid {COLORS.divider};
                border-radius: 7px;
                font-weight: 550;
            }}
            QPushButton#queueAddAction {{
                padding: 0 10px;
            }}
            QPushButton#dropBrowseButton:hover,
            QPushButton#dropBrowseButton:focus,
            QPushButton#queueAddAction:hover,
            QPushButton#queueAddAction:focus {{
                color: {COLORS.action_primary};
                background-color: {COLORS.surface_hover};
                border-color: {COLORS.action_primary};
            }}
            QLabel#recoveryWarning {{
                color: {COLORS.warning};
                font-size: 9.5pt;
            }}
            QScrollArea#configurationInspector {{
                background-color: {COLORS.canvas};
                border: none;
            }}
            QFrame#internalPageHeader {{
                background-color: transparent;
                border: none;
                border-bottom: 1px solid {COLORS.divider};
            }}
            QLabel#internalPageTitle {{
                color: {COLORS.text_primary};
                font-size: 13pt;
                font-weight: 650;
            }}
            QPushButton#internalBack {{
                min-width: 38px;
                max-width: 38px;
                min-height: 34px;
                max-height: 34px;
                padding: 0;
                background-color: transparent;
                border: none;
                border-radius: 8px;
            }}
            QPushButton#internalBack:hover {{
                background-color: transparent;
                border: none;
            }}
            QPushButton#internalBack:focus {{
                background-color: transparent;
                border: none;
            }}
            QPushButton#globalOutputDirectory {{
                min-height: 38px;
                max-height: 38px;
                color: {COLORS.text_secondary};
                background-color: transparent;
                border-color: transparent;
            }}
            QPushButton#globalOutputDirectory:hover,
            QPushButton#globalMenu:hover {{
                color: {COLORS.text_primary};
                background-color: {COLORS.surface_hover};
                border-color: transparent;
            }}
            QPushButton#globalOutputDirectory:focus,
            QPushButton#globalMenu:focus {{
                background-color: {COLORS.surface_hover};
                border-color: transparent;
            }}
            QPushButton#globalMenu {{
                padding: 0;
                background-color: transparent;
                border-color: transparent;
            }}
            """
        )
