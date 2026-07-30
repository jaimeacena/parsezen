"""Compact model administration for Parsezen's local Ollama integration."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QPainter, QPaintEvent, QPalette, QPen, QPolygonF, QResizeEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLayout,
    QLineEdit,
    QMenu,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from parsezen.local_models import (
    OllamaModel,
    OllamaStatus,
    choose_ollama_model,
    is_reasoning_model_id,
)
from parsezen.model_recommendations import (
    ModelRecommendation,
    ModelRecommendations,
    RecommendationRole,
)
from parsezen.presentation.design_system import BREAKPOINTS, COLORS, SPACING
from parsezen.settings import MAX_CONTEXT_WINDOW, MIN_CONTEXT_WINDOW


class ChevronButton(QPushButton):
    """Shared DPI-aware disclosure button retained for existing components."""

    def __init__(self, *, expanded: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._expanded = expanded
        self.setObjectName("modelChevron")
        self.setFixedSize(32, 32)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        self.update()

    def is_expanded(self) -> bool:
        return self._expanded

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        group = QPalette.ColorGroup.Active if self.isEnabled() else QPalette.ColorGroup.Disabled
        pen = QPen(self.palette().color(group, QPalette.ColorRole.ButtonText), 1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        center_x = self.width() / 2
        center_y = self.height() / 2
        offset = -2.0 if self._expanded else 2.0
        painter.drawPolyline(
            QPolygonF(
                [
                    QPointF(center_x - 4.5, center_y + offset),
                    QPointF(center_x, center_y - offset),
                    QPointF(center_x + 4.5, center_y + offset),
                ]
            )
        )


class TrashButton(QPushButton):
    """Shared accessible vector trash button for model management."""

    def __init__(
        self,
        item_name: str,
        parent: QWidget | None = None,
        *,
        object_name: str = "modelDeleteButton",
        size: int = 40,
        glyph_scale: float = 1.0,
    ) -> None:
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(f"Eliminar {item_name}")
        self.setToolTip(f"Eliminar {item_name}")
        self._glyph_scale = glyph_scale

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        group = QPalette.ColorGroup.Active if self.isEnabled() else QPalette.ColorGroup.Disabled
        pen = QPen(self.palette().color(group, QPalette.ColorRole.ButtonText), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.translate(self.width() / 2, self.height() / 2)
        painter.scale(self._glyph_scale, self._glyph_scale)
        painter.drawLine(QPointF(-7, -6), QPointF(7, -6))
        painter.drawLine(QPointF(-3, -9), QPointF(3, -9))
        painter.drawRoundedRect(QRectF(-5.5, -3.5, 11, 12), 1.5, 1.5)
        painter.drawLine(QPointF(-2, -1), QPointF(-2, 5))
        painter.drawLine(QPointF(2, -1), QPointF(2, 5))


class ModelMenuButton(QPushButton):
    """Crisp vector menu that does not depend on the active text font."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("modelMenu")
        self.setFixedSize(40, 40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        group = QPalette.ColorGroup.Active if self.isEnabled() else QPalette.ColorGroup.Disabled
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.palette().color(group, QPalette.ColorRole.ButtonText))
        center_x = self.width() / 2
        center_y = self.height() / 2
        for offset in (-5.0, 0.0, 5.0):
            painter.drawEllipse(QPointF(center_x, center_y + offset), 1.45, 1.45)
        painter.end()


class ContextWindowSelector(QComboBox):
    """Preset selector retaining the small QSpinBox-compatible API used by callers."""

    valueChanged = Signal(int)
    customRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._maximum = MAX_CONTEXT_WINDOW
        self._last_value = 0
        self.setObjectName("localAiContextWindow")
        self.setAccessibleName("Ventana de contexto de la IA local")
        self.currentIndexChanged.connect(self._emit_value)
        self._rebuild(None)

    def value(self) -> int:
        data = self.currentData()
        return data if isinstance(data, int) else 0

    def setValue(self, value: int) -> None:  # noqa: N802
        normalized = max(0, min(value, self._maximum))
        self._last_value = normalized
        index = self.findData(normalized)
        if index < 0 and normalized:
            self.addItem(_format_tokens(normalized), normalized)
            index = self.findData(normalized)
        self.setCurrentIndex(max(0, index))

    def maximum(self) -> int:
        return self._maximum

    def setMaximum(self, value: int) -> None:  # noqa: N802
        current = self.value()
        self._maximum = max(MIN_CONTEXT_WINDOW, value)
        self._rebuild(current)

    def _rebuild(self, selected: int | None) -> None:
        self.blockSignals(True)
        self.clear()
        self.addItem("Automática", 0)
        for value in (512, 4096, 8192, 16384, 32768):
            if value <= self._maximum:
                self.addItem(_format_tokens(value), value)
        self.insertSeparator(self.count())
        self.addItem("Personalizada…", -1)
        self.blockSignals(False)
        self.setValue(selected or 0)

    def _emit_value(self, _index: int) -> None:
        if self.currentData() == -1:
            self.customRequested.emit()
            return
        self._last_value = self.value()
        self.valueChanged.emit(self.value())

    def restore_last_value(self) -> None:
        self.setValue(self._last_value)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        """Draw a crisp disclosure mark instead of relying on the platform glyph."""

        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        group = QPalette.ColorGroup.Active if self.isEnabled() else QPalette.ColorGroup.Disabled
        pen = QPen(self.palette().color(group, QPalette.ColorRole.Text), 1.7)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        center_x = self.width() - 18.0
        center_y = self.height() / 2
        painter.drawPolyline(
            QPolygonF(
                [
                    QPointF(center_x - 4.5, center_y - 2.0),
                    QPointF(center_x, center_y + 2.5),
                    QPointF(center_x + 4.5, center_y - 2.0),
                ]
            )
        )
        painter.end()


@dataclass(frozen=True, slots=True)
class _ModelEntry:
    model_id: str
    display_name: str
    description: str
    size_bytes: int | None
    recommendation: ModelRecommendation | None
    installed: OllamaModel | None

    @property
    def compatible(self) -> bool:
        return not is_reasoning_model_id(self.model_id)


class ModelManagerDialog(QDialog):
    """Manage recommendations and installed models in one non-duplicated list."""

    install_requested = Signal(object)
    select_requested = Signal(str)
    delete_requested = Signal(str)
    custom_model_requested = Signal(str)
    library_requested = Signal()
    cancel_requested = Signal()
    connection_action_requested = Signal()
    context_window_changed = Signal(object)

    def __init__(
        self,
        installed_models: tuple[OllamaModel, ...],
        selected_model: str | None,
        recommendations: ModelRecommendations | None = None,
        parent: QWidget | None = None,
        *,
        context_window: int | None = None,
        ollama_status: OllamaStatus | None = None,
        ollama_message: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._installed_models = installed_models
        self._selected_model = selected_model
        self._recommendations = recommendations
        self._recommendation_error: str | None = None
        self._ollama_status = ollama_status
        self._ollama_message = ollama_message
        self._context_window = context_window
        self._active_filter = "installed" if installed_models else "recommended"
        self._busy = False
        self.recommendation_install_buttons: dict[str, QPushButton] = {}
        self.recommendation_action_buttons: dict[str, QPushButton] = {}
        self.installed_select_buttons: dict[str, QPushButton] = {}
        self.installed_delete_buttons: dict[str, QPushButton] = {}
        self.model_menu_buttons: dict[str, QPushButton] = {}
        self._responsive_rows: dict[
            QFrame,
            tuple[QGridLayout, QWidget, QLabel, ModelMenuButton, QPushButton],
        ] = {}
        self._compact = False

        self.setObjectName("modelManagerDialog")
        self.setWindowTitle("IA local — Parsezen")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumSize(320, 520)
        self.resize(980, 760)
        self._build_ui()
        if recommendations is not None:
            self.hardware_label.setText(_hardware_copy(recommendations.hardware_summary))
            self.hardware_label.setToolTip(recommendations.hardware_summary)
        self.set_connection_status(ollama_status, ollama_message)
        self._render_models()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        self.root_layout = root
        root.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        root.setContentsMargins(32, 12, 32, 24)
        root.setSpacing(16)

        intro = QLabel("IA local", self)
        intro.setObjectName("modelManagerIntro")
        root.addWidget(intro)

        self.status_card = QFrame(self)
        self.status_card.setObjectName("localAiStatusCard")
        status_layout = QGridLayout(self.status_card)
        self.status_layout = status_layout
        status_layout.setContentsMargins(16, 12, 16, 12)
        status_layout.setSpacing(14)
        self.status_copy_host = QWidget(self.status_card)
        status_copy = QVBoxLayout(self.status_copy_host)
        status_copy.setContentsMargins(0, 0, 0, 0)
        status_copy.setSpacing(3)
        self.connection_status_label = QLabel(self.status_card)
        self.connection_status_label.setObjectName("localAiStatus")
        status_copy.addWidget(self.connection_status_label)
        self.hardware_label = QLabel("Analizando el equipo…", self.status_card)
        self.hardware_label.setObjectName("modelHardwareLabel")
        status_copy.addWidget(self.hardware_label)
        status_layout.addWidget(self.status_copy_host, 0, 0)
        status_layout.setColumnStretch(0, 1)
        self.connection_action_button = QPushButton(self.status_card)
        self.connection_action_button.setObjectName("localAiConnectionAction")
        self.connection_action_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.connection_action_button.clicked.connect(self.connection_action_requested)
        status_layout.addWidget(self.connection_action_button, 0, 1)
        root.addWidget(self.status_card)

        filters = QHBoxLayout()
        filters.setSpacing(4)
        self.filter_buttons: dict[str, QPushButton] = {}
        for key, text in (
            ("installed", "Instalados"),
            ("recommended", "Añadir modelo"),
        ):
            button = QPushButton(text, self)
            button.setObjectName("modelFilter")
            button.setCheckable(True)
            active = key == self._active_filter
            button.setChecked(active)
            button.setProperty("activeFilter", active)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, selected=key: self._set_filter(selected))
            self.filter_buttons[key] = button
            filters.addWidget(button)
        filters.addStretch(1)
        root.addLayout(filters)

        self.all_models_tools = QWidget(self)
        self.all_models_tools.setObjectName("recommendedModelTools")
        all_tools_layout = QGridLayout(self.all_models_tools)
        self.all_tools_layout = all_tools_layout
        all_tools_layout.setContentsMargins(0, 0, 0, 0)
        all_tools_layout.setSpacing(10)
        self.search_input = QLineEdit(self.all_models_tools)
        self.search_input.setObjectName("modelSearch")
        self.search_input.setPlaceholderText("Buscar o escribir un modelo de Ollama")
        self.search_input.setAccessibleName("Buscar o escribir un modelo de Ollama")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setMinimumWidth(0)
        self.search_input.textChanged.connect(self._render_models)
        self.search_input.returnPressed.connect(self._request_custom_model)
        all_tools_layout.addWidget(self.search_input, 0, 0)
        all_tools_layout.setColumnStretch(0, 1)
        self.add_model_button = QPushButton("Buscar modelo", self.all_models_tools)
        self.add_model_button.setObjectName("addModel")
        self.add_model_button.setAccessibleName("Buscar e instalar el modelo escrito")
        self.add_model_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_model_button.clicked.connect(self._request_custom_model)
        all_tools_layout.addWidget(self.add_model_button, 0, 1)
        self.library_button = QPushButton(
            "Explorar catálogo de Ollama ↗",
            self.all_models_tools,
        )
        self.library_button.setObjectName("modelCatalogLink")
        self.library_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.library_button.setAccessibleName("Abrir el catálogo de modelos de Ollama")
        self.library_button.clicked.connect(self.library_requested)
        all_tools_layout.addWidget(self.library_button, 0, 2)
        root.addWidget(self.all_models_tools)

        # Compatibility aliases for the controller; the single field now owns
        # both filtering and manual Ollama installation.
        self.custom_model_input = self.search_input
        self.custom_install_button = self.add_model_button
        self.custom_panel = QWidget(self)
        self.custom_panel.hide()
        self.custom_error_label = QLabel(self)
        self.custom_error_label.setObjectName("customModelError")
        self.custom_error_label.setWordWrap(True)
        self.custom_error_label.hide()
        root.addWidget(self.custom_error_label)

        self.models_scroll = QScrollArea(self)
        self.models_scroll.setObjectName("modelManagerScroll")
        self.models_scroll.setWidgetResizable(True)
        self.models_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.models_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.models_host = QWidget(self.models_scroll)
        self.models_host.setObjectName("modelManagerBody")
        self.models_layout = QVBoxLayout(self.models_host)
        self.models_layout.setContentsMargins(0, 0, 4, 0)
        self.models_layout.setSpacing(0)
        self.models_scroll.setWidget(self.models_host)
        root.addWidget(self.models_scroll, 1)

        self.operation_widget = QFrame(self)
        self.operation_widget.setObjectName("modelOperation")
        operation_layout = QGridLayout(self.operation_widget)
        self.operation_layout = operation_layout
        operation_layout.setContentsMargins(12, 8, 12, 8)
        operation_layout.setSpacing(10)
        self.operation_label = QLabel(self.operation_widget)
        self.operation_label.setObjectName("modelOperationLabel")
        self.operation_label.setWordWrap(True)
        operation_layout.addWidget(self.operation_label, 0, 0)
        operation_layout.setColumnStretch(0, 1)
        self.operation_progress = QProgressBar(self.operation_widget)
        self.operation_progress.setObjectName("modelOperationProgress")
        self.operation_progress.setTextVisible(False)
        self.operation_progress.setFixedWidth(180)
        operation_layout.addWidget(self.operation_progress, 0, 1)
        self.cancel_button = QPushButton("Cancelar", self.operation_widget)
        self.cancel_button.clicked.connect(self.cancel_requested)
        operation_layout.addWidget(self.cancel_button, 0, 2)
        self.operation_widget.hide()
        root.addWidget(self.operation_widget)

        execution = QFrame(self)
        self.execution = execution
        execution.setObjectName("executionSettings")
        execution_layout = QGridLayout(execution)
        self.execution_layout = execution_layout
        execution_layout.setContentsMargins(16, 12, 16, 12)
        execution_layout.setSpacing(18)
        self.execution_copy_host = QWidget(execution)
        execution_copy = QVBoxLayout(self.execution_copy_host)
        execution_copy.setContentsMargins(0, 0, 0, 0)
        execution_copy.setSpacing(3)
        context_label = QLabel("Configuración predeterminada", execution)
        context_label.setObjectName("contextTitle")
        execution_copy.addWidget(context_label)
        self.default_model_label = QLabel(execution)
        self.default_model_label.setObjectName("defaultModelLabel")
        execution_copy.addWidget(self.default_model_label)
        self.default_scope_label = QLabel(execution)
        self.default_scope_label.setObjectName("contextHelp")
        self.default_scope_label.setWordWrap(True)
        execution_copy.addWidget(self.default_scope_label)
        context_help = QLabel(
            "El contexto automático se ajusta al modelo y al equipo. "
            "Amplíalo solo si una tarea concreta lo necesita.",
            execution,
        )
        context_help.setObjectName("contextHelp")
        execution_copy.addWidget(context_help)
        execution_layout.addWidget(self.execution_copy_host, 0, 0)
        execution_layout.setColumnStretch(0, 1)
        self.context_window = ContextWindowSelector(execution)
        self.context_window.setMaximum(self._selected_context_maximum())
        self.context_window.setValue(self._context_window or 0)
        self.context_window.setFixedSize(170, 38)
        self.context_window.valueChanged.connect(self._context_changed)
        self.context_window.customRequested.connect(self._choose_advanced_context)
        execution_layout.addWidget(
            self.context_window,
            0,
            1,
            1,
            1,
            Qt.AlignmentFlag.AlignVCenter,
        )
        root.insertWidget(2, execution)

        self.toast = QLabel(self)
        self.toast.setObjectName("modelToast")
        self.toast.setFixedSize(290, 48)
        self.toast.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.toast.hide()
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self.toast.hide)
        self.all_models_tools.setVisible(self._active_filter == "recommended")
        self._refresh_default_model()
        self.set_inherited_job_count(0)
        self._apply_styles()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.set_compact_mode(event.size().width() <= BREAKPOINTS.compact)
        self.toast.move(
            max(12, self.width() - self.toast.width() - 24),
            max(12, self.height() - self.toast.height() - 24),
        )

    def set_compact_mode(self, compact: bool) -> None:
        if compact == self._compact:
            return
        self._compact = compact
        for layout, widgets in (
            (
                self.status_layout,
                (self.status_copy_host, self.connection_action_button),
            ),
            (
                self.all_tools_layout,
                (self.search_input, self.add_model_button, self.library_button),
            ),
            (
                self.operation_layout,
                (self.operation_label, self.operation_progress, self.cancel_button),
            ),
            (
                self.execution_layout,
                (self.execution_copy_host, self.context_window),
            ),
        ):
            for widget in widgets:
                layout.removeWidget(widget)
        if compact:
            self.root_layout.setContentsMargins(
                SPACING.md,
                SPACING.sm,
                SPACING.md,
                SPACING.md,
            )
            self.status_layout.addWidget(self.status_copy_host, 0, 0, 1, 2)
            self.status_layout.addWidget(self.connection_action_button, 1, 0, 1, 2)
            self.all_tools_layout.addWidget(self.search_input, 0, 0, 1, 2)
            self.all_tools_layout.addWidget(self.add_model_button, 1, 0)
            self.all_tools_layout.addWidget(self.library_button, 1, 1)
            self.operation_layout.addWidget(self.operation_label, 0, 0, 1, 2)
            self.operation_layout.addWidget(self.operation_progress, 1, 0)
            self.operation_layout.addWidget(self.cancel_button, 1, 1)
            self.operation_progress.setMinimumWidth(0)
            self.operation_progress.setMaximumWidth(16_777_215)
            self.execution_layout.addWidget(self.execution_copy_host, 0, 0, 1, 2)
            self.execution_layout.addWidget(
                self.context_window,
                1,
                0,
                1,
                2,
                Qt.AlignmentFlag.AlignLeft,
            )
            self.toast.setFixedWidth(max(240, min(290, self.width() - 24)))
        else:
            self.root_layout.setContentsMargins(32, 12, 32, 24)
            self.status_layout.addWidget(self.status_copy_host, 0, 0)
            self.status_layout.addWidget(self.connection_action_button, 0, 1)
            self.all_tools_layout.addWidget(self.search_input, 0, 0)
            self.all_tools_layout.addWidget(self.add_model_button, 0, 1)
            self.all_tools_layout.addWidget(self.library_button, 0, 2)
            self.operation_layout.addWidget(self.operation_label, 0, 0)
            self.operation_layout.addWidget(self.operation_progress, 0, 1)
            self.operation_layout.addWidget(self.cancel_button, 0, 2)
            self.operation_progress.setFixedWidth(180)
            self.execution_layout.addWidget(self.execution_copy_host, 0, 0)
            self.execution_layout.addWidget(
                self.context_window,
                0,
                1,
                1,
                1,
                Qt.AlignmentFlag.AlignVCenter,
            )
            self.toast.setFixedWidth(290)
        for row, record in self._responsive_rows.items():
            self._layout_model_row(row, record, compact)

    def apply_theme(self) -> None:
        self._apply_styles()
        self.update()
        self.models_scroll.viewport().update()

    def _context_changed(self, value: int) -> None:
        self.context_window_changed.emit(value or None)

    def _refresh_default_model(self) -> None:
        selected_id = choose_ollama_model(self._installed_models, self._selected_model)
        selected = next(
            (model for model in self._installed_models if model.model_id == selected_id),
            None,
        )
        if selected is None:
            self.default_model_label.setText("Modelo: sin modelo predeterminado")
            self.default_model_label.setAccessibleName(
                "No hay un modelo predeterminado de IA local"
            )
            return
        self.default_model_label.setText(f"Modelo: {selected.display_name}")
        self.default_model_label.setToolTip(selected.model_id)
        self.default_model_label.setAccessibleName(
            f"Modelo predeterminado: {selected.display_name}"
        )

    def set_inherited_job_count(self, count: int) -> None:
        count = max(0, count)
        if count:
            text = (
                f"{count} trabajo{'s' if count != 1 else ''} pendiente"
                f"{'s' if count != 1 else ''} usa"
                f"{'n' if count != 1 else ''} este predeterminado."
            )
        else:
            text = "Se aplicará a los nuevos trabajos que necesiten IA local."
        self.default_scope_label.setText(text)

    def _selected_context_maximum(self) -> int:
        selected_id = choose_ollama_model(self._installed_models, self._selected_model)
        selected = next(
            (model for model in self._installed_models if model.model_id == selected_id),
            None,
        )
        if selected is None or selected.max_context is None:
            return MAX_CONTEXT_WINDOW
        return selected.max_context

    def set_connection_status(
        self,
        status: OllamaStatus | None,
        message: str | None = None,
    ) -> None:
        """Reflect Ollama availability while retaining its one useful recovery action."""

        self._ollama_status = status
        self._ollama_message = message
        labels: dict[OllamaStatus | None, tuple[str, str | None]] = {
            None: ("Comprobando Ollama…", "Comprobar"),
            OllamaStatus.READY: ("✓ Ollama listo", None),
            OllamaStatus.NOT_INSTALLED: ("Ollama no está instalado", "Instalar"),
            OllamaStatus.STOPPED: ("Ollama está detenido", "Iniciar"),
            OllamaStatus.MISSING_MODEL: ("Ollama listo · falta un modelo", None),
            OllamaStatus.LOCAL_ONLY_REQUIRED: (
                "Ollama necesita el modo solo local",
                "Corregir",
            ),
            OllamaStatus.UNAVAILABLE: ("Ollama no está disponible", "Reintentar"),
        }
        label, action = labels[status]
        self.connection_status_label.setText(message or label)
        self.connection_status_label.setToolTip(message or label)
        self.connection_status_label.setProperty("ready", status is OllamaStatus.READY)
        self.connection_status_label.style().unpolish(self.connection_status_label)
        self.connection_status_label.style().polish(self.connection_status_label)
        self.connection_action_button.setText(action or "")
        self.connection_action_button.setVisible(action is not None)

    def set_recommendations_loading(self) -> None:
        self._recommendations = None
        self._recommendation_error = None
        self.hardware_label.setText("Analizando la memoria y la gráfica de este equipo…")
        self._render_models()
        self.set_busy(True)

    def set_recommendations(self, recommendations: ModelRecommendations) -> None:
        self._recommendations = recommendations
        self._recommendation_error = None
        self.hardware_label.setText(_hardware_copy(recommendations.hardware_summary))
        self.hardware_label.setToolTip(recommendations.hardware_summary)
        self._render_models()
        self.set_busy(False)

    def set_recommendation_error(self, message: str) -> None:
        self._recommendations = None
        self._recommendation_error = message
        self.hardware_label.setText("No se pudo analizar el equipo ahora")
        self._render_models()
        self.set_busy(False)

    def set_installed_models(
        self,
        models: tuple[OllamaModel, ...],
        selected_model: str | None,
    ) -> None:
        self._installed_models = models
        self._selected_model = selected_model
        self.context_window.setMaximum(self._selected_context_maximum())
        self._refresh_default_model()
        self._render_models()

    def set_custom_error(self, message: str | None) -> None:
        self.custom_error_label.setText(message or "")
        self.custom_error_label.setVisible(bool(message))
        if message:
            self.custom_model_input.setFocus()

    def set_operation(
        self,
        message: str,
        *,
        percent: int | None = None,
        cancellable: bool = False,
    ) -> None:
        self.operation_label.setText(message)
        self.operation_progress.show()
        if percent is None:
            self.operation_progress.setRange(0, 0)
        else:
            self.operation_progress.setRange(0, 100)
            self.operation_progress.setValue(max(0, min(100, percent)))
        self.cancel_button.setVisible(cancellable)
        self.cancel_button.setEnabled(cancellable)
        self.operation_widget.show()
        self.set_busy(True)

    def finish_operation(
        self,
        message: str | None = None,
        *,
        completed: bool = False,
    ) -> None:
        self.set_busy(False)
        if completed:
            self.operation_widget.hide()
            self._show_toast("✓ " + (message or "Lista de modelos actualizada").rstrip("."))
            return
        if message:
            self.operation_label.setText(message)
            self.operation_progress.hide()
            self.cancel_button.hide()
            self.operation_widget.show()
        else:
            self.operation_widget.hide()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.connection_action_button.setEnabled(not busy)
        self.context_window.setEnabled(not busy)
        self.custom_model_input.setEnabled(not busy)
        self.custom_install_button.setEnabled(not busy)
        for button in self.recommendation_action_buttons.values():
            button.setEnabled(not busy and button.property("currentModel") is not True)
        for button in self.installed_select_buttons.values():
            button.setEnabled(
                not busy
                and button.property("currentModel") is not True
                and button.property("incompatibleModel") is not True
            )
        for button in self.model_menu_buttons.values():
            button.setEnabled(not busy)

    def _set_filter(self, selected: str) -> None:
        self._active_filter = selected
        for key, button in self.filter_buttons.items():
            active = key == selected
            button.setChecked(active)
            button.setProperty("activeFilter", active)
            button.style().unpolish(button)
            button.style().polish(button)
        self.all_models_tools.setVisible(selected == "recommended")
        self._render_models()

    def _request_custom_model(self) -> None:
        model_id = self.custom_model_input.text().strip()
        if not model_id:
            self.set_custom_error("Escribe el nombre de un modelo de Ollama.")
            return
        self.set_custom_error(None)
        self.custom_model_requested.emit(model_id)
        self.custom_model_input.clear()

    def _choose_advanced_context(self) -> None:
        current = self.context_window.value() or min(8192, self.context_window.maximum())
        value, accepted = QInputDialog.getInt(
            self,
            "Ventana de contexto",
            "Tokens:",
            current,
            MIN_CONTEXT_WINDOW,
            self.context_window.maximum(),
            512,
        )
        if accepted:
            self.context_window.setValue(value)
        else:
            self.context_window.restore_last_value()

    def _entries(self) -> tuple[_ModelEntry, ...]:
        entries: list[_ModelEntry] = []
        represented_installed: set[str] = set()
        recommendations = self._recommendations.models if self._recommendations else ()
        for recommendation in recommendations:
            installed_id = choose_ollama_model(
                self._installed_models,
                recommendation.model_id,
            )
            installed = next(
                (model for model in self._installed_models if model.model_id == installed_id),
                None,
            )
            if installed is not None:
                represented_installed.add(installed.model_id)
            entries.append(
                _ModelEntry(
                    recommendation.model_id,
                    recommendation.display_name,
                    _model_description(recommendation),
                    recommendation.download_size_bytes
                    or (installed.size_bytes if installed else None),
                    recommendation,
                    installed,
                )
            )
        for model in self._installed_models:
            if model.model_id in represented_installed:
                continue
            entries.append(
                _ModelEntry(
                    model.model_id,
                    model.display_name,
                    (
                        "No apto para transformar documentos; puedes eliminarlo desde el menú."
                        if is_reasoning_model_id(model.model_id)
                        else "Modelo local de Ollama."
                    ),
                    model.size_bytes,
                    None,
                    model,
                )
            )
        selected = choose_ollama_model(self._installed_models, self._selected_model)
        return tuple(
            sorted(
                entries,
                key=lambda item: (
                    0 if item.installed and item.installed.model_id == selected else 1,
                    0 if item.recommendation else 1,
                ),
            )
        )

    def _render_models(self) -> None:
        _clear_widget_layout(self.models_layout)
        self._responsive_rows.clear()
        self.recommendation_install_buttons.clear()
        self.recommendation_action_buttons.clear()
        self.installed_select_buttons.clear()
        self.installed_delete_buttons.clear()
        self.model_menu_buttons.clear()

        query = self.search_input.text().strip().casefold()
        entries = tuple(
            entry
            for entry in self._entries()
            if (
                self._active_filter == "recommended"
                and (
                    entry.recommendation is not None
                    or (self._recommendations is None and entry.installed is not None)
                )
                or (self._active_filter == "installed" and entry.installed is not None)
            )
            and (
                self._active_filter != "recommended"
                or not query
                or query in entry.display_name.casefold()
                or query in entry.model_id.casefold()
                or query in entry.description.casefold()
            )
        )
        if not entries:
            if self._active_filter == "installed":
                message = "Todavía no hay modelos instalados."
            else:
                message = self._recommendation_error or (
                    "No hay modelos que coincidan."
                    if query
                    else "No hay recomendaciones disponibles ahora."
                )
            label = QLabel(message, self.models_host)
            label.setObjectName("modelEmptyLabel")
            label.setWordWrap(True)
            self.models_layout.addWidget(label)
            self.models_layout.addStretch(1)
            return
        for entry in entries:
            self.models_layout.addWidget(self._create_model_row(entry))
        self.models_layout.addStretch(1)
        self.set_busy(self._busy)

    def _create_model_row(self, entry: _ModelEntry) -> QFrame:
        row = QFrame(self.models_host)
        row.setObjectName("modelListRow")
        row.setProperty(
            "recommended",
            bool(entry.recommendation and entry.recommendation.role is RecommendationRole.BALANCED),
        )
        row.setMinimumHeight(68)
        layout = QGridLayout(row)
        layout.setContentsMargins(16, 10, 12, 10)
        layout.setSpacing(14)

        copy_host = QWidget(row)
        copy = QVBoxLayout(copy_host)
        copy.setContentsMargins(0, 0, 0, 0)
        copy.setSpacing(3)
        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        name = QLabel(entry.display_name, row)
        name.setObjectName("modelName")
        name_row.addWidget(name)
        if entry.recommendation is not None:
            recommendation_tag = QLabel("Recomendado", row)
            recommendation_tag.setObjectName("recommendedTag")
            recommendation_tag.setAlignment(Qt.AlignmentFlag.AlignCenter)
            name_row.addWidget(recommendation_tag)
        name_row.addStretch(1)
        copy.addLayout(name_row)
        description = QLabel(entry.description, row)
        description.setObjectName("modelDescription")
        description.setWordWrap(True)
        copy.addWidget(description)
        layout.addWidget(copy_host, 0, 0)
        layout.setColumnStretch(0, 1)

        size = QLabel(_model_size(entry.size_bytes), row)
        size.setObjectName("modelSize")
        size.setFixedWidth(72)
        size.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(size, 0, 1)

        menu_button = ModelMenuButton(row)
        menu_button.setAccessibleName(f"Acciones para {entry.display_name}")
        menu_button.setToolTip(f"Acciones para {entry.display_name}")
        menu_button.clicked.connect(
            lambda _checked=False, item=entry, button=menu_button: self._show_model_menu(
                item,
                button,
            )
        )
        self.model_menu_buttons[entry.model_id] = menu_button
        if entry.installed is not None:
            self.installed_delete_buttons[entry.installed.model_id] = menu_button
        layout.addWidget(menu_button, 0, 2)

        selected = choose_ollama_model(self._installed_models, self._selected_model)
        is_current = entry.installed is not None and entry.installed.model_id == selected
        if entry.installed is None:
            action = QPushButton("Instalar" if entry.compatible else "No compatible", row)
            action.setObjectName("installModel" if entry.compatible else "useModel")
            action.setProperty("incompatibleModel", not entry.compatible)
            action.setEnabled(entry.compatible)
            if entry.compatible:
                action.clicked.connect(
                    lambda _checked=False, item=entry.recommendation: self.install_requested.emit(
                        item
                    )
                )
                if entry.recommendation is not None:
                    self.recommendation_install_buttons[entry.recommendation.model_id] = action
        else:
            action = QPushButton(
                (
                    "No compatible"
                    if not entry.compatible
                    else ("Predeterminado" if is_current else "Usar por defecto")
                ),
                row,
            )
            action.setObjectName("useModel")
            action.setProperty("currentModel", is_current)
            action.setProperty("incompatibleModel", not entry.compatible)
            action.setEnabled(entry.compatible and not is_current)
            if entry.compatible and not is_current:
                action.clicked.connect(
                    lambda _checked=False, model_id=entry.installed.model_id: (
                        self.select_requested.emit(model_id)
                    )
                )
            self.installed_select_buttons[entry.installed.model_id] = action
        action.setProperty("modelManagerAction", True)
        action.setFixedSize(140, 36)
        action.setCursor(Qt.CursorShape.PointingHandCursor)
        self.recommendation_action_buttons[entry.model_id] = action
        layout.addWidget(action, 0, 3)
        record = (layout, copy_host, size, menu_button, action)
        self._responsive_rows[row] = record
        self._layout_model_row(row, record, self._compact)
        return row

    @staticmethod
    def _layout_model_row(
        row: QFrame,
        record: tuple[QGridLayout, QWidget, QLabel, ModelMenuButton, QPushButton],
        compact: bool,
    ) -> None:
        layout, copy_host, size, menu_button, action = record
        for widget in (copy_host, size, menu_button, action):
            layout.removeWidget(widget)
        if compact:
            layout.setContentsMargins(12, 10, 10, 10)
            layout.setHorizontalSpacing(8)
            layout.setVerticalSpacing(8)
            layout.addWidget(copy_host, 0, 0, 1, 2)
            layout.addWidget(size, 1, 0)
            layout.addWidget(menu_button, 1, 1)
            layout.addWidget(action, 2, 0, 1, 2)
            size.setVisible(True)
            action.setMinimumWidth(0)
            action.setMaximumWidth(16_777_215)
            action.setFixedHeight(38)
            row.setMinimumHeight(154)
        else:
            layout.setContentsMargins(12, 8, 8, 8)
            layout.setSpacing(10)
            layout.addWidget(copy_host, 0, 0)
            layout.addWidget(size, 0, 1)
            layout.addWidget(menu_button, 0, 2)
            layout.addWidget(action, 0, 3)
            size.setVisible(True)
            action.setFixedSize(140, 36)
            row.setMinimumHeight(68)

    def _show_model_menu(self, entry: _ModelEntry, button: QPushButton) -> None:
        menu = QMenu(self)
        if entry.installed is not None:
            delete = menu.addAction("Eliminar")
        else:
            delete = None
        catalog = menu.addAction("Ver en el catálogo")
        selected = menu.exec(button.mapToGlobal(button.rect().bottomLeft()))
        if selected is delete and entry.installed is not None:
            self.delete_requested.emit(entry.installed.model_id)
        elif selected is catalog:
            self.library_requested.emit()

    def _show_toast(self, message: str) -> None:
        self.toast.setText(message)
        self.toast.show()
        self.toast.raise_()
        self._toast_timer.start(2800)

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            f"""
            QLabel#modelManagerIntro {{
                color: {COLORS.text_primary};
                font-size: 14pt;
                font-weight: 600;
            }}
            QFrame#localAiStatusCard,
            QFrame#executionSettings,
            QFrame#modelOperation {{
                background-color: {COLORS.surface_subtle};
                border: none;
                border-radius: 8px;
            }}
            QLabel#localAiStatus {{
                color: {COLORS.text_primary};
                font-size: 10.5pt;
                font-weight: 600;
            }}
            QLabel#localAiStatus[ready="true"] {{
                color: {COLORS.success};
            }}
            QLabel#modelHardwareLabel,
            QLabel#modelDescription,
            QLabel#contextHelp,
            QLabel#modelEmptyLabel {{
                color: {COLORS.text_secondary};
            }}
            QPushButton#modelFilter {{
                min-width: 86px;
                min-height: 36px;
                max-height: 36px;
                padding: 0 12px;
                background-color: transparent;
                border: none;
                border-bottom: 2px solid transparent;
                border-radius: 0;
            }}
            QPushButton#modelFilter[activeFilter="true"] {{
                color: {COLORS.action_primary_hover};
                background-color: transparent;
                border-bottom-color: {COLORS.action_primary};
            }}
            QPushButton#modelFilter:focus {{
                background-color: {COLORS.action_primary_soft};
                border: 2px solid {COLORS.focus_ring};
            }}
            QLineEdit#modelSearch,
            QPushButton#addModel {{
                min-height: 38px;
                max-height: 38px;
            }}
            QPushButton#addModel {{
                color: {COLORS.text_inverse};
                background-color: {COLORS.action_primary};
                border-color: {COLORS.action_primary};
                font-weight: 600;
            }}
            QComboBox#localAiContextWindow {{
                padding-right: 36px;
            }}
            QComboBox#localAiContextWindow::drop-down {{
                width: 36px;
                border: none;
            }}
            QComboBox#localAiContextWindow::down-arrow {{
                image: none;
            }}
            QFrame#modelListRow {{
                background-color: transparent;
                border: none;
                border-bottom: 1px solid {COLORS.divider};
                border-radius: 0;
            }}
            QFrame#modelListRow[recommended="true"] {{
                background-color: {COLORS.action_primary_soft};
            }}
            QFrame#modelListRow:hover {{
                background-color: {COLORS.surface_hover};
            }}
            QLabel#modelName {{
                color: {COLORS.text_primary};
                font-size: 10.5pt;
                font-weight: 650;
            }}
            QLabel#modelSize {{
                color: {COLORS.text_secondary};
            }}
            QLabel#recommendedTag {{
                min-height: 20px;
                max-height: 20px;
                padding: 0 8px;
                color: {COLORS.action_primary_hover};
                background-color: {COLORS.action_primary_soft};
                border: none;
                border-radius: 10px;
                font-size: 8.5pt;
                font-weight: 600;
            }}
            QPushButton#modelMenu {{
                min-width: 40px;
                max-width: 40px;
                min-height: 40px;
                max-height: 40px;
                padding: 0;
                font-size: 17pt;
                background-color: transparent;
                border-color: transparent;
            }}
            QPushButton#modelMenu:focus {{
                border: 2px solid {COLORS.focus_ring};
            }}
            QPushButton#useModel {{
                color: {COLORS.action_primary_hover};
                background-color: {COLORS.action_primary_soft};
                border-color: transparent;
                font-weight: 600;
            }}
            QPushButton#useModel[currentModel="true"] {{
                color: {COLORS.success};
                background-color: {COLORS.success_soft};
                border-color: transparent;
            }}
            QPushButton#installModel {{
                color: {COLORS.text_primary};
                background-color: {COLORS.surface_subtle};
                border-color: transparent;
                font-weight: 600;
            }}
            QLabel#contextTitle {{
                color: {COLORS.text_primary};
                font-weight: 600;
            }}
            QLabel#defaultModelLabel {{
                color: {COLORS.text_primary};
                font-weight: 500;
            }}
            QPushButton#modelCatalogLink {{
                min-height: 24px;
                max-height: 28px;
                padding: 0;
                color: {COLORS.action_primary_hover};
                background-color: transparent;
                border: none;
                text-align: left;
            }}
            QLabel#customModelError {{
                color: {COLORS.error};
            }}
            QLabel#modelToast {{
                color: {COLORS.text_primary};
                background-color: {COLORS.surface_subtle};
                border: 1px solid {COLORS.divider};
                border-radius: 8px;
            }}
            """
        )


def _clear_widget_layout(layout: QVBoxLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item is None:
            continue
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()


def _hardware_copy(summary: str) -> str:
    return summary.strip() or "Información del equipo no disponible"


def _model_description(recommendation: ModelRecommendation) -> str:
    if recommendation.role is RecommendationRole.BALANCED:
        return "Buen equilibrio entre calidad, velocidad y consumo de recursos."
    if recommendation.role is RecommendationRole.FASTER:
        return "Más rápido, ideal para tareas sencillas y respuestas breves."
    if recommendation.role is RecommendationRole.CAPACITY:
        return "Mayor capacidad para tareas complejas, con mayor uso de memoria."
    return recommendation.description or "Alternativa compatible con este equipo."


def _model_size(size_bytes: int | None) -> str:
    return f"{_format_decimal(size_bytes / 1_000_000_000)} GB" if size_bytes else "—"


def _format_tokens(value: int) -> str:
    return f"{value:,} tokens".replace(",", ".")


def _format_decimal(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".").replace(".", ",")
