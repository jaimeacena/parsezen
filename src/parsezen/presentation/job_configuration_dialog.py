"""Transactional, adaptive configuration editor for one independent document."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QResizeEvent, QStandardItemModel
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from parsezen.application.configuration_rules import (
    SUPPORTED_OUTPUTS,
    ConfigurationIssue,
    ConfigurationSection,
    configuration_issues,
    requires_ai,
)
from parsezen.domain.jobs import (
    AIProfileConfiguration,
    CoverStrategy,
    DocumentFormat,
    DocumentJob,
    JobConfiguration,
    OutputConfiguration,
    PageRangeConfiguration,
    RefinementConfiguration,
    StructureConfiguration,
    TranslationConfiguration,
    TranslationMethod,
    effective_ai_profile,
)
from parsezen.domain.stages import StageKind
from parsezen.epub_conversion import inspect_epub_package
from parsezen.errors import ConversionError, RequestValidationError
from parsezen.glossary import MAX_GLOSSARY_ENTRIES, GlossaryEntry, validate_glossary
from parsezen.local_models import OllamaStatus
from parsezen.presentation.components import ChevronComboBox, Switch
from parsezen.presentation.design_system import BREAKPOINTS, COLORS, SPACING
from parsezen.translation_quality import TARGET_LANGUAGE_CODES

_MANAGE_MODELS = "__manage_models__"
_DEFAULT_CONTEXT = 8_192
_AI_DEFAULT_UNSET = object()


class FlowNavigationRow(QFrame):
    """One selectable plan section with a separate optional activation switch."""

    selected = Signal()

    def __init__(
        self,
        title: str,
        summary: str,
        *,
        activation: Switch | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._title = title
        self._summary = summary
        self.setObjectName("flowNavigationRow")
        self.setProperty("selected", False)
        self.setProperty("hovered", False)
        self.setProperty("focusWithin", False)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 3, 6, 3)
        layout.setSpacing(6)
        self.button = QPushButton(self)
        self.button.setObjectName("flowNavigationButton")
        self.button.setAutoDefault(False)
        self.button.setDefault(False)
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.setAccessibleName(f"Abrir {title}")
        self.button.clicked.connect(self.selected)
        self.button.installEventFilter(self)
        layout.addWidget(self.button, 1)
        self.activation = activation
        if activation is not None:
            activation.installEventFilter(self)
            layout.addWidget(activation, 0, Qt.AlignmentFlag.AlignVCenter)
        self.installEventFilter(self)
        self._refresh_text()

    def set_summary(self, summary: str) -> None:
        self._summary = summary
        self._refresh_text()

    def set_selected(self, selected: bool) -> None:
        self._set_state_property("selected", selected)
        self.button.setAccessibleDescription(
            "Sección seleccionada" if selected else "Sección de configuración"
        )

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if watched in {self, self.button, self.activation}:
            if event.type() is QEvent.Type.Enter:
                self._set_state_property("hovered", True)
            elif event.type() is QEvent.Type.Leave:
                QTimer.singleShot(0, self._sync_hovered)
            elif event.type() is QEvent.Type.FocusIn:
                self._set_state_property("focusWithin", True)
            elif event.type() is QEvent.Type.FocusOut:
                QTimer.singleShot(0, self._sync_focus_within)
        return super().eventFilter(watched, event)

    def _sync_hovered(self) -> None:
        position = self.mapFromGlobal(QCursor.pos())
        self._set_state_property("hovered", self.rect().contains(position))

    def _sync_focus_within(self) -> None:
        focused = self.button.hasFocus() or bool(
            self.activation is not None and self.activation.hasFocus()
        )
        self._set_state_property("focusWithin", focused)

    def _set_state_property(self, name: str, value: bool) -> None:
        if self.property(name) is value:
            return
        self.setProperty(name, value)
        self.style().unpolish(self)
        self.style().polish(self)

    def _refresh_text(self) -> None:
        self.button.setText(f"{self._title}\n{self._summary}")
        self.button.setToolTip(self._summary)


class JobConfigurationDialog(QDialog):
    """Edit a complete immutable job configuration on one internal page."""

    save_requested = Signal()
    cancel_requested = Signal()
    models_requested = Signal()
    glossary_requested = Signal()

    def __init__(
        self,
        job: DocumentJob,
        *,
        models: tuple[tuple[str, str], ...] = (),
        stage: StageKind | None = None,
        embedded: bool = False,
        default_output_directory: Path | None = None,
        default_ai_model: str | None | object = _AI_DEFAULT_UNSET,
        default_ai_context: int | None | object = _AI_DEFAULT_UNSET,
        ollama_status: OllamaStatus | None = None,
        compatible_job_count: int = 0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._job = job
        self._stage = stage
        self._embedded = embedded
        self._default_output_directory = default_output_directory
        profile = effective_ai_profile(job.configuration)
        self._default_ai_model = (
            (None if profile.is_custom else profile.model)
            if default_ai_model is _AI_DEFAULT_UNSET
            else (default_ai_model if isinstance(default_ai_model, str) else None)
        )
        self._default_ai_context = (
            (None if profile.is_custom else profile.context_window)
            if default_ai_context is _AI_DEFAULT_UNSET
            else (
                default_ai_context
                if isinstance(default_ai_context, int) and not isinstance(default_ai_context, bool)
                else None
            )
        )
        self._ollama_status = ollama_status
        self._available_model_count = len(models)
        self._glossary = job.configuration.translation.glossary
        self._dirty = False
        self._loading = True
        self._structure_restore = False
        self._last_issues: tuple[ConfigurationIssue, ...] = ()
        self._compact = False
        self._showing_compact_detail = False
        self._source_book_title = job.source.path.stem
        self._source_book_author = ""
        if job.source.format is DocumentFormat.EPUB and job.source.path.is_file():
            try:
                source_metadata = inspect_epub_package(job.source.path)
            except ConversionError:
                pass
            else:
                self._source_book_title = source_metadata.title or job.source.path.stem
                self._source_book_author = ", ".join(source_metadata.authors)

        self.setWindowTitle(f"Configurar · {job.source.path.name}")
        self.setModal(not embedded)
        if embedded:
            self.setWindowFlags(Qt.WindowType.Widget)
        self.resize(980, 680)
        self.setObjectName("jobConfigurationEditor")

        layout = QVBoxLayout(self)
        self.root_layout = layout
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        layout.setContentsMargins(*(0, 0, 0, 0) if embedded else (24, 20, 24, 20))
        layout.setSpacing(12)

        self.heading = QLabel(job.source.path.name, self)
        heading_font = self.heading.font()
        heading_font.setPointSizeF(heading_font.pointSizeF() + 2)
        heading_font.setWeight(heading_font.Weight.DemiBold)
        self.heading.setFont(heading_font)
        self.heading.setVisible(not embedded)
        layout.addWidget(self.heading)

        self.tabs = QTabWidget(self)
        self.tabs.setObjectName("configurationTabs")
        self.tabs.tabBar().hide()
        self.output_page = self._build_output_page()
        self.translation_page = self._build_translation_page()
        self.refinement_page = self._build_refinement_page()
        self.personalization_page = self._build_personalization_page()
        self.ai_page = QWidget(self.tabs)
        ai_page_layout = QVBoxLayout(self.ai_page)
        self.ai_page_layout = ai_page_layout
        ai_page_layout.setContentsMargins(28, 24, 28, 24)
        self.ai_panel = self._build_ai_panel(models)
        ai_page_layout.addWidget(self.ai_panel)
        ai_page_layout.addStretch(1)
        # Compatibility alias for integrations that still address the former
        # chapter-only page by its domain-stage name.
        self.structure_page = self.personalization_page
        self.output_tab_index = self.tabs.addTab(self.output_page, "Resultado")
        self.translation_tab_index = self.tabs.addTab(self.translation_page, "Traducir")
        self.refinement_tab_index = self.tabs.addTab(self.refinement_page, "Corregir")
        self.structure_tab_index = self.tabs.addTab(
            self.personalization_page,
            "Personalizar",
        )
        self.personalization_tab_index = self.structure_tab_index
        self.ai_tab_index = self.tabs.addTab(self.ai_page, "IA local")

        self.plan_panel = QFrame(self)
        self.plan_panel.setObjectName("configurationPlan")
        plan_layout = QVBoxLayout(self.plan_panel)
        plan_layout.setContentsMargins(4, 8, 4, 12)
        plan_layout.setSpacing(3)
        self.plan_title = QLabel("Plan del documento", self.plan_panel)
        self.plan_title.setObjectName("configurationPlanTitle")
        plan_layout.addWidget(self.plan_title)
        self.plan_summary = QLabel(self.plan_panel)
        self.plan_summary.setObjectName("configurationPlanSummary")
        self.plan_summary.setWordWrap(True)
        self.plan_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        plan_layout.addWidget(self.plan_summary)
        layout.addWidget(self.plan_panel)

        self.configuration_body = QWidget(self)
        self.configuration_body.setObjectName("configurationMasterDetail")
        body_layout = QGridLayout(self.configuration_body)
        self.body_layout = body_layout
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setHorizontalSpacing(0)
        body_layout.setVerticalSpacing(8)

        self.navigation_panel = QFrame(self.configuration_body)
        self.navigation_panel.setObjectName("flowNavigation")
        navigation_layout = QVBoxLayout(self.navigation_panel)
        self.navigation_layout = navigation_layout
        navigation_layout.setContentsMargins(0, 8, 12, 8)
        navigation_layout.setSpacing(3)
        self.output_navigation = FlowNavigationRow(
            "Resultado",
            "Formato y destino",
            parent=self.navigation_panel,
        )
        self.translation_navigation = FlowNavigationRow(
            "Traducir",
            "Desactivado",
            activation=self.translation_enabled,
            parent=self.navigation_panel,
        )
        self.refinement_navigation = FlowNavigationRow(
            "Corregir",
            "Desactivado",
            activation=self.refinement_enabled,
            parent=self.navigation_panel,
        )
        self.personalization_navigation = FlowNavigationRow(
            "Personalizar",
            "Editor final EPUB",
            parent=self.navigation_panel,
        )
        self.ai_navigation = FlowNavigationRow(
            "IA local",
            "No necesaria",
            parent=self.navigation_panel,
        )
        self._navigation_rows = (
            self.output_navigation,
            self.translation_navigation,
            self.refinement_navigation,
            self.personalization_navigation,
            self.ai_navigation,
        )
        for index, row in enumerate(self._navigation_rows):
            row.selected.connect(lambda selected=index: self._select_section(selected))
            navigation_layout.addWidget(row)
        self.apply_compatible_section = QFrame(self.navigation_panel)
        self.apply_compatible_section.setObjectName("applyCompatibleSection")
        compatible_layout = QVBoxLayout(self.apply_compatible_section)
        compatible_layout.setContentsMargins(4, 12, 6, 3)
        compatible_layout.setSpacing(9)
        self.apply_compatible_separator = QFrame(self.apply_compatible_section)
        self.apply_compatible_separator.setObjectName("applyCompatibleSeparator")
        self.apply_compatible_separator.setFrameShape(QFrame.Shape.HLine)
        compatible_layout.addWidget(self.apply_compatible_separator)
        self.apply_compatible = Switch(self.apply_compatible_section)
        self.apply_compatible.setAccessibleName(
            "Aplicar misma configuración al resto de documentos"
        )
        self.apply_compatible_row = _switch_row(
            "Aplicar misma configuración al resto de documentos",
            self.apply_compatible,
            self.apply_compatible_section,
        )
        self.apply_compatible_row.setToolTip(
            f"Se aplicará a {compatible_job_count} documento"
            f"{'s' if compatible_job_count != 1 else ''} compatible"
            f"{'s' if compatible_job_count != 1 else ''}. "
            "Conserva el intervalo de páginas y la preferencia de OCR propios de cada documento."
        )
        compatible_layout.addWidget(self.apply_compatible_row)
        self.apply_compatible_section.setVisible(compatible_job_count > 0)
        navigation_layout.addWidget(self.apply_compatible_section)
        navigation_layout.addStretch(1)

        self.detail_panel = QFrame(self.configuration_body)
        self.detail_panel.setObjectName("configurationDetail")
        detail_layout = QVBoxLayout(self.detail_panel)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(6)
        self.compact_detail_back = QPushButton("Todas las opciones", self.detail_panel)
        self.compact_detail_back.setObjectName("configurationLocalBack")
        self.compact_detail_back.setAccessibleName("Volver al resumen de configuración")
        self.compact_detail_back.clicked.connect(self._show_compact_navigation)
        self.compact_detail_back.hide()
        detail_layout.addWidget(self.compact_detail_back)
        detail_layout.addWidget(self.tabs, 1)

        body_layout.addWidget(self.navigation_panel, 0, 0)
        body_layout.addWidget(self.detail_panel, 0, 1)
        body_layout.setColumnStretch(0, 0)
        body_layout.setColumnStretch(1, 1)
        self.navigation_panel.setFixedWidth(264)
        layout.addWidget(self.configuration_body, 1)

        # Compatibility selector retained for extensions. The visible
        # navigation is now the adaptive plan list.
        self.compact_navigation = ChevronComboBox(self)
        self.compact_navigation.setAccessibleName("Sección de configuración")
        for label in ("Resultado", "Traducir", "Corregir", "Personalizar", "IA local"):
            self.compact_navigation.addItem(label)
        self.compact_navigation.currentIndexChanged.connect(self.tabs.setCurrentIndex)
        self.tabs.currentChanged.connect(self.compact_navigation.setCurrentIndex)
        self.compact_navigation.hide()
        self.tabs.currentChanged.connect(self._sync_navigation_selection)

        self.validation_label = QLabel(self)
        self.validation_label.setObjectName("configurationValidation")
        self.validation_label.setWordWrap(True)
        self.validation_label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.validation_label.hide()
        layout.addWidget(self.validation_label)

        self.flow_summary = QLabel(self)
        self.flow_summary.setObjectName("configurationFlowSummary")
        self.flow_summary.setWordWrap(True)
        self.flow_summary.hide()
        # Stable aliases used by existing presentation integrations.
        self.output_group = self.output_page
        self.processing_group = self.tabs
        self.translation_options = self.translation_page
        self.refinement_options = self.refinement_page
        self.translation_model = self.ai_model
        self.refinement_model = self.ai_model
        self.translation_context = self.ai_context
        self.refinement_context = self.ai_context

        self._connect_changes()
        self._load(job.configuration)
        self._loading = False
        self._dirty = False
        self._select_requested_tab(stage)
        self._refresh_visibility()
        self._apply_embedded_styles()
        self.setMinimumWidth(0)

    def _build_ai_panel(
        self,
        models: tuple[tuple[str, str], ...],
    ) -> QFrame:
        panel = QFrame(self)
        panel.setObjectName("configurationAiProfile")
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)

        title = QLabel("IA local para este documento", panel)
        title.setObjectName("aiProfileTitle")
        outer.addWidget(title)
        self.ai_usage_summary = QLabel(panel)
        self.ai_usage_summary.setObjectName("aiUsageSummary")
        self.ai_usage_summary.setWordWrap(True)
        outer.addWidget(self.ai_usage_summary)
        self.ai_status_label = QLabel(panel)
        self.ai_status_label.setObjectName("aiStatus")
        self.ai_status_label.setWordWrap(True)
        outer.addWidget(self.ai_status_label)

        summary_row = QHBoxLayout()
        summary_row.setContentsMargins(0, 0, 0, 0)
        summary_row.setSpacing(14)
        self.ai_model_summary = QLabel("Sin modelo", panel)
        self.ai_model_summary.setObjectName("aiProfileValue")
        summary_row.addWidget(self.ai_model_summary)
        summary_row.addWidget(_separator_dot(panel))
        self.ai_context_summary = QLabel("Contexto automático", panel)
        self.ai_context_summary.setObjectName("aiProfileContext")
        summary_row.addWidget(self.ai_context_summary)
        summary_row.addStretch(1)
        self.change_ai_button = QPushButton("Cambiar", panel)
        self.change_ai_button.setObjectName("aiProfileChange")
        self.change_ai_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.change_ai_button.clicked.connect(self._toggle_ai_editor)
        summary_row.addWidget(self.change_ai_button)
        outer.addLayout(summary_row)

        default_row = QWidget(panel)
        default_layout = QHBoxLayout(default_row)
        default_layout.setContentsMargins(0, 4, 0, 0)
        default_layout.setSpacing(12)
        default_label = QLabel("Usar el modelo predeterminado", default_row)
        default_label.setObjectName("processDescription")
        default_layout.addWidget(default_label)
        default_layout.addStretch(1)
        self.ai_use_default = Switch(default_row)
        self.ai_use_default.setAccessibleName("Usar el modelo predeterminado de IA local")
        default_layout.addWidget(self.ai_use_default)
        outer.addWidget(default_row)

        self.ai_editor = QWidget(panel)
        ai_form = QFormLayout(self.ai_editor)
        ai_form.setContentsMargins(0, 6, 0, 0)
        ai_form.setVerticalSpacing(8)
        ai_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        ai_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        ai_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.ai_model = _model_combo(models, self.ai_editor)
        self.ai_model.activated.connect(self._handle_model_choice)
        self.ai_context = _context_spin(self.ai_editor)
        self.manage_models_button = QPushButton("Administrar modelos…", self.ai_editor)
        self.manage_models_button.clicked.connect(self.models_requested.emit)
        model_row = QWidget(self.ai_editor)
        model_layout = QHBoxLayout(model_row)
        model_layout.setContentsMargins(0, 0, 0, 0)
        model_layout.addWidget(self.ai_model, 1)
        model_layout.addWidget(self.manage_models_button)
        ai_form.addRow("Modelo", model_row)
        ai_form.addRow("Contexto", self.ai_context)
        context_help = QLabel(
            "Cantidad de contenido que la IA puede considerar en cada bloque. "
            "Un valor mayor consume más memoria y normalmente no mejora documentos largos.",
            self.ai_editor,
        )
        context_help.setObjectName("aiContextHelp")
        context_help.setWordWrap(True)
        ai_form.addRow("", context_help)
        outer.addWidget(self.ai_editor)
        self.ai_editor.hide()
        return panel

    def _build_output_page(self) -> QWidget:
        page = QWidget(self.tabs)
        form = QFormLayout(page)
        form.setContentsMargins(28, 24, 28, 24)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.output_form = form

        self.output_format = ChevronComboBox(page)
        output_model = QStandardItemModel(self.output_format)
        self.output_format.setModel(output_model)
        for document_format, label in (
            (DocumentFormat.TEXT, "Texto (.txt)"),
            (DocumentFormat.MARKDOWN, "Markdown (.md)"),
            (DocumentFormat.DOCX, "Word (.docx)"),
            (DocumentFormat.EPUB, "Libro EPUB (.epub)"),
        ):
            self.output_format.addItem(label, document_format)
            output_model.item(self.output_format.count() - 1).setEnabled(
                document_format in SUPPORTED_OUTPUTS[self._job.source.format]
            )
        form.addRow("Formato", self.output_format)

        self.page_range_enabled = Switch(page)
        self.page_range_enabled.setAccessibleName("Procesar solo un intervalo")
        self.page_range_enabled_row = _switch_row(
            "Procesar solo un intervalo",
            self.page_range_enabled,
            page,
        )
        self.page_first = QSpinBox(page)
        self.page_last = QSpinBox(page)
        for control in (self.page_first, self.page_last):
            control.setRange(1, 999_999)
        page_range_row = QWidget(page)
        page_range_layout = QHBoxLayout(page_range_row)
        page_range_layout.setContentsMargins(0, 0, 0, 0)
        page_range_layout.addWidget(QLabel("De", page_range_row))
        page_range_layout.addWidget(self.page_first)
        page_range_layout.addWidget(QLabel("a", page_range_row))
        page_range_layout.addWidget(self.page_last)
        page_range_layout.addStretch(1)
        form.addRow("", self.page_range_enabled_row)
        form.addRow("Páginas", page_range_row)
        self.page_range_row = page_range_row

        self.output_destination_button = QPushButton(page)
        self.output_destination_button.setObjectName("outputDestinationButton")
        self.output_destination_button.setAccessibleName("Elegir carpeta de destino")
        self.output_destination_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.output_destination_button.clicked.connect(self._choose_output_directory)
        form.addRow("Destino", self.output_destination_button)

        # Hidden compatibility state: the single destination button now owns
        # both the inherited global path and a per-document exception.
        self.output_destination_summary = self.output_destination_button
        self.custom_output_enabled = QCheckBox(page)
        self.custom_output_enabled.hide()
        self.output_directory = QLineEdit(page)
        self.output_directory.hide()
        self.output_row = self.output_destination_button

        self.include_images = Switch(page)
        self.include_images.setObjectName("processSwitch")
        self.include_images.setAccessibleName("Incluir imágenes")
        self.include_images_row = _switch_row(
            "Incluir imágenes",
            self.include_images,
            page,
        )
        self.image_directory = QLineEdit(page)
        self.image_directory.setPlaceholderText("Carpeta automática")
        self.image_browse = QPushButton("Elegir…", page)
        self.image_browse.clicked.connect(self._choose_image_directory)
        self.image_reset = QPushButton("Restaurar", page)
        self.image_reset.clicked.connect(self.image_directory.clear)
        self.image_buttons = QWidget(page)
        image_layout = QGridLayout(self.image_buttons)
        self.image_layout = image_layout
        image_layout.setContentsMargins(0, 0, 0, 0)
        image_layout.setHorizontalSpacing(8)
        image_layout.setVerticalSpacing(8)
        image_layout.addWidget(self.image_directory, 0, 0)
        image_layout.addWidget(self.image_browse, 0, 1)
        image_layout.addWidget(self.image_reset, 0, 2)
        image_layout.setColumnStretch(0, 1)
        form.addRow("", self.include_images_row)
        form.addRow("Guardar imágenes en", self.image_buttons)

        self.preserve_styles = Switch(page)
        self.preserve_styles.setObjectName("processSwitch")
        self.preserve_styles.setAccessibleName("Conservar estilos compatibles")
        self.preserve_styles_row = _switch_row(
            "Conservar estilos compatibles",
            self.preserve_styles,
            page,
        )
        form.addRow("", self.preserve_styles_row)
        return page

    def _build_personalization_page(self) -> QWidget:
        page = QWidget(self.tabs)
        layout = QVBoxLayout(page)
        self.personalization_layout = layout
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)

        # Metadata and cover now live in the final EPUB editor. Hidden controls
        # keep older saved jobs and preliminary EPUB generation compatible.
        self.book_title = QLineEdit(page)
        self.book_title.hide()
        self.book_author = QLineEdit(page)
        self.book_author.hide()
        self.restore_book_information = QPushButton("Restaurar datos originales", page)
        self.restore_book_information.hide()
        self.restore_book_information.clicked.connect(self._restore_book_information)
        self.cover_strategy = ChevronComboBox(page)
        self.cover_strategy.hide()
        self.cover_strategy.addItem(
            (
                "Conservar portada original"
                if self._job.source.format is DocumentFormat.EPUB
                else "Sin portada"
            ),
            CoverStrategy.NONE,
        )
        if self._job.source.format is DocumentFormat.EPUB:
            self.cover_strategy.addItem("Quitar portada", CoverStrategy.REMOVE)
        if self._job.source.format is DocumentFormat.PDF:
            self.cover_strategy.addItem("Usar la primera página", CoverStrategy.FIRST_PAGE)
        self.cover_strategy.addItem("Elegir una imagen", CoverStrategy.CUSTOM)
        self.cover_path = QLineEdit(page)
        self.cover_path.hide()
        self.cover_row = self.cover_path

        self.structure_enabled = Switch(page)
        self.structure_enabled.setObjectName("processSwitch")
        self.structure_enabled.setAccessibleName(
            "Activar o desactivar la pre-organización de capítulos"
        )
        self.structure_enabled.setToolTip("Activar o desactivar la pre-organización de capítulos")
        self.structure_enabled.setCursor(Qt.CursorShape.PointingHandCursor)
        editor_notice = QLabel(
            "Todos los resultados EPUB se abrirán en el editor antes de publicarse.",
            page,
        )
        editor_notice.setObjectName("personalizationNotice")
        editor_notice.setWordWrap(True)
        layout.addWidget(editor_notice)
        self.structure_summary = QLabel(
            "Pre-organizar capítulos y jerarquías con IA",
            page,
        )
        self.structure_summary.setObjectName("processDescription")
        self.structure_summary.setWordWrap(True)
        personalization_row = QWidget(page)
        personalization_layout = QHBoxLayout(personalization_row)
        personalization_layout.setContentsMargins(0, 0, 0, 0)
        personalization_layout.setSpacing(12)
        personalization_layout.addWidget(self.structure_summary)
        personalization_layout.addStretch(1)
        personalization_layout.addWidget(self.structure_enabled)
        layout.addWidget(personalization_row)
        layout.addStretch(1)
        self.structure_review = QCheckBox(page)
        self.structure_review.setChecked(True)
        self.structure_review.hide()
        return page

    def _build_translation_page(self) -> QWidget:
        page = QWidget(self.tabs)
        layout = QFormLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(10)
        layout.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.translation_form = layout
        self.translation_enabled = Switch(page)
        self.translation_enabled.setObjectName("processSwitch")
        self.translation_compact_switch = Switch(page)
        self.translation_compact_switch.setAccessibleName("Traducir este documento")
        self.translation_compact_switch.toggled.connect(self.translation_enabled.setChecked)
        self.translation_enabled.toggled.connect(self.translation_compact_switch.setChecked)
        self.translation_compact_row = _switch_row(
            "Traducir este documento",
            self.translation_compact_switch,
            page,
        )
        self.translation_compact_row.hide()
        layout.addRow("", self.translation_compact_row)

        method_row = QFrame(page)
        method_row.setObjectName("translationMethodSelector")
        self.translation_method_selector = method_row
        method_layout = QHBoxLayout(method_row)
        method_layout.setContentsMargins(3, 3, 3, 3)
        method_layout.setSpacing(0)
        self.method_group = QButtonGroup(self)
        self.method_group.setExclusive(True)
        self.offline_method_button = _method_button("Algoritmo local", page)
        self.ai_method_button = _method_button("IA local", page)
        self.method_group.addButton(self.offline_method_button, 0)
        self.method_group.addButton(self.ai_method_button, 1)
        method_layout.addWidget(self.offline_method_button, 1)
        method_layout.addWidget(self.ai_method_button, 1)
        layout.addRow("Método", method_row)

        # Hidden compatibility control; the visible method cards are authoritative.
        self.translation_method = ChevronComboBox(page)
        self.translation_method.addItem("Algoritmo local", TranslationMethod.OFFLINE)
        self.translation_method.addItem("IA local", TranslationMethod.LOCAL_AI)
        self.translation_method.hide()

        self.target_language = ChevronComboBox(page)
        for name in TARGET_LANGUAGE_CODES:
            self.target_language.addItem(name)
        self.target_language.setFixedHeight(34)
        layout.addRow("Traducir a", self.target_language)
        self.glossary_editor = self._build_glossary_editor(page)
        layout.addRow("Glosario", self.glossary_editor)
        # Compatibility alias for integrations that previously opened a modal.
        self.glossary_button = self.glossary_add_button
        self.translation_review = QCheckBox(page)
        self.translation_review.setChecked(True)
        self.translation_review.hide()
        return page

    def _build_glossary_editor(self, parent: QWidget) -> QWidget:
        editor = QWidget(parent)
        editor.setObjectName("inlineGlossary")
        outer = QVBoxLayout(editor)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)

        self.glossary_table = QTableWidget(0, 3, editor)
        self.glossary_table.setObjectName("inlineGlossaryTable")
        self.glossary_table.setHorizontalHeaderLabels(("Texto original", "Sustituir por", ""))
        self.glossary_table.horizontalHeader().setSectionResizeMode(
            0,
            QHeaderView.ResizeMode.Stretch,
        )
        self.glossary_table.horizontalHeader().setSectionResizeMode(
            1,
            QHeaderView.ResizeMode.Stretch,
        )
        self.glossary_table.horizontalHeader().setSectionResizeMode(
            2,
            QHeaderView.ResizeMode.Fixed,
        )
        self.glossary_table.setColumnWidth(2, 44)
        self.glossary_table.verticalHeader().hide()
        self.glossary_table.verticalHeader().setDefaultSectionSize(30)
        self.glossary_table.setShowGrid(False)
        self.glossary_table.setMinimumHeight(78)
        self.glossary_table.setMaximumHeight(174)
        self.glossary_table.setAccessibleName("Términos del glosario")
        self.glossary_table.itemChanged.connect(self._glossary_changed)
        outer.addWidget(self.glossary_table)

        self.glossary_add_button = QPushButton("Añadir término", editor)
        self.glossary_add_button.setObjectName("glossaryAddRow")
        self.glossary_add_button.setAccessibleName("Añadir un término al glosario")
        self.glossary_add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.glossary_add_button.clicked.connect(self._add_glossary_row)
        outer.addWidget(self.glossary_add_button)

        # Compatibility alias: removal now lives beside every individual row.
        self.glossary_remove_button = QPushButton(editor)
        self.glossary_remove_button.hide()
        return editor

    def _build_refinement_page(self) -> QWidget:
        page = QWidget(self.tabs)
        layout = QVBoxLayout(page)
        self.refinement_layout = layout
        layout.setContentsMargins(28, 24, 28, 24)
        self.refinement_enabled = Switch(page)
        self.refinement_enabled.setObjectName("processSwitch")
        self.refinement_compact_switch = Switch(page)
        self.refinement_compact_switch.setAccessibleName("Corregir este documento")
        self.refinement_compact_switch.toggled.connect(self.refinement_enabled.setChecked)
        self.refinement_enabled.toggled.connect(self.refinement_compact_switch.setChecked)
        self.refinement_compact_row = _switch_row(
            "Corregir este documento",
            self.refinement_compact_switch,
            page,
        )
        self.refinement_compact_row.hide()
        self.refinement_summary = QLabel(
            "Corrige errores gramaticales, ruido de conversión y artefactos evidentes. "
            "Los cambios se revisarán antes de aplicarse.",
            page,
        )
        self.refinement_summary.setObjectName("processDescription")
        self.refinement_summary.setWordWrap(True)
        layout.addWidget(self.refinement_compact_row)
        layout.addWidget(self.refinement_summary)
        layout.addStretch(1)
        self.refinement_review = QCheckBox(page)
        self.refinement_review.setChecked(True)
        self.refinement_review.hide()
        return page

    def _install_tab_switch(
        self,
        tab_index: int,
        switch: QCheckBox,
        accessible_name: str,
    ) -> None:
        switch.setAccessibleName(accessible_name)
        switch.setCursor(Qt.CursorShape.PointingHandCursor)
        switch.setToolTip(accessible_name)
        holder = QWidget(self.tabs.tabBar())
        holder_layout = QHBoxLayout(holder)
        holder_layout.setContentsMargins(4, 0, 16, 0)
        holder_layout.setSpacing(0)
        holder_layout.addWidget(switch)
        self.tabs.tabBar().setTabButton(
            tab_index,
            self.tabs.tabBar().ButtonPosition.RightSide,
            holder,
        )

    def _connect_changes(self) -> None:
        self.output_format.currentIndexChanged.connect(self._changed)
        self.page_range_enabled.toggled.connect(self._changed)
        self.page_first.valueChanged.connect(self._changed)
        self.page_last.valueChanged.connect(self._changed)
        self.custom_output_enabled.toggled.connect(self._changed)
        self.output_directory.textChanged.connect(self._changed)
        self.include_images.toggled.connect(self._changed)
        self.image_directory.textChanged.connect(self._changed)
        self.preserve_styles.toggled.connect(self._changed)
        self.book_title.textChanged.connect(self._changed)
        self.book_author.textChanged.connect(self._changed)
        self.cover_strategy.currentIndexChanged.connect(self._changed)
        self.cover_path.textChanged.connect(self._changed)
        self.translation_enabled.toggled.connect(self._changed)
        self.refinement_enabled.toggled.connect(self._changed)
        self.structure_enabled.toggled.connect(self._changed)
        self.target_language.currentTextChanged.connect(self._changed)
        self.translation_method.currentIndexChanged.connect(self._method_combo_changed)
        self.offline_method_button.clicked.connect(
            lambda: self.translation_method.setCurrentIndex(0)
        )
        self.ai_method_button.clicked.connect(lambda: self.translation_method.setCurrentIndex(1))
        self.ai_model.currentIndexChanged.connect(self._changed)
        self.ai_context.valueChanged.connect(self._changed)
        self.ai_use_default.toggled.connect(self._ai_scope_changed)
        self.apply_compatible.toggled.connect(self._changed)

    def _changed(self, *_args: object) -> None:
        if not self._loading:
            self._dirty = True
        self._refresh_visibility()

    def _ai_scope_changed(self, use_default: bool) -> None:
        if use_default:
            _set_combo_data(self.ai_model, self._default_ai_model)
            self.ai_context.setValue(self._default_ai_context or 0)
            self.ai_editor.hide()
        else:
            self.ai_editor.show()
            self.change_ai_button.setText("Ocultar opciones")
        self._changed()

    def _method_combo_changed(self, index: int) -> None:
        button = self.offline_method_button if index == 0 else self.ai_method_button
        button.setChecked(True)
        self._changed()

    def _toggle_ai_editor(self) -> None:
        if self.ai_use_default.isChecked():
            self.ai_use_default.setChecked(False)
            return
        self.ai_editor.setVisible(not self.ai_editor.isVisible())
        self.change_ai_button.setText(
            "Ocultar opciones" if self.ai_editor.isVisible() else "Mostrar opciones"
        )

    def _handle_model_choice(self, _index: int) -> None:
        if self.ai_model.currentData() == _MANAGE_MODELS or (
            self.ai_model.currentData() is None and self._available_model_count == 0
        ):
            self.ai_model.setCurrentIndex(0)
            self.models_requested.emit()
            return
        if self.ai_use_default.isChecked() and not self._loading:
            self.ai_use_default.setChecked(False)

    def set_models(self, models: tuple[tuple[str, str], ...]) -> None:
        selected = self.ai_model.currentData()
        self._available_model_count = len(models)
        self.ai_model.blockSignals(True)
        _populate_model_combo(self.ai_model, models)
        _set_combo_data(self.ai_model, selected)
        self.ai_model.blockSignals(False)
        self._refresh_visibility()

    def select_model(self, model_id: str, *, as_default: bool = True) -> bool:
        index = self.ai_model.findData(model_id)
        if index < 0:
            return False
        self.ai_model.setCurrentIndex(index)
        if as_default:
            self._default_ai_model = model_id
            if self.ai_use_default.isChecked():
                self.ai_use_default.setChecked(True)
        self._refresh_visibility()
        return True

    def set_default_ai_profile(self, model: str | None, context_window: int | None) -> None:
        self._default_ai_model = model
        self._default_ai_context = context_window
        if self.ai_use_default.isChecked():
            _set_combo_data(self.ai_model, model)
            self.ai_context.setValue(context_window or 0)
        self._refresh_visibility()

    def set_ai_status(self, status: OllamaStatus | None) -> None:
        self._ollama_status = status
        self._refresh_visibility()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.set_compact_mode(event.size().width() <= BREAKPOINTS.compact)

    def _select_section(self, index: int) -> None:
        if index == self.ai_tab_index and self.ai_navigation.isHidden():
            return
        if index == self.personalization_tab_index and self.personalization_navigation.isHidden():
            return
        self.tabs.setCurrentIndex(index)
        if self._compact:
            self._showing_compact_detail = True
            self.navigation_panel.hide()
            self.detail_panel.show()
            self.compact_detail_back.show()
        self._sync_navigation_selection()

    def _show_compact_navigation(self) -> None:
        if not self._compact:
            return
        self._showing_compact_detail = False
        self.detail_panel.hide()
        self.navigation_panel.show()
        self.compact_detail_back.hide()
        current = self._navigation_rows[self.tabs.currentIndex()]
        current.button.setFocus(Qt.FocusReason.OtherFocusReason)

    def _sync_navigation_selection(self, *_args: object) -> None:
        current = self.tabs.currentIndex()
        for index, row in enumerate(self._navigation_rows):
            row.set_selected(index == current)

    def set_compact_mode(self, compact: bool) -> None:
        """Adapt master-detail navigation and forms without losing any task."""

        if compact == self._compact:
            return
        self._compact = compact
        self.compact_navigation.hide()
        self.tabs.tabBar().hide()
        self.translation_compact_row.hide()
        self.refinement_compact_row.hide()
        for widget in (self.image_directory, self.image_browse, self.image_reset):
            self.image_layout.removeWidget(widget)
        self.output_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.translation_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        if compact:
            self.navigation_panel.setMinimumWidth(0)
            self.navigation_panel.setMaximumWidth(16_777_215)
            self.body_layout.addWidget(self.navigation_panel, 0, 0)
            self.body_layout.addWidget(self.detail_panel, 0, 0)
            if self._showing_compact_detail:
                self.navigation_panel.hide()
                self.detail_panel.show()
                self.compact_detail_back.show()
            else:
                self.navigation_panel.show()
                self.detail_panel.hide()
                self.compact_detail_back.hide()
            if not self._embedded:
                self.root_layout.setContentsMargins(12, 12, 12, 12)
            self.output_form.setContentsMargins(SPACING.md, SPACING.md, SPACING.md, SPACING.md)
            self.translation_form.setContentsMargins(SPACING.md, SPACING.md, SPACING.md, SPACING.md)
            self.personalization_layout.setContentsMargins(
                SPACING.md, SPACING.lg, SPACING.md, SPACING.lg
            )
            self.refinement_layout.setContentsMargins(
                SPACING.md, SPACING.lg, SPACING.md, SPACING.lg
            )
            self.ai_page_layout.setContentsMargins(SPACING.md, SPACING.lg, SPACING.md, SPACING.lg)
            self.image_layout.addWidget(self.image_directory, 0, 0, 1, 2)
            self.image_layout.addWidget(self.image_browse, 1, 0)
            self.image_layout.addWidget(self.image_reset, 1, 1)
        else:
            self._showing_compact_detail = False
            self.navigation_panel.show()
            self.detail_panel.show()
            self.compact_detail_back.hide()
            self.navigation_panel.setFixedWidth(264)
            self.body_layout.addWidget(self.navigation_panel, 0, 0)
            self.body_layout.addWidget(self.detail_panel, 0, 1)
            if not self._embedded:
                self.root_layout.setContentsMargins(24, 20, 24, 20)
            self.output_form.setContentsMargins(28, 24, 28, 24)
            self.translation_form.setContentsMargins(28, 24, 28, 24)
            self.personalization_layout.setContentsMargins(28, 24, 28, 24)
            self.refinement_layout.setContentsMargins(28, 24, 28, 24)
            self.ai_page_layout.setContentsMargins(28, 24, 28, 24)
            self.image_layout.addWidget(self.image_directory, 0, 0)
            self.image_layout.addWidget(self.image_browse, 0, 1)
            self.image_layout.addWidget(self.image_reset, 0, 2)

    def apply_theme(self) -> None:
        self._apply_embedded_styles()
        for switch in self.findChildren(Switch):
            switch.update()
        self.tabs.tabBar().update()

    def glossary_entries(self) -> tuple[GlossaryEntry, ...]:
        return validate_glossary(self._glossary_table_entries())

    def set_glossary_entries(self, entries: tuple[GlossaryEntry, ...]) -> None:
        self._glossary = tuple((entry.source, entry.target) for entry in entries)
        self._load_glossary_table(entries)
        self._dirty = True

    def configuration(self) -> JobConfiguration:
        try:
            output_format = DocumentFormat(self.output_format.currentData())
        except (TypeError, ValueError):
            raise ValueError("Selecciona un formato de salida compatible.") from None
        try:
            translation_method = TranslationMethod(self.translation_method.currentData())
        except (TypeError, ValueError):
            raise ValueError("Selecciona un método de traducción.") from None
        page_range = None
        if self._job.source.format is DocumentFormat.PDF and self.page_range_enabled.isChecked():
            page_range = PageRangeConfiguration(self.page_first.value(), self.page_last.value())
        output_directory = (
            _optional_path(self.output_directory.text())
            if self.custom_output_enabled.isChecked()
            else self._default_output_directory
        )
        include_images = self.include_images.isChecked()
        context_value = self.ai_context.value()
        try:
            glossary = self.glossary_entries()
        except RequestValidationError as exc:
            if self.translation_enabled.isChecked():
                raise ValueError(str(exc)) from None
            glossary = ()
        configuration = JobConfiguration(
            output=OutputConfiguration(
                configured=True,
                format=output_format,
                directory=output_directory,
                directory_is_custom=self.custom_output_enabled.isChecked(),
                include_images=include_images,
                image_directory=(
                    _optional_path(self.image_directory.text())
                    if output_format is DocumentFormat.MARKDOWN and include_images
                    else None
                ),
                preserve_styles=self.preserve_styles.isChecked(),
                title=None,
                author=None,
                cover_strategy=CoverStrategy.NONE,
                cover_path=None,
            ),
            ai=AIProfileConfiguration(
                model=_selected_model(self.ai_model),
                context_window=context_value or None,
                is_custom=not self.ai_use_default.isChecked(),
            ),
            translation=TranslationConfiguration(
                enabled=self.translation_enabled.isChecked(),
                method=translation_method,
                target_language=(
                    self.target_language.currentText().strip()
                    if self.translation_enabled.isChecked()
                    else None
                ),
                glossary=tuple((entry.source, entry.target) for entry in glossary),
                manual_review=True,
            ),
            refinement=RefinementConfiguration(
                enabled=self.refinement_enabled.isChecked(),
                manual_review=True,
            ),
            structure=StructureConfiguration(
                enabled=self.structure_enabled.isChecked(),
                manual_review=True,
            ),
            page_range=page_range,
            force_pdf_ocr=self._job.configuration.force_pdf_ocr,
        )
        issues = configuration_issues(self._job.source, configuration)
        if issues:
            raise ValueError(issues[0].message)
        return configuration

    def _draft_configuration(self) -> JobConfiguration:
        """Build a permissive draft so visibility and summaries never raise."""

        try:
            output_format = DocumentFormat(self.output_format.currentData())
        except (TypeError, ValueError):
            output_format = DocumentFormat.MARKDOWN
        try:
            method = TranslationMethod(self.translation_method.currentData())
        except (TypeError, ValueError):
            method = TranslationMethod.OFFLINE
        context_value = self.ai_context.value()
        return JobConfiguration(
            output=OutputConfiguration(
                configured=True,
                format=output_format,
                directory=(
                    _optional_path(self.output_directory.text())
                    if self.custom_output_enabled.isChecked()
                    else self._default_output_directory
                ),
                directory_is_custom=self.custom_output_enabled.isChecked(),
                include_images=self.include_images.isChecked(),
                image_directory=_optional_path(self.image_directory.text()),
                preserve_styles=self.preserve_styles.isChecked(),
                title=None,
                author=None,
                cover_strategy=CoverStrategy.NONE,
                cover_path=None,
            ),
            ai=AIProfileConfiguration(
                model=_selected_model(self.ai_model),
                context_window=context_value or None,
                is_custom=not self.ai_use_default.isChecked(),
            ),
            translation=TranslationConfiguration(
                enabled=self.translation_enabled.isChecked(),
                method=method,
                target_language=self.target_language.currentText().strip() or None,
                glossary=(self._glossary if self.translation_enabled.isChecked() else ()),
            ),
            refinement=RefinementConfiguration(enabled=self.refinement_enabled.isChecked()),
            structure=StructureConfiguration(enabled=self.structure_enabled.isChecked()),
            page_range=(
                PageRangeConfiguration(self.page_first.value(), self.page_last.value())
                if self._job.source.format is DocumentFormat.PDF
                and self.page_range_enabled.isChecked()
                else None
            ),
            force_pdf_ocr=self._job.configuration.force_pdf_ocr,
        )

    def _load(self, configuration: JobConfiguration) -> None:
        page_range = configuration.page_range
        self.page_range_enabled.setChecked(page_range is not None)
        self.page_first.setValue(page_range.first_page if page_range is not None else 1)
        self.page_last.setValue(page_range.last_page if page_range is not None else 1)
        self.translation_enabled.setChecked(configuration.translation.enabled)
        _set_combo_data(self.translation_method, configuration.translation.method)
        self.offline_method_button.setChecked(
            configuration.translation.method is TranslationMethod.OFFLINE
        )
        self.ai_method_button.setChecked(
            configuration.translation.method is TranslationMethod.LOCAL_AI
        )
        self.target_language.setCurrentText(configuration.translation.target_language or "")
        self.refinement_enabled.setChecked(configuration.refinement.enabled)
        self.structure_enabled.setChecked(configuration.structure.enabled)
        _set_combo_data(self.output_format, configuration.output.format)
        profile = effective_ai_profile(configuration)
        selected_model = profile.model if profile.is_custom else self._default_ai_model
        selected_context = profile.context_window if profile.is_custom else self._default_ai_context
        _set_combo_data(self.ai_model, selected_model)
        self.ai_context.setValue(selected_context or 0)
        self.ai_use_default.setChecked(not profile.is_custom)
        self.ai_editor.setVisible(profile.is_custom)
        custom_directory = configuration.output.directory_is_custom
        self.custom_output_enabled.setChecked(custom_directory)
        self.output_directory.setText(
            str(configuration.output.directory)
            if custom_directory and configuration.output.directory
            else ""
        )
        self.include_images.setChecked(configuration.output.include_images)
        self.image_directory.setText(
            str(configuration.output.image_directory)
            if configuration.output.image_directory
            else ""
        )
        self.preserve_styles.setChecked(configuration.output.preserve_styles)
        configured_title = configuration.output.title
        if (
            self._job.source.format is DocumentFormat.EPUB
            and not configuration.output.configured
            and configured_title == self._job.source.path.stem
        ):
            configured_title = None
        self.book_title.setText(configured_title or self._source_book_title)
        self.book_author.setText(
            configuration.output.author
            if configuration.output.author is not None
            else self._source_book_author
        )
        _set_combo_data(self.cover_strategy, configuration.output.cover_strategy)
        self.cover_path.setText(
            str(configuration.output.cover_path) if configuration.output.cover_path else ""
        )
        self.refinement_review.setChecked(True)
        self.structure_review.setChecked(True)
        self._load_glossary_table(
            tuple(GlossaryEntry(source, target) for source, target in self._glossary)
        )

    def _select_requested_tab(self, stage: StageKind | None) -> None:
        indexes = {
            StageKind.TRANSLATE: self.translation_tab_index,
            StageKind.REFINE: self.refinement_tab_index,
            StageKind.STRUCTURE: self.structure_tab_index,
            StageKind.PUBLISH: self.output_tab_index,
        }
        index = indexes[stage] if stage in indexes else self.output_tab_index
        self._select_section(index)

    def _refresh_visibility(self) -> None:
        if not hasattr(self, "output_format"):
            return
        draft = self._draft_configuration()
        output_format = draft.output.format
        is_epub = output_format is DocumentFormat.EPUB
        image_capable = output_format in {
            DocumentFormat.MARKDOWN,
            DocumentFormat.DOCX,
            DocumentFormat.EPUB,
        }

        pdf_source = self._job.source.format is DocumentFormat.PDF
        self.page_range_enabled_row.setVisible(pdf_source)
        _set_form_field_visible(
            self.output_form,
            self.page_range_row,
            pdf_source and self.page_range_enabled.isChecked(),
        )
        destination = (
            _optional_path(self.output_directory.text())
            if self.custom_output_enabled.isChecked()
            else self._default_output_directory
        )
        destination_text = (
            "Junto al documento original" if destination is None else str(destination)
        )
        self.output_destination_button.setText(destination_text)
        self.output_destination_button.setToolTip(destination_text)
        self.include_images_row.setVisible(image_capable)
        _set_form_field_visible(
            self.output_form,
            self.image_buttons,
            output_format is DocumentFormat.MARKDOWN and self.include_images.isChecked(),
        )
        self.preserve_styles_row.setVisible(
            self._job.source.format is output_format
            and output_format in {DocumentFormat.DOCX, DocumentFormat.EPUB}
        )
        self.tabs.setTabVisible(self.personalization_tab_index, is_epub)
        self.personalization_navigation.setVisible(is_epub)

        if not is_epub and self.structure_enabled.isChecked():
            self._structure_restore = True
            self.structure_enabled.blockSignals(True)
            self.structure_enabled.setChecked(False)
            self.structure_enabled.blockSignals(False)
        elif is_epub and self._structure_restore and not self.structure_enabled.isChecked():
            self.structure_enabled.blockSignals(True)
            self.structure_enabled.setChecked(True)
            self.structure_enabled.blockSignals(False)
            self._structure_restore = False
        self.structure_enabled.setEnabled(is_epub)
        self.structure_enabled.setToolTip(
            "Activar o desactivar la pre-organización de capítulos y jerarquías"
            if is_epub
            else "Disponible únicamente para resultados EPUB."
        )
        self.tabs.setTabToolTip(
            self.personalization_tab_index,
            "" if is_epub else "Disponible únicamente para resultados EPUB.",
        )
        self.structure_summary.setText("Pre-organizar capítulos y jerarquías con IA")
        translation_active = self.translation_enabled.isChecked()
        for control in (
            self.translation_method_selector,
            self.target_language,
            self.glossary_editor,
        ):
            control.setEnabled(translation_active)
        self.refinement_summary.setEnabled(self.refinement_enabled.isChecked())
        self.structure_summary.setEnabled(is_epub and self.structure_enabled.isChecked())

        draft = self._draft_configuration()
        ai_needed = requires_ai(draft)
        self.tabs.setTabVisible(self.ai_tab_index, ai_needed)
        self.ai_navigation.setVisible(ai_needed)
        current = self.tabs.currentIndex()
        if (current == self.personalization_tab_index and not is_epub) or (
            current == self.ai_tab_index and not ai_needed
        ):
            self.tabs.setCurrentIndex(self.output_tab_index)
        self._refresh_ai_summary(draft)
        self._refresh_flow_summary(draft)
        self._refresh_validation(draft)
        self._sync_navigation_selection()

    def _refresh_ai_summary(self, draft: JobConfiguration) -> None:
        model_text = self.ai_model.currentText()
        if _selected_model(self.ai_model) is None:
            model_text = "Sin modelo"
        scope = "Específico" if draft.ai.is_custom else "Predeterminado"
        self.ai_model_summary.setText(model_text)
        context_value = self.ai_context.value()
        self.ai_context_summary.setText(
            f"Contexto automático · {_format_integer(_DEFAULT_CONTEXT)} tokens"
            if context_value == 0
            else f"{_format_integer(context_value)} tokens"
        )
        needed = requires_ai(draft)
        consumers: list[str] = []
        if draft.translation.enabled and draft.translation.method is TranslationMethod.LOCAL_AI:
            consumers.append("Traducir")
        if draft.refinement.enabled:
            consumers.append("Corregir")
        if draft.structure.enabled:
            consumers.append("Pre-organizar capítulos")
        self.ai_usage_summary.setText(
            "Compartida por: " + ", ".join(consumers)
            if consumers
            else "No es necesaria para el plan actual."
        )
        missing = needed and _selected_model(self.ai_model) is None
        status = {
            None: "Sin comprobar",
            OllamaStatus.READY: "Lista",
            OllamaStatus.NOT_INSTALLED: "Ollama no está instalado",
            OllamaStatus.STOPPED: "Ollama está detenido",
            OllamaStatus.MISSING_MODEL: "Falta un modelo compatible",
            OllamaStatus.LOCAL_ONLY_REQUIRED: "Requiere activar el modo solo local",
            OllamaStatus.UNAVAILABLE: "No disponible",
        }[self._ollama_status]
        if missing:
            status = "Acción necesaria"
        status_problem = needed and self._ollama_status is not OllamaStatus.READY
        if missing:
            navigation_summary = status
        elif status_problem:
            navigation_summary = f"{status} · {scope}"
        else:
            navigation_summary = f"{scope} · {model_text}"
        self.ai_navigation.set_summary(navigation_summary)
        self.ai_status_label.setText(
            f"Estado: {status}. Todo el contenido permanece en este equipo."
        )
        tone = (
            "success"
            if self._ollama_status is OllamaStatus.READY and not missing
            else "warning"
            if self._ollama_status
            in {
                OllamaStatus.NOT_INSTALLED,
                OllamaStatus.STOPPED,
                OllamaStatus.MISSING_MODEL,
                OllamaStatus.LOCAL_ONLY_REQUIRED,
            }
            or missing
            else "muted"
        )
        self.ai_status_label.setProperty("tone", tone)
        self.ai_status_label.style().unpolish(self.ai_status_label)
        self.ai_status_label.style().polish(self.ai_status_label)
        self.ai_panel.setProperty("required", missing)
        custom = not self.ai_use_default.isChecked()
        self.ai_model.setEnabled(custom)
        self.ai_context.setEnabled(custom)
        self.ai_use_default.setEnabled(
            self._default_ai_model is not None or self.ai_use_default.isChecked()
        )
        self.change_ai_button.setText(
            "Usar otro modelo"
            if self.ai_use_default.isChecked()
            else ("Ocultar opciones" if self.ai_editor.isVisible() else "Mostrar opciones")
        )
        self.ai_panel.setToolTip(f"Estado de la IA local: {status}")
        self.ai_panel.style().unpolish(self.ai_panel)
        self.ai_panel.style().polish(self.ai_panel)

    def _refresh_flow_summary(self, draft: JobConfiguration) -> None:
        output = draft.output.format.value.upper()
        destination = (
            "destino específico" if draft.output.directory_is_custom else "destino general"
        )
        self.output_navigation.set_summary(f"{output} · {destination}")

        target = draft.translation.target_language or "idioma pendiente"
        if not draft.translation.enabled:
            translation_summary = "Desactivado"
        elif draft.translation.method is TranslationMethod.LOCAL_AI:
            translation_summary = f"{target} · IA local"
        else:
            translation_summary = f"{target} · Algoritmo local"
        self.translation_navigation.set_summary(translation_summary)

        if not draft.refinement.enabled:
            refinement_summary = "Desactivado"
        elif draft.translation.enabled and draft.translation.method is TranslationMethod.LOCAL_AI:
            refinement_summary = "Combinado con Traducir"
        else:
            refinement_summary = "Mediante IA local"
        self.refinement_navigation.set_summary(refinement_summary)

        if draft.output.format is DocumentFormat.EPUB:
            personalization_summary = (
                "Editor final · Preorganización con IA"
                if draft.structure.enabled
                else "Editor final · Organización manual"
            )
            self.personalization_navigation.set_summary(personalization_summary)

        operations: list[str] = []
        if (
            draft.translation.enabled
            and draft.refinement.enabled
            and draft.translation.method is TranslationMethod.LOCAL_AI
        ):
            operations.append(f"traducir a {target} y corregir con IA en una transformación")
        else:
            if draft.translation.enabled:
                method = (
                    "IA local"
                    if draft.translation.method is TranslationMethod.LOCAL_AI
                    else "algoritmo local"
                )
                operations.append(f"traducir a {target} con {method}")
            if draft.refinement.enabled:
                operations.append("corregir con IA local")
        if draft.structure.enabled:
            operations.append("pre-organizar capítulos con IA")
        processing = (
            "; después, ".join(operations) if operations else "sin transformaciones automáticas"
        )
        intervention = (
            "Después se abrirá Personalizar para revisar y publicar el EPUB."
            if draft.output.format is DocumentFormat.EPUB
            else "Solo se detendrá si existen propuestas que necesiten revisión."
        )
        ai_note = ""
        if requires_ai(draft):
            model = _selected_model(self.ai_model)
            ai_note = (
                f"\nIA local: {model}."
                if model
                else "\nIA local: falta elegir un modelo compatible."
            )
        elif draft.translation.enabled and draft.translation.method is TranslationMethod.OFFLINE:
            ai_note = "\nEl paquete de idioma se preparará si aún no está instalado."
        summary = (
            f"{self._job.source.format.value.upper()} → {output} · {destination}.\n"
            f"Procesamiento: {processing}. {intervention}{ai_note}"
        )
        self.plan_summary.setText(summary)
        self.flow_summary.setText(summary)

    def _refresh_validation(self, draft: JobConfiguration) -> None:
        issues = configuration_issues(self._job.source, draft)
        self._last_issues = issues
        self.validation_label.setVisible(bool(issues))
        self.validation_label.setText(issues[0].message if issues else "")

    def _submit(self, *, explain_failure: bool = False) -> bool:
        try:
            self.configuration()
        except ValueError as exc:
            message = str(exc)
            self.validation_label.setText(message)
            self.validation_label.setAccessibleName(f"Error de configuración: {message}")
            self.validation_label.show()
            if self._last_issues:
                self._focus_issue(self._last_issues[0])
            if explain_failure and not self._last_issues:
                self.validation_label.setFocus(Qt.FocusReason.OtherFocusReason)
            return False
        self._dirty = False
        self.save_requested.emit()
        return True

    def request_close(self) -> bool:
        """Autosave a valid draft before returning to the queue."""

        if not self._dirty:
            return True
        self._submit(explain_failure=True)
        return False

    def _cancel(self) -> None:
        """Compatibility hook for callers that explicitly abandon a draft."""

        self.cancel_requested.emit()

    def _focus_issue(self, issue: ConfigurationIssue) -> None:
        tab_index = {
            ConfigurationSection.RESULT: self.output_tab_index,
            ConfigurationSection.TRANSLATION: self.translation_tab_index,
            ConfigurationSection.REFINEMENT: self.refinement_tab_index,
            ConfigurationSection.STRUCTURE: self.structure_tab_index,
            ConfigurationSection.PERSONALIZATION: self.personalization_tab_index,
        }.get(issue.section)
        if tab_index is not None:
            self._select_section(tab_index)
            target = {
                ConfigurationSection.RESULT: self.output_destination_button,
                ConfigurationSection.TRANSLATION: self.target_language,
                ConfigurationSection.REFINEMENT: self.refinement_compact_switch,
                ConfigurationSection.STRUCTURE: self.structure_enabled,
                ConfigurationSection.PERSONALIZATION: self.structure_enabled,
            }.get(issue.section)
            if target is not None:
                target.setFocus(Qt.FocusReason.OtherFocusReason)
        elif issue.section is ConfigurationSection.AI:
            self._select_section(self.ai_tab_index)
            if _selected_model(self.ai_model) is None:
                self.manage_models_button.setFocus(Qt.FocusReason.OtherFocusReason)
            else:
                self.ai_model.setFocus(Qt.FocusReason.OtherFocusReason)

    def _edit_glossary(self) -> None:
        self._add_glossary_row()

    def _load_glossary_table(self, entries: tuple[GlossaryEntry, ...]) -> None:
        self.glossary_table.blockSignals(True)
        self.glossary_table.setRowCount(0)
        for entry in entries:
            self._append_glossary_entry(entry)
        self.glossary_table.blockSignals(False)

    def _append_glossary_entry(self, entry: GlossaryEntry) -> None:
        row = self.glossary_table.rowCount()
        self.glossary_table.insertRow(row)
        self.glossary_table.setItem(row, 0, QTableWidgetItem(entry.source))
        self.glossary_table.setItem(row, 1, QTableWidgetItem(entry.target))
        remove = QPushButton("×", self.glossary_table)
        remove.setObjectName("glossaryRemoveRow")
        remove.setAccessibleName(f"Eliminar término {row + 1}")
        remove.setToolTip("Eliminar este término")
        remove.setCursor(Qt.CursorShape.PointingHandCursor)
        remove.clicked.connect(
            lambda _checked=False, button=remove: self._remove_glossary_row(button)
        )
        self.glossary_table.setCellWidget(row, 2, remove)

    def _add_glossary_row(self) -> None:
        if self.glossary_table.rowCount() >= MAX_GLOSSARY_ENTRIES:
            return
        self._append_glossary_entry(GlossaryEntry("", ""))
        row = self.glossary_table.rowCount() - 1
        self.glossary_table.setCurrentCell(row, 0)
        item = self.glossary_table.currentItem()
        if item is not None:
            self.glossary_table.editItem(item)
        self._glossary_changed()

    def _remove_glossary_rows(self) -> None:
        selected = sorted(
            {index.row() for index in self.glossary_table.selectedIndexes()},
            reverse=True,
        )
        for row in selected:
            self.glossary_table.removeRow(row)
        self._glossary_changed()

    def _remove_glossary_row(self, button: QPushButton) -> None:
        for row in range(self.glossary_table.rowCount()):
            if self.glossary_table.cellWidget(row, 2) is button:
                self.glossary_table.removeRow(row)
                self._glossary_changed()
                return

    def _glossary_table_entries(self) -> tuple[GlossaryEntry, ...]:
        entries: list[GlossaryEntry] = []
        for row in range(self.glossary_table.rowCount()):
            source_item = self.glossary_table.item(row, 0)
            target_item = self.glossary_table.item(row, 1)
            source = source_item.text().strip() if source_item is not None else ""
            target = target_item.text().strip() if target_item is not None else ""
            if source or target:
                entries.append(GlossaryEntry(source, target))
        return tuple(entries)

    def _glossary_changed(self, *_args: object) -> None:
        self._glossary = tuple(
            (entry.source, entry.target) for entry in self._glossary_table_entries()
        )
        self._changed()

    def _choose_output_directory(self) -> None:
        current = (
            self.output_directory.text()
            if self.custom_output_enabled.isChecked()
            else str(self._default_output_directory or "")
        )
        selected = QFileDialog.getExistingDirectory(self, "Carpeta de salida", current)
        if selected:
            self.custom_output_enabled.setChecked(True)
            self.output_directory.setText(selected)

    def _choose_image_directory(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Carpeta para imágenes")
        if selected:
            self.image_directory.setText(selected)

    def _choose_cover(self) -> None:
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "Imagen de portada",
            filter="Imágenes (*.png *.jpg *.jpeg *.webp *.gif *.svg)",
        )
        if selected:
            self.cover_path.setText(selected)

    def _restore_book_information(self) -> None:
        self.book_title.setText(self._source_book_title)
        self.book_author.setText(self._source_book_author)

    def _apply_embedded_styles(self) -> None:
        self.setStyleSheet(
            f"""
            QDialog#jobConfigurationEditor {{
                background-color: {COLORS.canvas};
            }}
            QTabWidget#configurationTabs::pane {{
                background-color: transparent;
                border: none;
            }}
            QFrame#configurationPlan {{
                background-color: transparent;
                border: none;
                border-bottom: 1px solid {COLORS.divider};
            }}
            QLabel#configurationPlanTitle {{
                color: {COLORS.text_primary};
                font-weight: 650;
            }}
            QLabel#configurationPlanSummary,
            QLabel#aiUsageSummary,
            QLabel#aiContextHelp,
            QLabel#personalizationNotice {{
                color: {COLORS.text_secondary};
            }}
            QLabel#aiStatus {{
                color: {COLORS.text_muted};
                padding: 0;
                border: none;
                background-color: transparent;
            }}
            QLabel#aiStatus[tone="success"] {{
                color: {COLORS.success};
            }}
            QLabel#aiStatus[tone="warning"] {{
                color: {COLORS.warning};
            }}
            QFrame#flowNavigation {{
                background-color: transparent;
                border: none;
                border-right: 1px solid {COLORS.divider};
            }}
            QFrame#configurationDetail {{
                background-color: transparent;
                border: none;
            }}
            QFrame#flowNavigationRow {{
                background-color: transparent;
                border: none;
                border-radius: 6px;
            }}
            QFrame#flowNavigationRow[selected="true"] {{
                background-color: {COLORS.action_primary_soft};
                border: none;
            }}
            QFrame#flowNavigationRow[hovered="true"] {{
                background-color: {COLORS.surface_hover};
            }}
            QFrame#flowNavigationRow[focusWithin="true"] {{
                background-color: {COLORS.surface_hover};
                border: none;
            }}
            QFrame#flowNavigationRow[selected="true"][hovered="true"] {{
                background-color: {COLORS.action_primary_soft};
                border: none;
            }}
            QFrame#flowNavigationRow[selected="true"][focusWithin="true"] {{
                background-color: {COLORS.action_primary_soft};
                border: none;
            }}
            QPushButton#flowNavigationButton {{
                min-height: 48px;
                padding: 4px 8px;
                color: {COLORS.text_primary};
                background-color: transparent;
                border: none;
                text-align: left;
            }}
            QPushButton#flowNavigationButton:hover,
            QPushButton#flowNavigationButton:focus,
            QPushButton#flowNavigationButton:checked {{
                background-color: transparent;
                border: none;
            }}
            QFrame#applyCompatibleSection {{
                background-color: transparent;
                border: none;
            }}
            QFrame#applyCompatibleSeparator {{
                color: {COLORS.divider};
                background-color: {COLORS.divider};
                border: none;
                min-height: 1px;
                max-height: 1px;
            }}
            QPushButton#configurationLocalBack {{
                min-height: 34px;
                max-height: 34px;
                color: {COLORS.action_primary_hover};
                background-color: transparent;
                border: none;
                text-align: left;
            }}
            QFrame#configurationAiProfile {{
                background-color: transparent;
                border: none;
            }}
            QLabel#aiProfileTitle {{
                color: {COLORS.text_primary};
                font-size: 12pt;
                font-weight: 650;
            }}
            QLabel#aiProfileValue {{
                color: {COLORS.text_primary};
                font-weight: 600;
            }}
            QCheckBox#processSwitch {{
                min-width: 48px;
                max-width: 48px;
                min-height: 32px;
                max-height: 32px;
                padding: 0;
                background: transparent;
                border: none;
            }}
            QPushButton[methodChoice="true"] {{
                min-height: 30px;
                max-height: 30px;
                color: {COLORS.text_primary};
                background-color: transparent;
                border: none;
                border-radius: 14px;
            }}
            QPushButton[methodChoice="true"]:checked {{
                color: {COLORS.text_primary};
                background-color: {COLORS.action_primary_soft};
                border: none;
            }}
            QFrame#translationMethodSelector {{
                min-height: 36px;
                max-height: 36px;
                background-color: {COLORS.surface_subtle};
                border: 1px solid {COLORS.divider};
                border-radius: 6px;
            }}
            QPushButton#outputDestinationButton {{
                min-height: 36px;
                padding: 0 12px;
                color: {COLORS.text_primary};
                background-color: {COLORS.surface_raised};
                border: 1px solid {COLORS.border};
                border-radius: 6px;
                text-align: left;
            }}
            QPushButton#outputDestinationButton:hover,
            QPushButton#outputDestinationButton:focus {{
                border-color: {COLORS.border};
                background-color: {COLORS.action_primary_soft};
            }}
            QLabel#configurationValidation {{
                color: {COLORS.warning};
                padding: 0 6px;
            }}
            QLabel#processDescription {{
                color: {COLORS.text_secondary};
                font-size: 10.5pt;
            }}
            QTableWidget#inlineGlossaryTable {{
                color: {COLORS.text_primary};
                background-color: {COLORS.surface_raised};
                border: 1px solid {COLORS.divider};
                border-radius: 6px;
                gridline-color: transparent;
            }}
            QPushButton#glossaryAddRow {{
                min-height: 30px;
                max-height: 30px;
                color: {COLORS.action_primary_hover};
                background-color: transparent;
                border: none;
                border-radius: 6px;
                text-align: left;
            }}
            QPushButton#glossaryAddRow:hover,
            QPushButton#glossaryAddRow:focus {{
                background-color: {COLORS.action_primary_soft};
            }}
            QPushButton#glossaryAddRow:focus {{
                border: none;
            }}
            QPushButton#aiProfileChange {{
                color: {COLORS.action_primary_hover};
                background-color: transparent;
                border-color: transparent;
            }}
            QPushButton#aiProfileChange:focus {{
                background-color: {COLORS.action_primary_soft};
                border-color: transparent;
            }}
            QPushButton#glossaryRemoveRow {{
                min-width: 30px;
                max-width: 30px;
                min-height: 30px;
                max-height: 30px;
                padding: 0;
                color: {COLORS.text_muted};
                background-color: transparent;
                border: none;
                font-size: 15pt;
            }}
            QPushButton#glossaryRemoveRow:hover,
            QPushButton#glossaryRemoveRow:focus {{
                color: {COLORS.error};
                background-color: {COLORS.error_soft};
                border-radius: 6px;
            }}
            """
        )


def _model_combo(
    models: tuple[tuple[str, str], ...],
    parent: QWidget,
) -> QComboBox:
    combo = ChevronComboBox(parent)
    _populate_model_combo(combo, models)
    return combo


def _populate_model_combo(
    combo: QComboBox,
    models: tuple[tuple[str, str], ...],
) -> None:
    combo.clear()
    combo.addItem("Seleccionar modelo", None)
    for model_id, display_name in models:
        combo.addItem(display_name, model_id)
    combo.insertSeparator(combo.count())
    combo.addItem("Gestionar modelos de IA…", _MANAGE_MODELS)


def _context_spin(parent: QWidget) -> QSpinBox:
    spin = QSpinBox(parent)
    spin.setRange(0, 262_144)
    spin.setSingleStep(512)
    spin.setSpecialValueText("Automático")
    spin.setSuffix(" tokens")
    return spin


def _method_button(text: str, parent: QWidget) -> QPushButton:
    button = QPushButton(text, parent)
    button.setCheckable(True)
    button.setProperty("methodChoice", True)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    return button


def _separator_dot(parent: QWidget) -> QLabel:
    label = QLabel("·", parent)
    label.setStyleSheet(f"color: {COLORS.text_muted};")
    return label


def _field_with_button(
    field: QLineEdit,
    button: QPushButton,
    parent: QWidget,
) -> QWidget:
    container = QWidget(parent)
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(field, 1)
    layout.addWidget(button)
    return container


def _switch_row(text: str, switch: QCheckBox, parent: QWidget) -> QWidget:
    container = QWidget(parent)
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(12)
    label = QLabel(text, container)
    label.setWordWrap(True)
    label.setMinimumWidth(0)
    label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    label.setBuddy(switch)
    layout.addWidget(label, 1)
    layout.addWidget(switch)
    return container


def _optional_path(value: str) -> Path | None:
    normalized = value.strip()
    return Path(normalized) if normalized else None


def _set_combo_data(combo: QComboBox, value: object) -> None:
    index = combo.findData(value, role=Qt.ItemDataRole.UserRole)
    combo.setCurrentIndex(max(0, index))


def _selected_model(combo: QComboBox) -> str | None:
    value = combo.currentData()
    return value if isinstance(value, str) and value and value != _MANAGE_MODELS else None


def _set_form_field_visible(
    form: QFormLayout,
    field: QWidget,
    visible: bool,
) -> None:
    label = form.labelForField(field)
    if label is not None:
        label.setVisible(visible)
    field.setVisible(visible)


def _format_integer(value: int) -> str:
    return f"{value:,}".replace(",", ".")
