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
    QKeyEvent,
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
from parsezen.application.recovery import RecoveryAction, RecoveryPlan
from parsezen.branding import BRAND_DARK_LOGO_PATH, BRAND_LOGO_PATH
from parsezen.domain.jobs import DocumentJob
from parsezen.domain.stages import StageKind
from parsezen.final_integrity import FinalIntegrityReport
from parsezen.local_models import OllamaStatus
from parsezen.presentation.components import StatusMessage
from parsezen.presentation.design_system import (
    BREAKPOINTS,
    COLORS,
    SPACING,
    ThemeMode,
    back_icon,
    cloud_upload_icon,
    current_theme_mode,
    folder_icon,
    local_ai_icon,
    pause_icon,
    play_icon,
    theme_toggle_icon,
)
from parsezen.presentation.job_table import JobTableView
from parsezen.presentation.job_view_model import queue_header_view


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
    """Compact, keyboard-operable drop target with one explicit browse link."""

    activated = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("documentDropArea")
        self.setAccessibleName("Añadir documentos TXT, Markdown, Word, PDF o EPUB")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(76)

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
        self.text_host.setMinimumWidth(280)
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
        self.primary_label.setTextFormat(Qt.TextFormat.RichText)
        self.primary_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByKeyboard
        )
        self.primary_label.setOpenExternalLinks(False)
        self.primary_label.linkActivated.connect(lambda _link: self.activated.emit())
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
        layout.addStretch(1)
        self.refresh_theme()

    def refresh_theme(self) -> None:
        self.icon_label.setPixmap(cloud_upload_icon().pixmap(30, 30))
        self.primary_label.setText(
            'Arrastra documentos aquí o <a href="browse" style="color:'
            f'{COLORS.info}; text-decoration:underline">examínalos</a>'
        )

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() is Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.activated.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space}:
            self.activated.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class ParsezenWorkspace(QWidget):
    add_requested = Signal()
    files_dropped = Signal(object)
    settings_requested = Signal()
    local_ai_requested = Signal()
    theme_toggle_requested = Signal()
    configure_requested = Signal(str, object)
    review_requested = Signal(str, object)
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
        self._compact_layout: bool | None = None
        self._configuration_focus_target: QWidget | None = None

        layout = QVBoxLayout(self)
        self.root_layout = layout
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        layout.setContentsMargins(24, 16, 24, 20)
        layout.setSpacing(16)

        self.app_header = QWidget(self)
        self.app_header.setObjectName("appHeader")
        header_layout = QGridLayout(self.app_header)
        self.header_layout = header_layout
        header_layout.setContentsMargins(8, 2, 0, 2)
        header_layout.setSpacing(8)
        self.logo = QLabel(self.app_header)
        self.logo.setObjectName("brandLogo")
        self.logo.setAccessibleName(APP_DISPLAY_NAME)
        self._apply_logo()
        header_layout.addWidget(self.logo, 0, 0)
        self.header_separator = QFrame(self.app_header)
        self.header_separator.setObjectName("headerSeparator")
        self.header_separator.setFrameShape(QFrame.Shape.VLine)
        self.header_separator.setFixedHeight(32)
        header_layout.addWidget(self.header_separator, 0, 1)
        self.queue_summary = QLabel("0 documentos", self.app_header)
        self.queue_summary.setObjectName("queueSummary")
        self.queue_summary.setWordWrap(True)
        self.queue_summary.setMinimumWidth(0)
        self.queue_summary.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Preferred,
        )
        header_layout.addWidget(self.queue_summary, 0, 2)
        header_layout.setColumnStretch(3, 1)

        self.local_ai_button = QPushButton("IA local", self.app_header)
        self.local_ai_button.setObjectName("localAiSettings")
        self.local_ai_button.setAccessibleName("Configurar la IA local")
        self.local_ai_button.setIcon(local_ai_icon())
        self.local_ai_button.setIconSize(QSize(19, 19))
        self.local_ai_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.local_ai_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.local_ai_button.clicked.connect(self.local_ai_requested)
        header_layout.addWidget(self.local_ai_button, 0, 4)

        self.output_directory_button = QPushButton("Destino: Original", self.app_header)
        self.output_directory_button.setObjectName("globalOutputDirectory")
        self.output_directory_button.setAccessibleName("Cambiar la carpeta de destino")
        self.output_directory_button.setIcon(folder_icon())
        self.output_directory_button.setIconSize(QSize(19, 19))
        self.output_directory_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.output_directory_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.output_directory_button.clicked.connect(self._show_output_directory_menu)
        header_layout.addWidget(self.output_directory_button, 0, 5)

        self.theme_button = QPushButton(self.app_header)
        self.theme_button.setObjectName("themeToggle")
        self.theme_button.setAccessibleName("Elegir apariencia: sistema, claro u oscuro")
        self.theme_button.setToolTip("Cambiar apariencia")
        self.theme_button.setIcon(theme_toggle_icon())
        self.theme_button.setIconSize(QSize(20, 20))
        self.theme_button.setFixedSize(42, 42)
        self.theme_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.theme_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.theme_button.clicked.connect(self.theme_toggle_requested)
        header_layout.addWidget(self.theme_button, 0, 6)

        self.primary_button = QPushButton(self.app_header)
        self.primary_button.setObjectName("primaryAction")
        self.primary_button.setMinimumWidth(156)
        self.primary_button.setIconSize(QSize(18, 18))
        self.primary_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.primary_button.clicked.connect(self._emit_primary_action)
        self.primary_button.hide()
        header_layout.addWidget(self.primary_button, 0, 7)
        layout.addWidget(self.app_header)

        self.content_stack = QStackedWidget(self)
        self.content_stack.setObjectName("workspacePages")

        self.queue_pane = QWidget(self.content_stack)
        queue_layout = QVBoxLayout(self.queue_pane)
        queue_layout.setContentsMargins(0, 0, 0, 0)
        queue_layout.setSpacing(16)
        self.table_panel = RoundedTablePanel(self.queue_pane)
        self.table_panel.setObjectName("jobTablePanel")
        table_layout = QVBoxLayout(self.table_panel)
        table_layout.setContentsMargins(1, 1, 1, 1)
        table_layout.setSpacing(0)
        self.job_table = JobTableView(self.table_panel)
        self.job_table.configure_requested.connect(self.configure_requested)
        self.job_table.review_requested.connect(self.review_requested)
        self.job_table.error_requested.connect(self.error_requested)
        self.job_table.open_result_requested.connect(self.open_result_requested)
        self.job_table.open_folder_requested.connect(self.open_folder_requested)
        self.job_table.result_summary_requested.connect(self.result_summary_requested)
        self.job_table.remove_requested.connect(self.remove_requested)
        self.job_table.move_requested.connect(self.move_requested)
        table_layout.addWidget(self.job_table)
        queue_layout.addWidget(self.table_panel)

        self.drop_area = DocumentDropArea(self.queue_pane)
        self.drop_area.activated.connect(self.add_requested)
        self.add_button = self.drop_area
        queue_layout.addWidget(self.drop_area)
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
        queue_layout.addStretch(1)
        self.content_stack.addWidget(self.queue_pane)

        self.configuration_page = QWidget(self.content_stack)
        configuration_page_layout = QVBoxLayout(self.configuration_page)
        configuration_page_layout.setContentsMargins(0, 0, 0, 0)
        configuration_page_layout.setSpacing(0)
        configuration_header = QFrame(self.configuration_page)
        configuration_header.setObjectName("internalPageHeader")
        configuration_header_layout = QHBoxLayout(configuration_header)
        configuration_header_layout.setContentsMargins(8, 6, 8, 10)
        self.configuration_back_button = InternalBackButton(configuration_header)
        self.configuration_back_button.setAccessibleName(
            "Volver a la cola y guardar la configuración"
        )
        self.configuration_back_button.setToolTip("Volver a la cola y guardar la configuración")
        self.configuration_back_button.setIcon(back_icon())
        self.configuration_back_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.configuration_back_button.clicked.connect(
            lambda: self.internal_back_requested.emit(self.current_internal_widget)
        )
        configuration_header_layout.addWidget(self.configuration_back_button)
        self.configuration_title = QLabel("Configurar documento", configuration_header)
        self.configuration_title.setObjectName("internalPageTitle")
        self.configuration_title.setWordWrap(True)
        self.configuration_title.setMinimumWidth(0)
        self.configuration_title.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        configuration_header_layout.addWidget(self.configuration_title, 1)
        configuration_page_layout.addWidget(configuration_header)

        self.configuration_scroll = QScrollArea(self.configuration_page)
        self.configuration_scroll.setObjectName("configurationInspector")
        self.configuration_scroll.setWidgetResizable(True)
        self.configuration_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.configuration_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.configuration_host = QWidget(self.configuration_scroll)
        self.configuration_layout = QVBoxLayout(self.configuration_host)
        self.configuration_layout.setContentsMargins(24, 16, 24, 20)
        self.configuration_layout.setSpacing(12)
        self.configuration_scroll.setWidget(self.configuration_host)
        configuration_page_layout.addWidget(self.configuration_scroll, 1)
        self.content_stack.addWidget(self.configuration_page)
        self.content_stack.setCurrentWidget(self.queue_pane)
        self._internal_pages: dict[
            QWidget,
            tuple[QWidget, QWidget, bool, QWidget | None],
        ] = {}
        self.current_internal_widget: QWidget | None = None
        layout.addWidget(self.content_stack, 1)

        self._apply_local_styles()
        self.set_jobs(())

    def apply_theme(self) -> None:
        """Refresh controls whose colors or assets are created at runtime."""

        self._apply_logo()
        self.local_ai_button.setIcon(local_ai_icon())
        self.output_directory_button.setIcon(folder_icon())
        self.theme_button.setIcon(theme_toggle_icon())
        self.configuration_back_button.setIcon(back_icon())
        self.drop_area.refresh_theme()
        self._apply_local_styles()
        self._refresh_header()
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
        target_width = 96 if self._compact_layout else 132
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
        self.job_table.set_jobs(self._jobs)
        self.table_panel.setVisible(bool(self._jobs))
        self._refresh_header()

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
        self._refresh_header()

    def set_preflight(
        self,
        forecasts: dict[str, DocumentPreflight],
        queue_preflight: QueuePreflight | None,
    ) -> None:
        """Expose a content-free prediction without changing queue state."""

        self._forecasts = dict(forecasts)
        self._queue_preflight = queue_preflight
        self.job_table.set_forecasts(self._forecasts)
        self._refresh_header()

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
            label = "Original"
            tooltip = "Cada resultado se guardará junto a su documento original."
        else:
            label = directory.name or str(directory)
            tooltip = str(directory)
        self.output_directory_button.setText(label if self._compact_layout else f"Destino: {label}")
        self.output_directory_button.setToolTip(tooltip)

    def set_local_ai_status(
        self,
        status: OllamaStatus | None,
        model: str | None,
    ) -> None:
        """Expose readiness without treating an unused IA setup as an error."""

        self._local_ai_status = status
        self._local_ai_model = model
        label = {
            None: "Sin comprobar",
            OllamaStatus.READY: "Lista",
            OllamaStatus.NOT_INSTALLED: "No configurada",
            OllamaStatus.STOPPED: "Detenida",
            OllamaStatus.MISSING_MODEL: "Sin modelo",
            OllamaStatus.LOCAL_ONLY_REQUIRED: "Revisar privacidad",
            OllamaStatus.UNAVAILABLE: "No disponible",
        }[status]
        compact_text = "IA"
        full_text = f"IA local · {label}"
        self.local_ai_button.setText(compact_text if self._compact_layout else full_text)
        details = f"Modelo predeterminado: {model}." if model else "Sin modelo predeterminado."
        self.local_ai_button.setToolTip(f"IA local: {label}. {details}")
        self.local_ai_button.setAccessibleName(f"Abrir IA local. Estado: {label}. {details}")

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
    ) -> None:
        self.batch_message.show_message(
            message,
            tone=tone,
            action_label="Ver actividad" if activity_available else None,
        )

    def clear_batch_summary(self) -> None:
        self.batch_message.hide()

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
            f"{plan.title}. {plan.explanation} {plan.preserved_work}",
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
        self.job_message.hide()
        if action is RecoveryAction.RETRY:
            self.retry_requested.emit(self._message_job_id)
        elif action is RecoveryAction.LOCAL_AI:
            self.local_ai_requested.emit()
        elif action is RecoveryAction.CONFIGURE:
            self.configure_requested.emit(self._message_job_id, self._message_stage)

    def show_configuration_panel(
        self,
        panel: QWidget,
        title: str = "Configurar documento",
    ) -> None:
        """Open the complete document configuration as an internal page."""

        self.close_configuration_panel()
        self._configuration_focus_target = self.focusWidget()
        panel.setParent(self.configuration_host)
        self.configuration_layout.addWidget(panel)
        panel.show()
        self.configuration_title.setText(title)
        self.current_internal_widget = panel
        self.configuration_scroll.show()
        self.content_stack.setCurrentWidget(self.configuration_page)
        if hasattr(panel, "set_compact_mode"):
            panel.set_compact_mode(bool(self._compact_layout))
        QTimer.singleShot(0, self.configuration_back_button.setFocus)

    def close_configuration_panel(self) -> None:
        had_configuration = (
            self.configuration_layout.count() > 0
            or self.content_stack.currentWidget() is self.configuration_page
        )
        while self.configuration_layout.count():
            item = self.configuration_layout.takeAt(0)
            if item is None:
                continue
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        if self.content_stack.currentWidget() is self.configuration_page:
            self.content_stack.setCurrentWidget(self.queue_pane)
        self.configuration_scroll.hide()
        self.current_internal_widget = None
        if had_configuration:
            target = self._configuration_focus_target
            self._configuration_focus_target = None
            self._restore_workflow_focus(target)

    def show_internal_view(
        self,
        widget: QWidget,
        title: str,
        *,
        scroll: bool = False,
        replace_app_header: bool = False,
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
        page_layout.addWidget(header)
        if scroll:
            viewport = QScrollArea(page)
            viewport.setWidgetResizable(True)
            viewport.setFrameShape(QFrame.Shape.NoFrame)
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
            replace_app_header,
            focus_target,
        )
        self.current_internal_widget = widget
        self.content_stack.setCurrentWidget(page)
        self.app_header.setVisible(not replace_app_header)
        widget.show()
        if hasattr(widget, "set_compact_mode"):
            widget.set_compact_mode(bool(self._compact_layout))
        QTimer.singleShot(0, back.setFocus)

    def close_internal_view(self, widget: QWidget) -> None:
        """Return from one internal workflow to the page that opened it."""

        record = self._internal_pages.pop(widget, None)
        if record is None:
            return
        page, previous, replaced_header, focus_target = record
        if self.content_stack.currentWidget() is page:
            self.content_stack.setCurrentWidget(previous)
        self.content_stack.removeWidget(page)
        widget.hide()
        widget.setParent(self)
        page.deleteLater()
        if replaced_header:
            self.app_header.show()
        if previous is self.configuration_page:
            item = self.configuration_layout.itemAt(0)
            self.current_internal_widget = item.widget() if item is not None else None
        else:
            self.current_internal_widget = None
        self._restore_workflow_focus(focus_target)

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
        self._apply_responsive_layout(event.size().width() <= BREAKPOINTS.compact)

    def _apply_responsive_layout(self, compact: bool) -> None:
        if self._compact_layout is compact:
            return
        self._compact_layout = compact
        for widget in (
            self.logo,
            self.header_separator,
            self.queue_summary,
            self.local_ai_button,
            self.output_directory_button,
            self.theme_button,
            self.primary_button,
        ):
            self.header_layout.removeWidget(widget)
        for column in range(8):
            self.header_layout.setColumnStretch(column, 0)
        if compact:
            self.root_layout.setContentsMargins(
                SPACING.md,
                SPACING.sm,
                SPACING.md,
                SPACING.md,
            )
            self.header_layout.setContentsMargins(0, SPACING.xs, 0, SPACING.xs)
            self.header_layout.setHorizontalSpacing(SPACING.sm)
            self.header_layout.setVerticalSpacing(SPACING.sm)
            self.header_layout.addWidget(self.logo, 0, 0)
            self.header_layout.addWidget(self.queue_summary, 0, 1)
            self.header_layout.addWidget(self.theme_button, 0, 2)
            self.header_layout.addWidget(self.local_ai_button, 1, 0)
            self.header_layout.addWidget(self.output_directory_button, 1, 1, 1, 2)
            self.header_layout.addWidget(self.primary_button, 2, 0, 1, 3)
            self.header_layout.setColumnStretch(1, 1)
            self.header_separator.hide()
            self.set_local_ai_status(self._local_ai_status, self._local_ai_model)
            self._apply_logo()
            self.set_output_directory(self._output_directory)
            self.primary_button.setMinimumWidth(0)
            self.drop_area.setFixedHeight(96)
            self.drop_area.content_layout.setDirection(QBoxLayout.Direction.TopToBottom)
            self.drop_area.content_layout.setContentsMargins(12, 10, 12, 10)
            self.drop_area.content_layout.setSpacing(SPACING.xs)
            self.drop_area.content_layout.setAlignment(
                self.drop_area.icon_label,
                Qt.AlignmentFlag.AlignHCenter,
            )
            self.drop_area.text_host.setMinimumWidth(0)
            self.drop_area.primary_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.drop_area.secondary_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.configuration_layout.setContentsMargins(
                SPACING.md,
                SPACING.sm,
                SPACING.md,
                SPACING.md,
            )
        else:
            self.root_layout.setContentsMargins(24, 16, 24, 20)
            self.header_layout.setContentsMargins(8, 2, 0, 2)
            self.header_layout.setSpacing(8)
            self.header_layout.addWidget(self.logo, 0, 0)
            self.header_layout.addWidget(self.header_separator, 0, 1)
            self.header_layout.addWidget(self.queue_summary, 0, 2)
            self.header_layout.addWidget(self.local_ai_button, 0, 4)
            self.header_layout.addWidget(self.output_directory_button, 0, 5)
            self.header_layout.addWidget(self.theme_button, 0, 6)
            self.header_layout.addWidget(self.primary_button, 0, 7)
            self.header_layout.setColumnStretch(3, 1)
            self.header_separator.show()
            self.set_local_ai_status(self._local_ai_status, self._local_ai_model)
            self._apply_logo()
            self.set_output_directory(self._output_directory)
            self.primary_button.setMinimumWidth(156)
            self.drop_area.setFixedHeight(76)
            self.drop_area.content_layout.setDirection(QBoxLayout.Direction.LeftToRight)
            self.drop_area.content_layout.setContentsMargins(18, 8, 18, 8)
            self.drop_area.content_layout.setSpacing(12)
            self.drop_area.content_layout.setAlignment(
                self.drop_area.icon_label,
                Qt.AlignmentFlag.AlignVCenter,
            )
            self.drop_area.text_host.setMinimumWidth(280)
            self.drop_area.primary_label.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
            self.drop_area.secondary_label.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
            self.configuration_layout.setContentsMargins(24, 16, 24, 20)
        self.job_table.set_compact_mode(compact)
        self.batch_message.set_compact_mode(compact)
        self.job_message.set_compact_mode(compact)
        current = self.current_internal_widget
        if current is not None and hasattr(current, "set_compact_mode"):
            current.set_compact_mode(compact)

    def _refresh_header(self) -> None:
        view = queue_header_view(self._jobs)
        if view.review_count:
            self.queue_summary.setText(view.summary)
            self.queue_summary.setTextFormat(Qt.TextFormat.PlainText)
        else:
            summary = view.summary
            if view.primary_mode == "process" and self._queue_preflight is not None:
                summary = (
                    f"{summary} · aprox. {format_duration_range(self._queue_preflight.estimate)}"
                )
            self.queue_summary.setText(summary)
            self.queue_summary.setTextFormat(Qt.TextFormat.PlainText)
        if self._preparing_job_ids:
            self._primary_mode = "pause" if self._preparing_can_pause else None
            self._pause_feedback_pending = False
            self.primary_button.setVisible(True)
            self.primary_button.setEnabled(self._preparing_can_pause)
            self.primary_button.setText("Pausar" if self._preparing_can_pause else "Preparando…")
            self.primary_button.setIcon(pause_icon() if self._preparing_can_pause else QIcon())
            return
        self._primary_mode = view.primary_mode
        self.primary_button.setVisible(view.primary_mode is not None)
        if view.primary_mode != "pause":
            self._pause_feedback_pending = False
        self.primary_button.setEnabled(
            view.primary_mode is not None and not self._pause_feedback_pending
        )
        self.primary_button.setText(
            "Pausando…"
            if view.primary_mode == "pause" and self._pause_feedback_pending
            else view.primary_label or ""
        )
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
                self.primary_button.setText("Pausando…")
                QTimer.singleShot(2500, self._clear_pause_feedback)
            self.primary_requested.emit(self._primary_mode)

    def _clear_pause_feedback(self) -> None:
        if self._pause_feedback_pending:
            self._pause_feedback_pending = False
            self._refresh_header()

    def _apply_local_styles(self) -> None:
        self.setStyleSheet(
            f"""
            QWidget#appHeader {{
                background-color: transparent;
            }}
            QFrame#headerSeparator {{
                color: {COLORS.divider};
                background-color: {COLORS.divider};
                max-width: 1px;
            }}
            QLabel#queueSummary {{
                color: {COLORS.text_secondary};
                font-size: 10pt;
            }}
            QFrame#documentDropArea {{
                color: {COLORS.text_primary};
                background-color: {COLORS.surface_subtle};
                border: 1px dashed {COLORS.border};
                border-radius: 8px;
            }}
            QFrame#documentDropArea:hover,
            QFrame#documentDropArea:focus {{
                border-color: {COLORS.action_primary};
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
            QPushButton#globalOutputDirectory,
            QPushButton#localAiSettings {{
                min-height: 38px;
                max-height: 38px;
                color: {COLORS.text_secondary};
                background-color: transparent;
                border-color: transparent;
            }}
            QPushButton#globalOutputDirectory:hover,
            QPushButton#localAiSettings:hover,
            QPushButton#themeToggle:hover {{
                color: {COLORS.text_primary};
                background-color: {COLORS.surface_hover};
                border-color: transparent;
            }}
            QPushButton#globalOutputDirectory:focus,
            QPushButton#localAiSettings:focus,
            QPushButton#themeToggle:focus {{
                background-color: {COLORS.surface_hover};
                border-color: transparent;
            }}
            QPushButton#themeToggle {{
                padding: 0;
                background-color: transparent;
                border-color: transparent;
            }}
            """
        )
