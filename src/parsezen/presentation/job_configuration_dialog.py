"""Compact, immediately persisted document configuration page."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPoint, Qt, Signal, Slot
from PySide6.QtGui import QKeyEvent, QMouseEvent, QResizeEvent
from PySide6.QtWidgets import (
    QBoxLayout,
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QMenu,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from parsezen.application.configuration_rules import configuration_issues
from parsezen.domain.jobs import (
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
from parsezen.presentation.components import Switch
from parsezen.presentation.design_system import BREAKPOINTS, SPACING
from parsezen.translation_quality import TARGET_LANGUAGE_CODES

_AI_DEFAULT_UNSET = object()


class _ChoiceCard(QFrame):
    """A whole-card format choice retained as the page's one visual selector."""

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


class _OptionRow(QWidget):
    """One keyboard-operable setting rendered as label, current value and chevron."""

    activated = Signal()

    def __init__(self, label: str, value: str, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("configurationOptionRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(label)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(SPACING.sm, SPACING.sm, SPACING.sm, SPACING.sm)
        layout.setSpacing(SPACING.sm)
        self.label = QLabel(label, self)
        self.label.setObjectName("configurationOptionLabel")
        layout.addWidget(self.label)
        layout.addStretch(1)
        self.value = QLabel(value, self)
        self.value.setObjectName("configurationOptionValue")
        self.value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.value)
        self.chevron = QLabel("›", self)
        self.chevron.setObjectName("configurationOptionChevron")
        self.chevron.setAccessibleName("Abrir")
        layout.addWidget(self.chevron)
        self._sync_accessible_description()

    def set_value(self, value: str) -> None:
        self.value.setText(value)
        self._sync_accessible_description()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() is Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.setFocus(Qt.FocusReason.MouseFocusReason)
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

    def _sync_accessible_description(self) -> None:
        self.setAccessibleDescription(f"Valor actual: {self.value.text()}")


class _PageRangeDialog(QDialog):
    """Ask for an inclusive PDF interval away from the flat settings page."""

    def __init__(
        self,
        page_range: PageRangeConfiguration | None,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Intervalo de páginas")
        self.setModal(True)
        self.setObjectName("configurationRangeDialog")
        self.setMinimumWidth(340)

        root = QVBoxLayout(self)
        root.setContentsMargins(SPACING.lg, SPACING.lg, SPACING.lg, SPACING.lg)
        root.setSpacing(SPACING.md)
        prompt = QLabel("Indica la primera y la última página.", self)
        root.addWidget(prompt)

        range_layout = QGridLayout()
        range_layout.setHorizontalSpacing(SPACING.md)
        self.first_page = QSpinBox(self)
        self.last_page = QSpinBox(self)
        for spin in (self.first_page, self.last_page):
            spin.setRange(1, 2_147_483_647)
        first = page_range.first_page if page_range is not None else 1
        last = page_range.last_page if page_range is not None else first
        self.first_page.setValue(first)
        self.last_page.setMinimum(first)
        self.last_page.setValue(last)
        self.first_page.valueChanged.connect(self.last_page.setMinimum)
        range_layout.addWidget(QLabel("Primera", self), 0, 0)
        range_layout.addWidget(QLabel("Última", self), 0, 1)
        range_layout.addWidget(self.first_page, 1, 0)
        range_layout.addWidget(self.last_page, 1, 1)
        root.addLayout(range_layout)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok,
            parent=self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Aplicar")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def page_range(self) -> PageRangeConfiguration:
        return PageRangeConfiguration(self.first_page.value(), self.last_page.value())


class _GlossaryEditorDialog(QDialog):
    """Edit optional translation terms without expanding the settings page."""

    def __init__(
        self,
        entries: tuple[GlossaryEntry, ...],
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Glosario de traducción")
        self.setModal(True)
        self.resize(620, 420)

        root = QVBoxLayout(self)
        root.setContentsMargins(SPACING.lg, SPACING.lg, SPACING.lg, SPACING.lg)
        root.setSpacing(SPACING.md)

        help_label = QLabel(
            "Añade solo términos cuya traducción quieras mantener constante.",
            self,
        )
        help_label.setObjectName("sectionHelp")
        help_label.setWordWrap(True)
        root.addWidget(help_label)

        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(("Original", "Traducción", ""))
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        root.addWidget(self.table, 1)

        self.validation_label = QLabel(self)
        self.validation_label.setObjectName("configurationValidation")
        self.validation_label.setWordWrap(True)
        self.validation_label.hide()
        root.addWidget(self.validation_label)

        footer = QHBoxLayout()
        self.add_button = QPushButton("Añadir término", self)
        self.add_button.clicked.connect(self._add_empty_row)
        footer.addWidget(self.add_button)
        footer.addStretch(1)
        cancel_button = QPushButton("Cancelar", self)
        save_button = QPushButton("Guardar glosario", self)
        save_button.setObjectName("primaryAction")
        cancel_button.clicked.connect(self.reject)
        save_button.clicked.connect(self._submit)
        footer.addWidget(cancel_button)
        footer.addWidget(save_button)
        root.addLayout(footer)

        for entry in entries:
            self._append_entry(entry)

    def entries(self) -> tuple[GlossaryEntry, ...]:
        entries: list[GlossaryEntry] = []
        for row in range(self.table.rowCount()):
            source_item = self.table.item(row, 0)
            target_item = self.table.item(row, 1)
            source = source_item.text().strip() if source_item is not None else ""
            target = target_item.text().strip() if target_item is not None else ""
            if source or target:
                entries.append(GlossaryEntry(source, target))
        return tuple(entries)

    def _append_entry(self, entry: GlossaryEntry) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(entry.source))
        self.table.setItem(row, 1, QTableWidgetItem(entry.target))
        remove = QPushButton("×", self.table)
        remove.setAccessibleName(f"Eliminar término {row + 1}")
        remove.clicked.connect(lambda _checked=False, button=remove: self._remove_row(button))
        self.table.setCellWidget(row, 2, remove)

    def _add_empty_row(self) -> None:
        if self.table.rowCount() < MAX_GLOSSARY_ENTRIES:
            self._append_entry(GlossaryEntry("", ""))

    def _remove_row(self, button: QPushButton) -> None:
        for row in range(self.table.rowCount()):
            if self.table.cellWidget(row, 2) is button:
                self.table.removeRow(row)
                return

    def _submit(self) -> None:
        try:
            validate_glossary(self.entries())
        except RequestValidationError as exc:
            self.validation_label.setText(str(exc))
            self.validation_label.show()
            return
        self.accept()


class JobConfigurationDialog(QDialog):
    """Present a compact conversion configuration that is saved immediately."""

    configuration_changed = Signal()
    component_setup_requested = Signal()
    apply_compatible_requested = Signal()

    def __init__(
        self,
        job: DocumentJob,
        *,
        models: tuple[tuple[str, str], ...] = (),
        stage: StageKind | None = None,
        embedded: bool = False,
        compatible_count: int = 0,
        default_output_directory: Path | None = None,
        default_ai_model: str | None | object = _AI_DEFAULT_UNSET,
        default_ai_context: int | None | object = _AI_DEFAULT_UNSET,
        ollama_status: OllamaStatus | None = None,
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
        self._glossary_values: list[GlossaryEntry] = []
        self._load(job.configuration)

        self.setWindowTitle(f"Configurar · {job.source.path.name}")
        self.setObjectName("jobConfigurationEditor")
        self.setModal(not embedded)
        if embedded:
            self.setWindowFlags(Qt.WindowType.Widget)
        self.resize(720, 560)
        self.setMinimumWidth(0)

        root = QVBoxLayout(self)
        self._root_layout = root
        root.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        root.setContentsMargins(SPACING.xl, SPACING.lg, SPACING.xl, SPACING.lg)
        root.setSpacing(0)

        self.content = QWidget(self)
        self.content.setObjectName("configurationFlatList")
        self.content.setMaximumWidth(760)
        self.content.setMinimumWidth(0)
        self.content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.options_layout = QVBoxLayout(self.content)
        self.options_layout.setContentsMargins(0, 0, 0, 0)
        self.options_layout.setSpacing(SPACING.xs)

        self.output_group = QButtonGroup(self)
        self.output_group.setExclusive(True)
        self.markdown_card = _ChoiceCard(
            "Markdown",
            "Texto editable.",
            parent=self.content,
        )
        self.epub_card = _ChoiceCard(
            "EPUB",
            "Libro electrónico.",
            parent=self.content,
        )
        self.output_markdown = self.markdown_card.radio
        self.output_epub = self.epub_card.radio
        self.output_group.addButton(self.output_markdown)
        self.output_group.addButton(self.output_epub)
        self.output_choices = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.output_choices.setContentsMargins(0, 0, 0, 0)
        self.output_choices.setSpacing(SPACING.sm)
        self.output_choices.addWidget(self.markdown_card, 1)
        self.output_choices.addWidget(self.epub_card, 1)

        self.translate_row = _OptionRow("Traducir", "", parent=self.content)
        self.translator_row = _OptionRow("Traductor", "", parent=self.content)
        self.glossary_row = _OptionRow("Glosario", "", parent=self.content)
        self.review_row = QWidget(self.content)
        self.review_row.setObjectName("configurationSwitchRow")
        review_layout = QHBoxLayout(self.review_row)
        review_layout.setContentsMargins(SPACING.sm, SPACING.sm, SPACING.sm, SPACING.sm)
        review_layout.setSpacing(SPACING.sm)
        self.review_label = QLabel("Revisión adicional con IA", self.review_row)
        self.review_label.setObjectName("configurationOptionLabel")
        review_layout.addWidget(self.review_label)
        review_layout.addStretch(1)
        self.plan_reviewed = Switch(self.review_row)
        self.plan_reviewed.setAccessibleName("Revisión adicional con IA")
        review_layout.addWidget(self.plan_reviewed)
        self.pages_row = _OptionRow("Páginas", "", parent=self.content)
        self.ocr_row = _OptionRow("OCR", "", parent=self.content)

        self.options_layout.addLayout(self.output_choices)
        self.options_layout.addSpacing(SPACING.sm)
        self.options_layout.addWidget(self.translate_row)
        self.options_layout.addWidget(self.translator_row)
        self.options_layout.addWidget(self.glossary_row)
        self.options_layout.addSpacing(SPACING.sm)
        self.options_layout.addWidget(self.review_row)
        self.options_layout.addWidget(self.pages_row)
        self.options_layout.addWidget(self.ocr_row)

        self.validation_label = QLabel(self.content)
        self.validation_label.setObjectName("configurationValidation")
        self.validation_label.setWordWrap(True)
        self.validation_label.hide()
        self.options_layout.addWidget(self.validation_label)

        self.apply_compatible_button = QPushButton(
            f"Aplicar a {compatible_count} compatibles",
            self.content,
        )
        self.apply_compatible_button.setAccessibleName(
            f"Aplicar configuración a {compatible_count} documentos compatibles"
        )
        self.apply_compatible_button.setVisible(compatible_count > 0)
        self.options_layout.addWidget(self.apply_compatible_button)

        root.addWidget(
            self.content,
            0,
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter,
        )
        root.addStretch(1)

        self.output_markdown.toggled.connect(self._set_markdown_output_when_checked)
        self.output_epub.toggled.connect(self._set_epub_output_when_checked)
        self.translate_row.activated.connect(self._open_translation_menu)
        self.translator_row.activated.connect(self._open_translator_menu)
        self.glossary_row.activated.connect(self._open_glossary)
        self.plan_reviewed.toggled.connect(self._set_review_enabled)
        self.pages_row.activated.connect(self._open_pages_menu)
        self.ocr_row.activated.connect(self._open_ocr_menu)
        self.apply_compatible_button.clicked.connect(self._request_compatible_application)
        self._refresh()
        self._focus_stage(stage)

    def configuration(self) -> JobConfiguration:
        translating = self._translation_language is not None
        glossary = validate_glossary(self._glossary_entries()) if translating else ()
        previous = self._job.configuration.output
        configuration = JobConfiguration(
            output=OutputConfiguration(
                configured=True,
                format=self._output_format,
                directory=self._default_output_directory,
                include_images=True,
                image_directory=None,
                preserve_styles=self._output_format is DocumentFormat.EPUB,
                markdown_organization=MarkdownOrganization.SINGLE_FILE,
                markdown_include_metadata=False,
                markdown_include_page_references=False,
                title=(previous.title or self._job.source.path.stem)
                if self._output_format is DocumentFormat.EPUB
                else None,
                author=previous.author if self._output_format is DocumentFormat.EPUB else None,
                cover_strategy=previous.cover_strategy,
                cover_path=previous.cover_path,
            ),
            # Keep the immutable phase-specific profile captured by the job.
            # The editor only changes the legacy/global pair; rebuilding the
            # object from those two values used to silently discard the
            # verified translation/review models and policy snapshot whenever
            # an otherwise unrelated option was edited.
            ai=replace(
                self._job.configuration.ai,
                model=self._default_ai_model,
                context_window=self._default_ai_context,
            ),
            translation=TranslationConfiguration(
                enabled=translating,
                method=(self._translation_method if translating else TranslationMethod.LOCAL_AI),
                target_language=self._translation_language,
                glossary=(
                    tuple((entry.source, entry.target) for entry in glossary) if translating else ()
                ),
            ),
            plan=(
                ProcessingPlan.LOCAL_AI_REVIEWED
                if self._review_enabled
                else ProcessingPlan.STANDARD
            ),
            page_range=(
                self._page_range if self._job.source.format is DocumentFormat.PDF else None
            ),
            force_pdf_ocr=(
                self._force_pdf_ocr if self._job.source.format is DocumentFormat.PDF else False
            ),
        )
        issues = configuration_issues(self._job.source, configuration)
        if issues:
            raise ValueError(issues[0].message)
        return configuration

    def persist_if_valid(self, *, open_models: bool = False) -> bool:
        """Persist the visible choices when valid, leaving incomplete AI intent visible."""

        try:
            self.configuration()
        except (RequestValidationError, ValueError) as exc:
            self.validation_label.setText(str(exc))
            self.validation_label.show()
            if open_models and self._ai_needed() and self._ai_setup_required():
                self.component_setup_requested.emit()
            return False
        self.validation_label.hide()
        self.configuration_changed.emit()
        return True

    def _request_compatible_application(self) -> None:
        if self.persist_if_valid():
            self.apply_compatible_requested.emit()

    def mark_persisted(self, job: DocumentJob) -> None:
        """Keep later immediate updates based on the last committed configuration."""

        self._job = job

    @property
    def review_enabled(self) -> bool:
        return self._review_enabled

    def set_models(self, models: tuple[tuple[str, str], ...]) -> None:
        self._models = dict(models)

    def set_default_ai_profile(self, model: str | None, context_window: int | None) -> None:
        self._default_ai_model = model
        self._default_ai_context = context_window

    def set_ai_status(self, status: OllamaStatus | None) -> None:
        self._ollama_status = status

    def set_compact_mode(self, compact: bool) -> None:
        """Stack only the visual format choices when horizontal space is limited."""

        direction = (
            QBoxLayout.Direction.TopToBottom if compact else QBoxLayout.Direction.LeftToRight
        )
        self.output_choices.setDirection(direction)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            event.accept()
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        margins = self._root_layout.contentsMargins()
        available_width = max(0, event.size().width() - margins.left() - margins.right())
        self.content.setFixedWidth(min(760, available_width))
        self.set_compact_mode(event.size().width() <= BREAKPOINTS.compact)
        super().resizeEvent(event)

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
        self._output_format = output
        self._translation_language = (
            configuration.translation.target_language if configuration.translation.enabled else None
        )
        self._translation_method = configuration.translation.method
        self._review_enabled = configuration.plan is ProcessingPlan.LOCAL_AI_REVIEWED
        self._page_range = configuration.page_range
        self._force_pdf_ocr = configuration.force_pdf_ocr
        self._glossary_values = [
            GlossaryEntry(source, target) for source, target in configuration.translation.glossary
        ]

    def _refresh(self) -> None:
        self._set_checked_without_signal(
            self.output_markdown,
            self._output_format is DocumentFormat.MARKDOWN,
        )
        self._set_checked_without_signal(
            self.output_epub,
            self._output_format is DocumentFormat.EPUB,
        )
        self.markdown_card._refresh_state(  # noqa: SLF001
            self._output_format is DocumentFormat.MARKDOWN
        )
        self.epub_card._refresh_state(  # noqa: SLF001
            self._output_format is DocumentFormat.EPUB
        )
        language_name = self._language_name(self._translation_language)
        self.translate_row.set_value(language_name or "No traducir")
        translating = self._translation_language is not None
        self.translator_row.setVisible(translating)
        self.glossary_row.setVisible(translating)
        self.translator_row.set_value(
            "IA local · contextual"
            if self._translation_method is TranslationMethod.LOCAL_AI
            else "Argos · ligero"
        )
        glossary_count = len(self._glossary_values)
        self.glossary_row.set_value(
            "Ninguno"
            if glossary_count == 0
            else f"{glossary_count} término{'s' if glossary_count != 1 else ''}"
        )
        if translating:
            review_description = (
                "La IA compara de nuevo bloques de la traducción con el original. "
                "Cualquier cambio queda como propuesta."
            )
        else:
            review_description = "La IA propone correcciones del contenido para que las confirmes."
        if self._output_format is DocumentFormat.EPUB:
            review_description += " También propone la estructura del EPUB."
        self.plan_reviewed.setAccessibleDescription(review_description)
        self.plan_reviewed.setToolTip(review_description)
        self._set_checked_without_signal(self.plan_reviewed, self._review_enabled)
        is_pdf = self._job.source.format is DocumentFormat.PDF
        self.pages_row.setVisible(is_pdf)
        self.ocr_row.setVisible(is_pdf)
        self.pages_row.set_value(
            "Todas"
            if self._page_range is None
            else f"{self._page_range.first_page}–{self._page_range.last_page}"
        )
        self.ocr_row.set_value("Todas las páginas" if self._force_pdf_ocr else "Automático")

    def _open_translation_menu(self) -> None:
        self._open_menu(
            self.translate_row,
            self._translation_choices(),
            self._translation_language,
            self._set_translation_language,
        )

    @staticmethod
    def _translation_choices() -> tuple[tuple[str, str | None], ...]:
        return (
            ("No traducir", None),
            *((name, code) for name, code in TARGET_LANGUAGE_CODES.items()),
        )

    def _open_translator_menu(self) -> None:
        self._open_menu(
            self.translator_row,
            (
                ("IA local · contextual y más lenta", TranslationMethod.LOCAL_AI),
                ("Argos · ligero y predecible", TranslationMethod.OFFLINE),
            ),
            self._translation_method,
            self._set_translation_method,
        )

    def _open_pages_menu(self) -> None:
        menu = QMenu(self)
        all_pages = menu.addAction("Todas")
        all_pages.setCheckable(True)
        all_pages.setChecked(self._page_range is None)
        interval = menu.addAction("Intervalo…")
        interval.setCheckable(True)
        interval.setChecked(self._page_range is not None)
        all_pages.triggered.connect(lambda: self._set_page_range(None))
        interval.triggered.connect(self._choose_page_interval)
        self._show_menu(menu, self.pages_row)

    def _open_ocr_menu(self) -> None:
        self._open_menu(
            self.ocr_row,
            (("Automático", False), ("Todas las páginas", True)),
            self._force_pdf_ocr,
            self._set_force_pdf_ocr,
        )

    def _open_menu(
        self,
        row: _OptionRow,
        choices: tuple[tuple[str, Any], ...],
        current: Any,
        callback: Any,
    ) -> None:
        menu = QMenu(self)
        for label, value in choices:
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(value == current)
            action.triggered.connect(lambda _checked=False, selected=value: callback(selected))
        self._show_menu(menu, row)

    @staticmethod
    def _show_menu(menu: QMenu, row: _OptionRow) -> None:
        menu.exec(JobConfigurationDialog._menu_anchor(menu, row))

    @staticmethod
    def _menu_anchor(menu: QMenu, row: _OptionRow) -> QPoint:
        """Right-align a choice menu with the value that opened it."""

        menu.ensurePolished()
        menu_width = menu.sizeHint().width()
        return row.mapToGlobal(QPoint(max(0, row.width() - menu_width), row.height()))

    def _set_output_format(self, output: DocumentFormat) -> None:
        self._output_format = output
        self._changed()

    def _set_translation_language(self, language: str | None) -> None:
        self._translation_language = language
        self._changed()

    def _set_translation_method(self, method: TranslationMethod) -> None:
        self._translation_method = method
        self._changed(open_models=method is TranslationMethod.LOCAL_AI)

    def _set_review_enabled(self, enabled: bool) -> None:
        self._review_enabled = enabled
        self._changed(open_models=enabled)

    def _set_page_range(self, page_range: PageRangeConfiguration | None) -> None:
        self._page_range = page_range
        self._changed()

    def _choose_page_interval(self) -> None:
        dialog = _PageRangeDialog(self._page_range, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._set_page_range(dialog.page_range())

    def _set_force_pdf_ocr(self, force: bool) -> None:
        self._force_pdf_ocr = force
        self._changed()

    def _open_glossary(self) -> None:
        editor = _GlossaryEditorDialog(self._glossary_entries(), parent=self)
        if editor.exec() != QDialog.DialogCode.Accepted:
            return
        updated = list(editor.entries())
        if updated != self._glossary_values:
            self._glossary_values = updated
            self._changed()

    def _append_glossary_entry(self, entry: GlossaryEntry) -> None:
        self._glossary_values.append(entry)
        self._refresh()

    def _glossary_entries(self) -> tuple[GlossaryEntry, ...]:
        return tuple(self._glossary_values)

    def _changed(self, *, open_models: bool = False) -> None:
        self._refresh()
        self.persist_if_valid(open_models=open_models)

    def _focus_stage(self, stage: StageKind | None) -> None:
        if stage is None:
            return
        target = {
            StageKind.TRANSLATE: self.translate_row,
            StageKind.REFINE: self.plan_reviewed,
            StageKind.STRUCTURE: self.plan_reviewed,
            StageKind.PREPARE: self.pages_row,
            StageKind.PUBLISH: (
                self.output_epub
                if self._output_format is DocumentFormat.EPUB
                else self.output_markdown
            ),
        }.get(stage)
        if target is not None and target.isVisible():
            target.setFocus(Qt.FocusReason.OtherFocusReason)

    @Slot(bool)
    def _set_markdown_output_when_checked(self, checked: bool) -> None:
        if checked:
            self._set_output_format(DocumentFormat.MARKDOWN)

    @Slot(bool)
    def _set_epub_output_when_checked(self, checked: bool) -> None:
        if checked:
            self._set_output_format(DocumentFormat.EPUB)

    @staticmethod
    def _set_checked_without_signal(widget: QRadioButton | Switch, checked: bool) -> None:
        previous = widget.blockSignals(True)
        widget.setChecked(checked)
        widget.blockSignals(previous)

    def _ai_needed(self) -> bool:
        return self._review_enabled or (
            self._translation_language is not None
            and self._translation_method is TranslationMethod.LOCAL_AI
        )

    def _ai_setup_required(self) -> bool:
        if self._default_ai_model is None:
            return True
        return self._ollama_status in {
            OllamaStatus.MISSING_MODEL,
            OllamaStatus.NOT_INSTALLED,
            OllamaStatus.LOCAL_ONLY_REQUIRED,
            OllamaStatus.UNAVAILABLE,
        }

    @staticmethod
    def _language_name(code: str | None) -> str | None:
        if code is None:
            return None
        return next(
            (name for name, candidate in TARGET_LANGUAGE_CODES.items() if candidate == code),
            code,
        )
