"""Compact, progressive document configuration sheet."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent, QKeyEvent, QMouseEvent, QResizeEvent
from PySide6.QtWidgets import (
    QBoxLayout,
    QButtonGroup,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from parsezen.application.configuration_rules import configuration_issues
from parsezen.application.processing_explanation import (
    processing_flow_steps,
    processing_pass_summary,
)
from parsezen.domain.jobs import (
    AIProfileConfiguration,
    DocumentFormat,
    DocumentJob,
    JobConfiguration,
    MarkdownOrganization,
    OutputConfiguration,
    PageRangeConfiguration,
    ProcessingPlan,
    TranslationConfiguration,
    TranslationMethod,
)
from parsezen.domain.stages import StageKind
from parsezen.errors import RequestValidationError
from parsezen.glossary import MAX_GLOSSARY_ENTRIES, GlossaryEntry, validate_glossary
from parsezen.local_models import OllamaStatus
from parsezen.presentation.components import ChevronComboBox, Switch
from parsezen.presentation.design_system import COLORS, SPACING
from parsezen.translation_quality import TARGET_LANGUAGE_CODES

_AI_DEFAULT_UNSET = object()


class _ChoiceCard(QFrame):
    """A whole-card radio target with concise, purpose-led copy."""

    def __init__(
        self,
        title: str,
        description: str,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("configurationChoice")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(SPACING.md, SPACING.sm, SPACING.md, SPACING.sm)
        layout.setSpacing(SPACING.sm)
        self.radio = QRadioButton(self)
        self.radio.setAccessibleName(title)
        self.radio.setAccessibleDescription(description)
        layout.addWidget(self.radio, 0, Qt.AlignmentFlag.AlignTop)
        copy = QVBoxLayout()
        copy.setSpacing(2)
        self.title = QLabel(title, self)
        self.title.setObjectName("choiceTitle")
        self.description = QLabel(description, self)
        self.description.setObjectName("choiceDescription")
        self.description.setWordWrap(True)
        self.description.setMinimumWidth(0)
        copy.addWidget(self.title)
        copy.addWidget(self.description)
        layout.addLayout(copy, 1)
        self.radio.toggled.connect(self._refresh_state)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() is Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.radio.setChecked(True)
            self.radio.setFocus(Qt.FocusReason.MouseFocusReason)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _refresh_state(self, checked: bool) -> None:
        self.setProperty("selected", checked)
        self.style().unpolish(self)
        self.style().polish(self)


class JobConfigurationDialog(QDialog):
    """Ask only for the desired result; keep exceptional controls behind one disclosure."""

    save_requested = Signal()
    cancel_requested = Signal()
    models_requested = Signal()

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
        self._embedded = embedded
        self._default_output_directory = default_output_directory
        self._default_ai_model = (
            job.configuration.ai.model
            if default_ai_model is _AI_DEFAULT_UNSET
            else default_ai_model
            if isinstance(default_ai_model, str)
            else None
        )
        self._default_ai_context = (
            job.configuration.ai.context_window
            if default_ai_context is _AI_DEFAULT_UNSET
            else default_ai_context
            if isinstance(default_ai_context, int) and not isinstance(default_ai_context, bool)
            else None
        )
        self._models = dict(models)
        self._ollama_status = ollama_status
        self._dirty = False
        self._loading = True
        self._compact = False

        self.setWindowTitle(f"Configurar · {job.source.path.name}")
        self.setObjectName("jobConfigurationEditor")
        self.setModal(not embedded)
        self.setWindowModality(
            Qt.WindowModality.NonModal if embedded else Qt.WindowModality.WindowModal
        )
        if embedded:
            self.setWindowFlags(Qt.WindowType.Widget)
        self.resize(680, 520)
        self.setMinimumWidth(0)

        root = QVBoxLayout(self)
        root.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        root.setContentsMargins(*(0, 0, 0, 0) if embedded else (24, 20, 24, 20))
        root.setSpacing(SPACING.md)

        heading = QLabel("¿Qué quieres crear?", self)
        heading.setObjectName("configurationHeading")
        root.addWidget(heading)
        intro = QLabel(
            "Elige el resultado. Parsezen se ocupa de la extracción, las comprobaciones y del "
            "OCR si hace falta, sin pedirte más decisiones.",
            self,
        )
        intro.setObjectName("configurationIntro")
        intro.setWordWrap(True)
        root.addWidget(intro)

        self.scroll_area = QScrollArea(self)
        self.scroll_area.setObjectName("configurationScroll")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content = QWidget(self.scroll_area)
        self.content.setMinimumWidth(0)
        self.content.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        content_layout = QVBoxLayout(self.content)
        content_layout.setContentsMargins(0, 0, SPACING.xs, 0)
        content_layout.setSpacing(SPACING.md)

        self.output_group = QButtonGroup(self)
        self.output_group.setExclusive(True)
        self.markdown_card = _ChoiceCard(
            "Markdown",
            "Para editar, buscar o usar en tus notas.",
            parent=self.content,
        )
        self.epub_card = _ChoiceCard(
            "EPUB",
            "Para leer como un libro en cualquier dispositivo.",
            parent=self.content,
        )
        self.output_markdown = self.markdown_card.radio
        self.output_epub = self.epub_card.radio
        self.output_group.addButton(self.output_markdown)
        self.output_group.addButton(self.output_epub)
        self.output_choices = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.output_choices.setSpacing(SPACING.sm)
        self.output_choices.addWidget(self.markdown_card, 1)
        self.output_choices.addWidget(self.epub_card, 1)
        content_layout.addLayout(self.output_choices)

        translation_row = QGridLayout()
        translation_row.setHorizontalSpacing(SPACING.md)
        translation_row.setVerticalSpacing(SPACING.xs)
        translation_label = QLabel("Traducción", self.content)
        translation_label.setObjectName("configurationFieldLabel")
        translation_row.addWidget(translation_label, 0, 0)
        self.translation_target = ChevronComboBox(self.content)
        self.translation_target.setMinimumWidth(0)
        self.translation_target.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Fixed,
        )
        self.translation_target.setAccessibleName("Traducción e idioma de destino")
        self.translation_target.addItem("No traducir", None)
        for display_name, code in TARGET_LANGUAGE_CODES.items():
            self.translation_target.addItem(f"Traducir a {display_name}", code)
        translation_row.addWidget(self.translation_target, 0, 1)
        translation_row.setColumnStretch(1, 1)
        content_layout.addLayout(translation_row)

        self.summary_card = QFrame(self.content)
        self.summary_card.setObjectName("configurationSummary")
        summary_layout = QVBoxLayout(self.summary_card)
        summary_layout.setContentsMargins(SPACING.md, SPACING.sm, SPACING.md, SPACING.sm)
        summary_layout.setSpacing(3)
        self.summary_title = QLabel(self.summary_card)
        self.summary_title.setObjectName("configurationSummaryTitle")
        self.summary_detail = QLabel(self.summary_card)
        self.summary_detail.setObjectName("configurationSummaryDetail")
        self.summary_detail.setWordWrap(True)
        self.destination_summary = QLabel(self.summary_card)
        self.destination_summary.setObjectName("inheritedSetting")
        self.destination_summary.setWordWrap(True)
        summary_layout.addWidget(self.summary_title)
        summary_layout.addWidget(self.summary_detail)
        summary_layout.addWidget(self.destination_summary)
        content_layout.addWidget(self.summary_card)

        self.advanced_toggle = QPushButton("Más opciones", self.content)
        self.advanced_toggle.setObjectName("configurationDisclosure")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setAccessibleName("Mostrar más opciones de procesamiento")
        content_layout.addWidget(self.advanced_toggle, 0, Qt.AlignmentFlag.AlignLeft)

        self.advanced_panel = QFrame(self.content)
        self.advanced_panel.setObjectName("configurationAdvanced")
        advanced_layout = QVBoxLayout(self.advanced_panel)
        advanced_layout.setContentsMargins(SPACING.md, SPACING.md, SPACING.md, SPACING.md)
        advanced_layout.setSpacing(SPACING.md)

        self.review_row = QWidget(self.advanced_panel)
        review_layout = QGridLayout(self.review_row)
        review_layout.setContentsMargins(0, 0, 0, 0)
        review_layout.setHorizontalSpacing(SPACING.md)
        review_title = QLabel("Revisar todo con IA", self.review_row)
        review_title.setObjectName("configurationFieldLabel")
        review_layout.addWidget(review_title, 0, 0)
        self.plan_reviewed = Switch(self.review_row)
        self.plan_reviewed.setAccessibleName("Revisar todo el documento con IA local")
        review_layout.addWidget(self.plan_reviewed, 0, 1, Qt.AlignmentFlag.AlignRight)
        review_help = QLabel(
            "Más lento. El modo normal ya detecta zonas dudosas y te permite revisar solo esas.",
            self.review_row,
        )
        review_help.setObjectName("sectionHelp")
        review_help.setWordWrap(True)
        review_layout.addWidget(review_help, 1, 0, 1, 2)
        review_layout.setColumnStretch(0, 1)
        advanced_layout.addWidget(self.review_row)

        self.translation_method_row = QWidget(self.advanced_panel)
        method_layout = QGridLayout(self.translation_method_row)
        method_layout.setContentsMargins(0, 0, 0, 0)
        method_layout.setHorizontalSpacing(SPACING.md)
        method_label = QLabel("Traductor", self.translation_method_row)
        method_label.setObjectName("configurationFieldLabel")
        method_layout.addWidget(method_label, 0, 0)
        self.translation_method = ChevronComboBox(self.translation_method_row)
        self.translation_method.setAccessibleName("Motor de traducción")
        self.translation_method.addItem(
            "Argos · Rápido y recomendado",
            TranslationMethod.OFFLINE,
        )
        self.translation_method.addItem(
            "IA local · Más contextual",
            TranslationMethod.LOCAL_AI,
        )
        self.translation_method.setMinimumWidth(0)
        self.translation_method.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Fixed,
        )
        method_layout.addWidget(self.translation_method, 0, 1)
        method_layout.setColumnStretch(1, 1)
        advanced_layout.addWidget(self.translation_method_row)

        self.ai_summary = QLabel(self.advanced_panel)
        self.ai_summary.setObjectName("inheritedSetting")
        self.ai_summary.setWordWrap(True)
        advanced_layout.addWidget(self.ai_summary)
        self.manage_models_button = QPushButton("Configurar IA local…", self.advanced_panel)
        self.manage_models_button.clicked.connect(self.models_requested.emit)
        advanced_layout.addWidget(
            self.manage_models_button,
            0,
            Qt.AlignmentFlag.AlignLeft,
        )

        self.glossary_toggle = QPushButton("Glosario", self.advanced_panel)
        self.glossary_toggle.setCheckable(True)
        self.glossary_toggle.toggled.connect(self._toggle_glossary)
        advanced_layout.addWidget(self.glossary_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.glossary_panel = QWidget(self.advanced_panel)
        glossary_layout = QVBoxLayout(self.glossary_panel)
        glossary_layout.setContentsMargins(0, 0, 0, 0)
        self.glossary_table = QTableWidget(0, 3, self.glossary_panel)
        self.glossary_table.setHorizontalHeaderLabels(("Original", "Traducción", ""))
        self.glossary_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.glossary_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.glossary_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.glossary_table.setMinimumHeight(150)
        glossary_layout.addWidget(self.glossary_table)
        self.add_glossary_button = QPushButton("Añadir término", self.glossary_panel)
        self.add_glossary_button.clicked.connect(self._add_glossary_row)
        glossary_layout.addWidget(self.add_glossary_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.glossary_panel.hide()
        advanced_layout.addWidget(self.glossary_panel)

        self.pdf_options = QWidget(self.advanced_panel)
        pdf_layout = QGridLayout(self.pdf_options)
        pdf_layout.setContentsMargins(0, 0, 0, 0)
        pdf_layout.setHorizontalSpacing(SPACING.md)
        pdf_layout.setVerticalSpacing(SPACING.xs)
        range_label = QLabel("Procesar solo algunas páginas", self.pdf_options)
        range_label.setObjectName("configurationFieldLabel")
        pdf_layout.addWidget(range_label, 0, 0)
        self.page_range_enabled = Switch(self.pdf_options)
        self.page_range_enabled.setAccessibleName("Procesar solo un intervalo de páginas")
        pdf_layout.addWidget(self.page_range_enabled, 0, 1, Qt.AlignmentFlag.AlignRight)
        self.page_first = QSpinBox(self.pdf_options)
        self.page_last = QSpinBox(self.pdf_options)
        for spin in (self.page_first, self.page_last):
            spin.setRange(1, 2_147_483_647)
        range_row = QHBoxLayout()
        range_row.addWidget(QLabel("De", self.pdf_options))
        range_row.addWidget(self.page_first)
        range_row.addWidget(QLabel("a", self.pdf_options))
        range_row.addWidget(self.page_last)
        range_row.addStretch(1)
        pdf_layout.addLayout(range_row, 1, 0, 1, 2)
        ocr_label = QLabel("Forzar OCR en todas las páginas", self.pdf_options)
        ocr_label.setObjectName("configurationFieldLabel")
        pdf_layout.addWidget(ocr_label, 2, 0)
        self.force_pdf_ocr = Switch(self.pdf_options)
        self.force_pdf_ocr.setAccessibleName("Forzar OCR en todas las páginas")
        pdf_layout.addWidget(self.force_pdf_ocr, 2, 1, Qt.AlignmentFlag.AlignRight)
        ocr_help = QLabel(
            "Déjalo desactivado salvo que el PDF sea una imagen y la detección automática falle.",
            self.pdf_options,
        )
        ocr_help.setObjectName("sectionHelp")
        ocr_help.setWordWrap(True)
        pdf_layout.addWidget(ocr_help, 3, 0, 1, 2)
        pdf_layout.setColumnStretch(0, 1)
        advanced_layout.addWidget(self.pdf_options)

        self.epub_note = QLabel(
            "Antes de publicar podrás confirmar título, autor, idioma y portada.",
            self.advanced_panel,
        )
        self.epub_note.setObjectName("sectionHelp")
        self.epub_note.setWordWrap(True)
        advanced_layout.addWidget(self.epub_note)

        self.route_summary = QLabel(self.advanced_panel)
        self.route_summary.setObjectName("routeDetail")
        self.route_summary.setWordWrap(True)
        advanced_layout.addWidget(self.route_summary)
        content_layout.addWidget(self.advanced_panel)
        content_layout.addStretch(1)

        self.scroll_area.setWidget(self.content)
        root.addWidget(self.scroll_area, 1)

        self.validation_label = QLabel(self)
        self.validation_label.setObjectName("configurationValidation")
        self.validation_label.setWordWrap(True)
        self.validation_label.hide()
        root.addWidget(self.validation_label)

        self.apply_compatible_row = QWidget(self)
        apply_layout = QHBoxLayout(self.apply_compatible_row)
        apply_layout.setContentsMargins(0, 0, 0, 0)
        self.apply_compatible = Switch(self.apply_compatible_row)
        self.apply_compatible.setAccessibleName("Aplicar a documentos compatibles")
        self.apply_compatible_label = QLabel(
            f"Usar también en {compatible_job_count} documento"
            f"{'s' if compatible_job_count != 1 else ''} del mismo tipo",
            self.apply_compatible_row,
        )
        self.apply_compatible_label.setWordWrap(True)
        apply_layout.addWidget(self.apply_compatible)
        apply_layout.addWidget(self.apply_compatible_label, 1)
        self.apply_compatible_row.setVisible(compatible_job_count > 0)
        root.addWidget(self.apply_compatible_row)

        footer = QHBoxLayout()
        footer.addStretch(1)
        self.cancel_button = QPushButton("Cancelar", self)
        self.save_button = QPushButton("Guardar", self)
        self.save_button.setObjectName("primaryAction")
        self.save_button.setDefault(True)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.save_button)
        root.addLayout(footer)

        self._load(job.configuration)
        self._connect_changes()
        self._loading = False
        self._dirty = False
        self._refresh()
        self._focus_stage(stage)
        self._apply_styles()

    def _connect_changes(self) -> None:
        self.output_markdown.toggled.connect(self._changed)
        self.output_epub.toggled.connect(self._changed)
        self.translation_target.currentIndexChanged.connect(self._changed)
        self.advanced_toggle.toggled.connect(lambda _checked: self._refresh())
        self.plan_reviewed.toggled.connect(self._changed)
        self.translation_method.currentIndexChanged.connect(self._changed)
        self.page_range_enabled.toggled.connect(self._changed)
        self.page_first.valueChanged.connect(self._changed)
        self.page_last.valueChanged.connect(self._changed)
        self.force_pdf_ocr.toggled.connect(self._changed)
        self.glossary_table.itemChanged.connect(self._changed)
        self.save_button.clicked.connect(self._submit)
        self.cancel_button.clicked.connect(self._cancel)

    def _load(self, configuration: JobConfiguration) -> None:
        output = configuration.output.format
        if output not in {DocumentFormat.MARKDOWN, DocumentFormat.EPUB}:
            output = DocumentFormat.MARKDOWN
        if (
            not configuration.output.configured
            and self._job.source.format is DocumentFormat.MARKDOWN
            and output is DocumentFormat.MARKDOWN
        ):
            output = DocumentFormat.EPUB
        self.output_epub.setChecked(output is DocumentFormat.EPUB)
        self.output_markdown.setChecked(output is DocumentFormat.MARKDOWN)
        self._set_combo_data(
            self.translation_target,
            configuration.translation.target_language
            if configuration.translation.enabled
            else None,
        )
        self._set_combo_data(self.translation_method, configuration.translation.method)
        self.plan_reviewed.setChecked(configuration.plan is ProcessingPlan.LOCAL_AI_REVIEWED)
        for source, target in configuration.translation.glossary:
            self._append_glossary_entry(GlossaryEntry(source, target))
        page_range = configuration.page_range
        self.page_range_enabled.setChecked(page_range is not None)
        self.page_first.setValue(page_range.first_page if page_range is not None else 1)
        self.page_last.setValue(page_range.last_page if page_range is not None else 1)
        self.force_pdf_ocr.setChecked(configuration.force_pdf_ocr)
        has_advanced = bool(
            configuration.plan is ProcessingPlan.LOCAL_AI_REVIEWED
            or (
                configuration.translation.enabled
                and configuration.translation.method is TranslationMethod.LOCAL_AI
            )
            or configuration.translation.glossary
            or page_range is not None
            or configuration.force_pdf_ocr
        )
        self.advanced_toggle.setChecked(has_advanced)
        self.glossary_toggle.setChecked(bool(configuration.translation.glossary))

    def configuration(self) -> JobConfiguration:
        output_format = self._selected_output()
        target_data = self.translation_target.currentData()
        translating = isinstance(target_data, str) and bool(target_data)
        glossary = validate_glossary(self._glossary_entries()) if translating else ()
        page_range = None
        if self._job.source.format is DocumentFormat.PDF and self.page_range_enabled.isChecked():
            page_range = PageRangeConfiguration(self.page_first.value(), self.page_last.value())
        previous = self._job.configuration.output
        method = self._selected_translation_method()
        configuration = JobConfiguration(
            output=OutputConfiguration(
                configured=True,
                format=output_format,
                directory=self._default_output_directory,
                include_images=True,
                image_directory=None,
                preserve_styles=output_format is DocumentFormat.EPUB,
                markdown_organization=MarkdownOrganization.SINGLE_FILE,
                markdown_include_metadata=False,
                markdown_include_page_references=False,
                title=(previous.title or self._job.source.path.stem)
                if output_format is DocumentFormat.EPUB
                else None,
                author=previous.author if output_format is DocumentFormat.EPUB else None,
                cover_strategy=previous.cover_strategy,
                cover_path=previous.cover_path,
            ),
            ai=AIProfileConfiguration(
                model=self._default_ai_model,
                context_window=self._default_ai_context,
            ),
            translation=TranslationConfiguration(
                enabled=translating,
                method=method if translating else TranslationMethod.OFFLINE,
                target_language=str(target_data) if translating else None,
                glossary=(
                    tuple((entry.source, entry.target) for entry in glossary) if translating else ()
                ),
            ),
            plan=(
                ProcessingPlan.LOCAL_AI_REVIEWED
                if self.plan_reviewed.isChecked()
                else ProcessingPlan.STANDARD
            ),
            page_range=page_range,
            force_pdf_ocr=(
                self.force_pdf_ocr.isChecked()
                if self._job.source.format is DocumentFormat.PDF
                else False
            ),
        )
        issues = configuration_issues(self._job.source, configuration)
        if issues:
            raise ValueError(issues[0].message)
        return configuration

    def set_models(self, models: tuple[tuple[str, str], ...]) -> None:
        self._models = dict(models)
        self._refresh_ai_summary()

    def set_default_ai_profile(self, model: str | None, context_window: int | None) -> None:
        self._default_ai_model = model
        self._default_ai_context = context_window
        self._refresh_ai_summary()

    def set_ai_status(self, status: OllamaStatus | None) -> None:
        self._ollama_status = status
        self._refresh_ai_summary()

    def request_close(self) -> bool:
        if not self._dirty:
            return True
        answer = QMessageBox.question(
            self,
            "Descartar cambios",
            "¿Cerrar sin guardar los cambios de este documento?",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer is QMessageBox.StandardButton.Discard

    def set_compact_mode(self, compact: bool) -> None:
        if compact == self._compact:
            return
        self._compact = compact
        self.output_choices.setDirection(
            QBoxLayout.Direction.TopToBottom if compact else QBoxLayout.Direction.LeftToRight
        )

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.set_compact_mode(event.size().width() < 520)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self._cancel()
            event.accept()
            return
        super().keyPressEvent(event)

    def _changed(self, *_args: object) -> None:
        if not self._loading:
            self._dirty = True
        self.validation_label.hide()
        self._refresh()

    def _refresh(self) -> None:
        advanced = self.advanced_toggle.isChecked()
        translating = isinstance(self.translation_target.currentData(), str)
        output_format = self._selected_output()
        self.advanced_panel.setVisible(advanced)
        self.translation_method_row.setVisible(translating)
        self.glossary_toggle.setVisible(translating)
        self.glossary_panel.setVisible(translating and self.glossary_toggle.isChecked())
        self.pdf_options.setVisible(self._job.source.format is DocumentFormat.PDF)
        self.page_first.setEnabled(self.page_range_enabled.isChecked())
        self.page_last.setEnabled(self.page_range_enabled.isChecked())
        self.epub_note.setVisible(output_format is DocumentFormat.EPUB)
        self._refresh_advanced_label()
        self._refresh_summary()
        self._refresh_ai_summary()
        self._refresh_route_summary()

    def _refresh_advanced_label(self) -> None:
        active = sum(
            (
                self.plan_reviewed.isChecked(),
                isinstance(self.translation_target.currentData(), str)
                and self._selected_translation_method() is TranslationMethod.LOCAL_AI,
                bool(self._glossary_entries()),
                self._job.source.format is DocumentFormat.PDF
                and self.page_range_enabled.isChecked(),
                self._job.source.format is DocumentFormat.PDF and self.force_pdf_ocr.isChecked(),
            )
        )
        suffix = f" · {active} activada{'s' if active != 1 else ''}" if active else ""
        prefix = "Menos opciones" if self.advanced_toggle.isChecked() else "Más opciones"
        self.advanced_toggle.setText(prefix + suffix)

    def _refresh_summary(self) -> None:
        output = self._selected_output()
        target_data = self.translation_target.currentData()
        translating = isinstance(target_data, str)
        output_label = (
            "Markdown para editar" if output is DocumentFormat.MARKDOWN else "EPUB para leer"
        )
        translation_label = (
            self.translation_target.currentText().removeprefix("Traducir a ")
            if translating
            else "Sin traducción"
        )
        self.summary_title.setText(f"{output_label} · {translation_label}")
        if self.plan_reviewed.isChecked():
            detail = "Revisión completa con IA local antes de publicar."
        else:
            automatic_work = (
                "OCR cuando haga falta y comprobaciones automáticas."
                if self._job.source.format is DocumentFormat.PDF
                else "Conversión y comprobaciones automáticas."
            )
            detail = (
                f"{automatic_work} Si hay señales concretas, podrás revisar después solo los "
                "bloques afectados."
            )
        self.summary_detail.setText(detail)
        if self._default_output_directory is None:
            destination_text = "Se guardará junto al original."
        else:
            destination = self._default_output_directory.name or str(self._default_output_directory)
            destination_text = f"Se guardará en {destination}."
        self.destination_summary.setText(destination_text)
        self.destination_summary.setToolTip(
            str(self._default_output_directory)
            if self._default_output_directory is not None
            else "El resultado se guardará junto al documento original."
        )

    def _refresh_ai_summary(self) -> None:
        ai_needed = self.plan_reviewed.isChecked() or (
            isinstance(self.translation_target.currentData(), str)
            and self._selected_translation_method() is TranslationMethod.LOCAL_AI
        )
        self.ai_summary.setVisible(ai_needed)
        self.manage_models_button.setVisible(ai_needed)
        if not ai_needed:
            return
        model = self._default_ai_model
        model_name = self._models.get(model or "", model or "Sin modelo configurado")
        status = {
            OllamaStatus.READY: "disponible",
            OllamaStatus.MISSING_MODEL: "sin modelo instalado",
            OllamaStatus.NOT_INSTALLED: "Ollama no instalado",
            OllamaStatus.STOPPED: "Ollama detenido",
            OllamaStatus.LOCAL_ONLY_REQUIRED: "requiere configuración local",
            OllamaStatus.UNAVAILABLE: "Ollama no disponible",
            None: "sin comprobar",
        }.get(self._ollama_status, "sin comprobar")
        self.ai_summary.setText(f"IA local: {model_name} · {status}.")

    def _refresh_route_summary(self) -> None:
        target_data = self.translation_target.currentData()
        translating = isinstance(target_data, str)
        configuration = JobConfiguration(
            output=OutputConfiguration(format=self._selected_output()),
            translation=TranslationConfiguration(
                enabled=translating,
                method=self._selected_translation_method(),
                target_language=str(target_data) if translating else None,
            ),
            plan=(
                ProcessingPlan.LOCAL_AI_REVIEWED
                if self.plan_reviewed.isChecked()
                else ProcessingPlan.STANDARD
            ),
        )
        steps = processing_flow_steps(self._job.source.format, configuration)
        self.route_summary.setText(
            "Detalle: " + " → ".join(steps) + ". " + processing_pass_summary(configuration)
        )

    def _submit(self) -> None:
        try:
            self.configuration()
        except (RequestValidationError, ValueError) as exc:
            self.validation_label.setText(str(exc))
            self.validation_label.show()
            self.validation_label.setFocus(Qt.FocusReason.OtherFocusReason)
            return
        self.save_requested.emit()

    def _cancel(self) -> None:
        if not self.request_close():
            return
        if self._embedded:
            self.cancel_requested.emit()
        else:
            self.reject()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._embedded or self.request_close():
            event.accept()
        else:
            event.ignore()

    def _toggle_glossary(self, visible: bool) -> None:
        self.glossary_panel.setVisible(
            visible and isinstance(self.translation_target.currentData(), str)
        )
        self.glossary_toggle.setText("Ocultar glosario" if visible else "Glosario")

    def _append_glossary_entry(self, entry: GlossaryEntry) -> None:
        row = self.glossary_table.rowCount()
        self.glossary_table.insertRow(row)
        self.glossary_table.setItem(row, 0, QTableWidgetItem(entry.source))
        self.glossary_table.setItem(row, 1, QTableWidgetItem(entry.target))
        remove = QPushButton("×", self.glossary_table)
        remove.setAccessibleName(f"Eliminar término {row + 1}")
        remove.clicked.connect(lambda _checked=False, button=remove: self._remove_glossary(button))
        self.glossary_table.setCellWidget(row, 2, remove)

    def _add_glossary_row(self) -> None:
        if self.glossary_table.rowCount() >= MAX_GLOSSARY_ENTRIES:
            return
        self._append_glossary_entry(GlossaryEntry("", ""))
        self._dirty = True

    def _remove_glossary(self, button: QPushButton) -> None:
        for row in range(self.glossary_table.rowCount()):
            if self.glossary_table.cellWidget(row, 2) is button:
                self.glossary_table.removeRow(row)
                self._changed()
                return

    def _glossary_entries(self) -> tuple[GlossaryEntry, ...]:
        entries: list[GlossaryEntry] = []
        for row in range(self.glossary_table.rowCount()):
            source_item = self.glossary_table.item(row, 0)
            target_item = self.glossary_table.item(row, 1)
            source = source_item.text().strip() if source_item is not None else ""
            target = target_item.text().strip() if target_item is not None else ""
            if source or target:
                entries.append(GlossaryEntry(source, target))
        return tuple(entries)

    def _focus_stage(self, stage: StageKind | None) -> None:
        if stage is None:
            return
        if stage in {
            StageKind.TRANSLATE,
            StageKind.REFINE,
            StageKind.STRUCTURE,
            StageKind.PREPARE,
        }:
            self.advanced_toggle.setChecked(True)
        target = {
            StageKind.TRANSLATE: self.translation_method_row,
            StageKind.REFINE: self.review_row,
            StageKind.STRUCTURE: self.review_row,
            StageKind.PREPARE: self.pdf_options,
            StageKind.PUBLISH: self.markdown_card,
        }.get(stage)
        if target is not None:
            self.scroll_area.ensureWidgetVisible(target)

    def _selected_output(self) -> DocumentFormat:
        return DocumentFormat.EPUB if self.output_epub.isChecked() else DocumentFormat.MARKDOWN

    def _selected_translation_method(self) -> TranslationMethod:
        try:
            return TranslationMethod(self.translation_method.currentData())
        except (TypeError, ValueError):
            return TranslationMethod.OFFLINE

    @staticmethod
    def _set_combo_data(combo: ChevronComboBox, value: object) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            f"""
            QDialog#jobConfigurationEditor {{ background: {COLORS.canvas}; }}
            QScrollArea#configurationScroll {{ border: none; background: transparent; }}
            QScrollArea#configurationScroll > QWidget > QWidget {{ background: transparent; }}
            QLabel#configurationHeading {{
                color: {COLORS.text_primary};
                font-size: 16pt;
                font-weight: 700;
            }}
            QLabel#configurationIntro,
            QLabel#choiceDescription,
            QLabel#sectionHelp,
            QLabel#inheritedSetting,
            QLabel#configurationSummaryDetail,
            QLabel#routeDetail {{ color: {COLORS.text_secondary}; }}
            QLabel#configurationFieldLabel,
            QLabel#choiceTitle,
            QLabel#configurationSummaryTitle {{
                color: {COLORS.text_primary};
                font-weight: 650;
            }}
            QFrame#configurationChoice,
            QFrame#configurationAdvanced,
            QFrame#configurationSummary {{
                background: {COLORS.surface};
                border: 1px solid {COLORS.divider};
                border-radius: 9px;
            }}
            QFrame#configurationChoice[selected="true"] {{
                border: 2px solid {COLORS.action_primary};
                background: {COLORS.action_primary_soft};
            }}
            QFrame#configurationSummary {{ background: {COLORS.surface_subtle}; }}
            QPushButton#configurationDisclosure {{
                color: {COLORS.action_primary};
                background: transparent;
                border: none;
                padding: 6px 2px;
                text-align: left;
                font-weight: 650;
            }}
            QPushButton#configurationDisclosure:hover {{
                color: {COLORS.action_primary_hover};
                text-decoration: underline;
            }}
            QLabel#configurationValidation {{
                color: {COLORS.error};
                background: {COLORS.error_soft};
                border-radius: 6px;
                padding: 8px;
            }}
            """
        )
