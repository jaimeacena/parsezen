"""Reusable rich EPUB editor for every source format."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from lxml import etree, html
from PySide6.QtCore import QSize, Qt, QUrl
from PySide6.QtGui import (
    QColor,
    QFont,
    QImage,
    QResizeEvent,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextListFormat,
)
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTextEdit,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from parsezen.application.book_editor import BookEditor, publish_book
from parsezen.domain.books import BookDocument, BookSection
from parsezen.errors import ParsezenError
from parsezen.infrastructure.artifact_store import ArtifactStore
from parsezen.output import replace_binary_output
from parsezen.presentation.components import HorizontalToolStrip
from parsezen.presentation.design_system import (
    BREAKPOINTS,
    COLORS,
    SPACING,
    chevron_icon,
    editor_icon,
)

_SECTION_ID_ROLE = int(Qt.ItemDataRole.UserRole)
_EDITOR_TOOL_HEIGHT = 34
_EDITOR_TOOL_CONTENT_SIZE = _EDITOR_TOOL_HEIGHT - 2
_EDITOR_ICON_SIZE = 16
_EDITOR_ANCHOR_PREFIX = "parsezen-anchor:"
_EDITOR_ANCHOR_SENTINEL = "\u2060"
_EDITOR_ANCHOR_EXTERNAL_TAGS = frozenset(
    {"a", "area", "br", "hr", "img", "input", "link", "meta", "source", "wbr"}
)


class _BookTextDocument(QTextDocument):
    """Resolve encrypted local book images without rewriting their XHTML URLs."""

    def __init__(
        self,
        book: BookDocument,
        artifacts: ArtifactStore,
        *,
        job_id: str,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self._artifacts = artifacts
        self._job_id = job_id
        self._resources = {
            resource.href.casefold(): resource.payload_artifact_id
            for resource in book.resources
            if resource.media_type.startswith("image/")
        }

    def loadResource(self, resource_type: int, name: QUrl | str) -> Any:  # noqa: N802
        url = name if isinstance(name, QUrl) else QUrl(name)
        if resource_type == int(QTextDocument.ResourceType.ImageResource):
            href = url.path().replace("\\", "/").lstrip("/")
            candidates = {href.casefold(), href.removeprefix("../").casefold()}
            for prefix in ("../images/", "EPUB/images/", "images/"):
                if href.startswith(prefix):
                    candidates.add(href[len(prefix) :].casefold())
            artifact_id = next(
                (
                    self._resources[candidate]
                    for candidate in candidates
                    if candidate in self._resources
                ),
                None,
            )
            if artifact_id is not None:
                image = QImage.fromData(self._artifacts.read(self._job_id, artifact_id))
                if not image.isNull():
                    return image
        return super().loadResource(resource_type, name)


class BookEditorDialog(QDialog):
    """Edit structure and basic formatting, then atomically rebuild one EPUB."""

    MIN_ZOOM = 70
    MAX_ZOOM = 160
    ZOOM_STEP = 10

    def __init__(
        self,
        book: BookDocument,
        artifacts: ArtifactStore,
        *,
        job_id: str,
        destination: Path | None,
        publish_on_accept: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._book = book
        self._artifacts = artifacts
        self._job_id = job_id
        self._destination = destination
        self._publish_on_accept = publish_on_accept
        self._loading = False
        self._current_section_id: str | None = None
        self._saved_for_later = False
        self._zoom_percent = 100
        self.setWindowTitle("Editar libro EPUB · Parsezen")
        self.resize(1320, 820)

        layout = QVBoxLayout(self)
        self.root_layout = layout
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        header = QHBoxLayout()
        title = QLabel("Estructura y contenido del libro", self)
        title.setObjectName("dialogTitle")
        title.setWordWrap(True)
        header.addWidget(title)
        header.addStretch(1)
        layout.addLayout(header)

        self.metadata_button = QPushButton("Metadatos", self)
        self.metadata_button.setObjectName("metadataToggle")
        self.metadata_button.setCheckable(True)
        self.metadata_button.setIcon(chevron_icon(expanded=False))
        self.metadata_button.setIconSize(QSize(16, 16))
        self.metadata_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.metadata_button.setAccessibleName("Mostrar metadatos y portada")
        layout.addWidget(self.metadata_button, 0, Qt.AlignmentFlag.AlignLeft)

        self.metadata_panel = QFrame(self)
        self.metadata_panel.setObjectName("bookMetadataPanel")
        metadata = QFormLayout(self.metadata_panel)
        self.metadata_layout = metadata
        metadata.setContentsMargins(12, 10, 12, 10)
        metadata.setHorizontalSpacing(12)
        metadata.setVerticalSpacing(6)
        metadata.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        metadata.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        metadata.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.title_input = QLineEdit(book.metadata.title, self)
        self.title_input.setAccessibleName("Título del libro")
        self.title_input.setProperty("compactEditorControl", True)
        metadata.addRow("Título", self.title_input)
        self.author_input = QLineEdit(book.metadata.author or "", self)
        self.author_input.setAccessibleName("Autor del libro")
        self.author_input.setProperty("compactEditorControl", True)
        metadata.addRow("Autor", self.author_input)
        self.language_input = QLineEdit(book.metadata.language, self)
        self.language_input.setAccessibleName("Código de idioma del libro")
        self.language_input.setProperty("compactEditorControl", True)
        self.language_input.setMaximumWidth(180)
        metadata.addRow("Idioma", self.language_input)
        self.cover_selector = QComboBox(self.metadata_panel)
        self.cover_selector.setAccessibleName("Portada del libro")
        self.cover_selector.setProperty("compactEditorControl", True)
        self._populate_cover_selector()
        self.cover_selector.activated.connect(self._cover_choice)
        metadata.addRow("Portada", self.cover_selector)
        layout.addWidget(self.metadata_panel)
        self.metadata_panel.hide()
        self.metadata_button.toggled.connect(self._toggle_metadata)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setChildrenCollapsible(False)
        structure_pane = QFrame(self.splitter)
        structure_pane.setMinimumWidth(0)
        structure_pane.setObjectName("bookStructurePane")
        structure_layout = QVBoxLayout(structure_pane)
        structure_layout.setContentsMargins(0, 0, 4, 0)
        structure_layout.setSpacing(6)
        self.structure_toolbar = QWidget(structure_pane)
        self.structure_toolbar.setAccessibleName("Herramientas de capítulos y secciones")
        structure_actions = QHBoxLayout(self.structure_toolbar)
        structure_actions.setContentsMargins(0, 0, 0, 0)
        structure_actions.setSpacing(3)
        self._add_icon_tool(
            structure_actions,
            "split",
            self._split,
            "Separar desde aquí",
        )
        self._add_icon_tool(
            structure_actions,
            "merge",
            self._merge,
            "Unir con la división anterior",
        )
        self._add_toolbar_separator(structure_actions)
        self._add_icon_tool(structure_actions, "move_up", self._move_up, "Mover arriba")
        self._add_icon_tool(structure_actions, "move_down", self._move_down, "Mover abajo")
        self._add_icon_tool(structure_actions, "outdent", self._outdent, "Elevar nivel")
        self._add_icon_tool(structure_actions, "indent", self._indent, "Anidar")
        self._add_icon_tool(structure_actions, "rename", self._rename, "Renombrar")
        structure_actions.addStretch(1)
        self.structure_tool_strip = HorizontalToolStrip(
            self.structure_toolbar,
            structure_pane,
        )
        self.structure_tool_strip.setMinimumWidth(0)
        structure_layout.addWidget(self.structure_tool_strip)
        self.tree = QTreeWidget(structure_pane)
        self.tree.setMinimumWidth(0)
        self.tree.setHeaderHidden(True)
        self.tree.setAccessibleName("Capítulos y secciones")
        self.tree.currentItemChanged.connect(self._section_changed)
        self.tree.itemDoubleClicked.connect(lambda _item, _column: self._rename())
        structure_layout.addWidget(self.tree, 1)

        content_pane = QFrame(self.splitter)
        content_pane.setMinimumWidth(0)
        content_pane.setObjectName("bookContentPane")
        content_layout = QVBoxLayout(content_pane)
        content_layout.setContentsMargins(4, 0, 0, 0)
        content_layout.setSpacing(6)
        self.content_toolbar = QWidget(content_pane)
        self.content_toolbar.setAccessibleName("Herramientas de edición del contenido")
        content_tools = QHBoxLayout(self.content_toolbar)
        content_tools.setContentsMargins(0, 0, 0, 0)
        content_tools.setSpacing(3)
        self.undo_button = self._add_icon_tool(
            content_tools,
            "undo",
            None,
            "Deshacer",
        )
        self.redo_button = self._add_icon_tool(
            content_tools,
            "redo",
            None,
            "Rehacer",
        )
        self._add_toolbar_separator(content_tools)
        self._add_format_button(content_tools, "B", self._bold, "Negrita", bold=True)
        self._add_icon_tool(content_tools, "italic", self._italic, "Cursiva")
        self._add_format_button(content_tools, "U", self._underline, "Subrayado", underline=True)
        self.heading = QComboBox(self.content_toolbar)
        self.heading.addItem("Párrafo", 0)
        for level in range(1, 7):
            self.heading.addItem(f"Título {level}", level)
        self.heading.currentIndexChanged.connect(self._heading_changed)
        self.heading.setAccessibleName("Tipo de párrafo")
        self.heading.setProperty("compactEditorControl", True)
        self.heading.setProperty("editorToolbarControl", True)
        self.heading.setMinimumWidth(104)
        self.heading.setMaximumWidth(118)
        content_tools.addWidget(self.heading)
        self._add_icon_tool(
            content_tools,
            "bullet_list",
            self._bullet_list,
            "Lista con viñetas",
        )
        self._add_icon_tool(
            content_tools,
            "numbered_list",
            self._numbered_list,
            "Lista numerada",
        )
        self.alignment = QComboBox(self.content_toolbar)
        self.alignment.setAccessibleName("Alineación del texto")
        self.alignment.addItem(editor_icon("align_left"), "Izquierda", Qt.AlignmentFlag.AlignLeft)
        self.alignment.addItem(editor_icon("align_center"), "Centro", Qt.AlignmentFlag.AlignCenter)
        self.alignment.addItem(editor_icon("align_right"), "Derecha", Qt.AlignmentFlag.AlignRight)
        self.alignment.addItem(
            editor_icon("align_justify"),
            "Justificar",
            Qt.AlignmentFlag.AlignJustify,
        )
        self.alignment.setIconSize(QSize(_EDITOR_ICON_SIZE, _EDITOR_ICON_SIZE))
        self.alignment.setProperty("compactEditorControl", True)
        self.alignment.setProperty("editorToolbarControl", True)
        self.alignment.setMinimumWidth(104)
        self.alignment.setMaximumWidth(118)
        self.alignment.currentIndexChanged.connect(self._alignment_changed)
        content_tools.addWidget(self.alignment)
        self._add_icon_tool(content_tools, "link", self._insert_link, "Insertar enlace")
        self._add_icon_tool(
            content_tools,
            "clear_formatting",
            self._clear_formatting,
            "Limpiar formato",
        )
        content_tools.addStretch(1)
        self.zoom_out_button = self._add_icon_tool(
            content_tools,
            "zoom_out",
            self._zoom_out,
            "Reducir texto del editor",
        )
        self.zoom_reset_button = self._add_text_tool(
            content_tools,
            "100 %",
            self._reset_zoom,
        )
        self.zoom_reset_button.setObjectName("zoomValue")
        self.zoom_reset_button.setFixedSize(56, _EDITOR_TOOL_CONTENT_SIZE)
        self.zoom_reset_button.setAccessibleName("Restablecer zoom al 100 %")
        self.zoom_reset_button.setToolTip("Restablecer zoom al 100 %")
        self.zoom_in_button = self._add_icon_tool(
            content_tools,
            "zoom_in",
            self._zoom_in,
            "Ampliar texto del editor",
        )
        self._add_toolbar_separator(content_tools)
        self.previous_button = self._add_icon_tool(
            content_tools,
            "previous",
            lambda: self._navigate(-1),
            "Capítulo anterior",
        )
        self.position_label = QLabel(self.content_toolbar)
        self.position_label.setObjectName("bookPosition")
        self.position_label.setAccessibleName("Posición del capítulo")
        self.position_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.position_label.setFixedSize(54, _EDITOR_TOOL_HEIGHT)
        content_tools.addWidget(self.position_label)
        self.next_button = self._add_icon_tool(
            content_tools,
            "next",
            lambda: self._navigate(1),
            "Capítulo siguiente",
        )
        self.content_tool_strip = HorizontalToolStrip(
            self.content_toolbar,
            content_pane,
        )
        self.content_tool_strip.setMinimumWidth(0)
        content_layout.addWidget(self.content_tool_strip)
        self.editor = QTextEdit(content_pane)
        self.editor.setMinimumWidth(0)
        self._editor_document = _BookTextDocument(
            book,
            artifacts,
            job_id=job_id,
            parent=self.editor,
        )
        self.editor.setDocument(self._editor_document)
        self.editor.setAcceptRichText(True)
        self.editor.setAccessibleName("Contenido editable del capítulo")
        self.editor.cursorPositionChanged.connect(self._cursor_changed)
        # The editor must exist before wiring commands that call its native
        # undo/redo slots.
        self.undo_button.clicked.connect(self.editor.undo)
        self.redo_button.clicked.connect(self.editor.redo)
        content_layout.addWidget(self.editor, 1)
        self.splitter.addWidget(structure_pane)
        self.splitter.addWidget(content_pane)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([300, 980])
        layout.addWidget(self.splitter, 1)

        footer = QGridLayout()
        self.footer_layout = footer
        self.save_later_button = QPushButton("Guardar y continuar después", self)
        self.save_later_button.setAccessibleName("Guardar y continuar después")
        self.save_later_button.clicked.connect(self._save_and_close)
        footer.addWidget(self.save_later_button, 0, 0)
        footer.setColumnStretch(1, 1)
        self.cancel_button = QPushButton("Cancelar", self)
        self.cancel_button.clicked.connect(self._cancel)
        footer.addWidget(self.cancel_button, 0, 2)
        self.publish_button = QPushButton("Generar EPUB definitivo", self)
        self.publish_button.setAccessibleName("Generar EPUB definitivo")
        self.publish_button.setObjectName("primaryAction")
        self.publish_button.clicked.connect(self._publish)
        footer.addWidget(self.publish_button, 0, 3)
        layout.addLayout(footer)
        self._compact = False
        self._apply_styles()
        self._populate()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.set_compact_mode(event.size().width() <= BREAKPOINTS.compact)

    def set_compact_mode(self, compact: bool) -> None:
        if compact == self._compact:
            return
        self._compact = compact
        for widget in (
            self.save_later_button,
            self.cancel_button,
            self.publish_button,
        ):
            self.footer_layout.removeWidget(widget)
        self.metadata_layout.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        if compact:
            self.root_layout.setContentsMargins(
                SPACING.sm,
                SPACING.sm,
                SPACING.sm,
                SPACING.sm,
            )
            self.splitter.setOrientation(Qt.Orientation.Vertical)
            self.splitter.setSizes([260, 380])
            self.footer_layout.addWidget(self.save_later_button, 0, 0, 1, 2)
            self.footer_layout.addWidget(self.cancel_button, 1, 0)
            self.footer_layout.addWidget(self.publish_button, 1, 1)
            self.save_later_button.setText("Guardar borrador")
            self.publish_button.setText("Generar EPUB")
        else:
            self.root_layout.setContentsMargins(14, 12, 14, 12)
            self.splitter.setOrientation(Qt.Orientation.Horizontal)
            self.splitter.setSizes([300, 980])
            self.footer_layout.addWidget(self.save_later_button, 0, 0)
            self.footer_layout.setColumnStretch(1, 1)
            self.footer_layout.addWidget(self.cancel_button, 0, 2)
            self.footer_layout.addWidget(self.publish_button, 0, 3)
            self.save_later_button.setText("Guardar y continuar después")
            self.publish_button.setText("Generar EPUB definitivo")

    def apply_theme(self) -> None:
        self._apply_styles()
        self.metadata_button.setIcon(chevron_icon(expanded=self.metadata_button.isChecked()))
        for button in self.findChildren(QToolButton):
            icon_name = button.property("editorIconName")
            if isinstance(icon_name, str):
                button.setIcon(editor_icon(icon_name))
        for index, icon_name in enumerate(
            ("align_left", "align_center", "align_right", "align_justify")
        ):
            self.alignment.setItemIcon(index, editor_icon(icon_name))
        self.editor.viewport().update()
        self.tree.viewport().update()

    def _toggle_metadata(self, visible: bool) -> None:
        self.metadata_panel.setVisible(visible)
        self.metadata_button.setIcon(chevron_icon(expanded=visible))
        self.metadata_button.setAccessibleName(
            "Ocultar metadatos y portada" if visible else "Mostrar metadatos y portada"
        )

    def _populate_cover_selector(self) -> None:
        self.cover_selector.blockSignals(True)
        self.cover_selector.clear()
        if self._book.cover_resource_id is not None:
            self.cover_selector.addItem("Conservar portada actual", "keep")
            self.cover_selector.addItem("Quitar portada", "remove")
        else:
            self.cover_selector.addItem("Sin portada", "keep")
        self.cover_selector.addItem("Elegir una imagen…", "choose")
        self.cover_selector.blockSignals(False)

    def _cover_choice(self, _index: int) -> None:
        if self.cover_selector.currentData() != "choose":
            return
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "Imagen de portada",
            filter="Imágenes (*.png *.jpg *.jpeg *.webp *.gif *.svg)",
        )
        if not selected:
            self.cover_selector.setCurrentIndex(0)
            return
        try:
            path = Path(selected)
            self._book = self._service().replace_cover(path.name, path.read_bytes())
        except (ParsezenError, ValueError, OSError) as exc:
            QMessageBox.warning(self, "No se pudo cambiar la portada", str(exc))
            self.cover_selector.setCurrentIndex(0)
            return
        self._populate_cover_selector()

    @property
    def book(self) -> BookDocument:
        return self._book

    @property
    def saved_for_later(self) -> bool:
        return self._saved_for_later

    def _service(self) -> BookEditor:
        return BookEditor(self._book, self._artifacts, job_id=self._job_id)

    def _populate(self, selected_id: str | None = None) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        selected: QTreeWidgetItem | None = None

        def add(parent: QTreeWidget | QTreeWidgetItem, sections: tuple[BookSection, ...]) -> None:
            nonlocal selected
            for section in sections:
                item = QTreeWidgetItem([section.title])
                item.setData(0, _SECTION_ID_ROLE, section.id)
                if isinstance(parent, QTreeWidgetItem):
                    parent.addChild(item)
                else:
                    parent.addTopLevelItem(item)
                add(item, section.children)
                if section.id == selected_id:
                    selected = item

        add(self.tree, self._book.sections)
        self.tree.expandAll()
        self.tree.blockSignals(False)
        selected = selected or self.tree.topLevelItem(0)
        if selected is not None:
            self.tree.setCurrentItem(selected)
            self._load_section(str(selected.data(0, _SECTION_ID_ROLE)))

    def _save_current(self) -> bool:
        if self._loading or self._current_section_id is None:
            return True
        try:
            self._book = self._service().update_metadata(
                title=self.title_input.text(),
                author=self.author_input.text(),
                language=self.language_input.text(),
            )
            if self.cover_selector.currentData() == "remove":
                self._book = self._service().remove_cover()
                self._populate_cover_selector()
            body = _body_from_qt_html(self.editor.toHtml())
            self._book = self._service().update_content(self._current_section_id, body)
            return True
        except (ParsezenError, ValueError, OSError) as exc:
            QMessageBox.warning(self, "No se pudo guardar el capítulo", str(exc))
            return False

    def _load_section(self, section_id: str) -> None:
        self._loading = True
        try:
            self._current_section_id = section_id
            self.editor.setHtml(_html_for_qt_editor(self._service().editable_html(section_id)))
            position = self._book.spine.index(section_id) + 1
            position_text = f"{position} de {len(self._book.spine)}"
            self.position_label.setText(position_text)
            self.position_label.setAccessibleDescription(
                f"Capítulo {position_text} en el orden de lectura"
            )
            self.previous_button.setEnabled(position > 1)
            self.next_button.setEnabled(position < len(self._book.spine))
        finally:
            self._loading = False

    def _section_changed(
        self,
        current: QTreeWidgetItem | None,
        previous: QTreeWidgetItem | None,
    ) -> None:
        if current is None:
            return
        if previous is not None and not self._save_current():
            self.tree.blockSignals(True)
            self.tree.setCurrentItem(previous)
            self.tree.blockSignals(False)
            return
        self._load_section(str(current.data(0, _SECTION_ID_ROLE)))

    def _selected_id(self) -> str | None:
        item = self.tree.currentItem()
        return str(item.data(0, _SECTION_ID_ROLE)) if item is not None else None

    def _rename(self) -> None:
        section_id = self._selected_id()
        if section_id is None:
            return
        current = self._book.section(section_id).title
        title, accepted = QInputDialog.getText(self, "Renombrar división", "Título", text=current)
        if accepted:
            try:
                self._book = self._service().rename(section_id, title)
            except ValueError as exc:
                QMessageBox.warning(self, "Título no válido", str(exc))
                return
            self._populate(section_id)

    def _add(self) -> None:
        section_id = self._selected_id()
        if section_id is None or not self._save_current():
            return
        title, accepted = QInputDialog.getText(self, "Crear capítulo", "Título")
        if not accepted:
            return
        try:
            self._book = self._service().add_after(section_id, title)
        except ValueError as exc:
            QMessageBox.warning(self, "No se pudo crear", str(exc))
            return
        new_id = self._book.spine[self._book.spine.index(section_id) + 1]
        self._populate(new_id)

    def _split(self) -> None:
        section_id = self._selected_id()
        if section_id is None:
            return
        cursor = self.editor.textCursor()
        split_position = cursor.block().position()
        document_end = self.editor.document().characterCount() - 1
        if split_position <= 0 or split_position >= document_end:
            QMessageBox.information(
                self,
                "Elige otro punto",
                "Sitúa el cursor al comienzo del párrafo que iniciará el nuevo capítulo.",
            )
            return
        first_cursor = QTextCursor(self.editor.document())
        first_cursor.setPosition(0)
        first_cursor.setPosition(split_position, QTextCursor.MoveMode.KeepAnchor)
        second_cursor = QTextCursor(self.editor.document())
        second_cursor.setPosition(split_position)
        second_cursor.setPosition(document_end, QTextCursor.MoveMode.KeepAnchor)
        first_html = _body_from_qt_html(first_cursor.selection().toHtml())
        second_html = _body_from_qt_html(second_cursor.selection().toHtml())
        title, accepted = QInputDialog.getText(self, "Nuevo capítulo", "Título")
        if not accepted:
            return
        try:
            self._book = self._service().split(
                section_id,
                first_html,
                second_html,
                title,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "No se pudo dividir", str(exc))
            return
        new_id = self._book.spine[self._book.spine.index(section_id) + 1]
        self._populate(new_id)

    def _merge(self) -> None:
        section_id = self._selected_id()
        if section_id is None or not self._save_current():
            return
        if self._book.spine.index(section_id) == 0:
            return
        answer = QMessageBox.question(
            self,
            "Quitar división",
            "El contenido se unirá al capítulo anterior. No se borrará texto.",
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        previous = self._book.spine[self._book.spine.index(section_id) - 1]
        self._book = self._service().merge_with_previous(section_id)
        self._populate(previous)

    def _move_up(self) -> None:
        self._structural_change("move", -1)

    def _move_down(self) -> None:
        self._structural_change("move", 1)

    def _indent(self) -> None:
        self._structural_change("indent")

    def _outdent(self) -> None:
        self._structural_change("outdent")

    def _structural_change(self, action: str, offset: int = 0) -> None:
        section_id = self._selected_id()
        if section_id is None or not self._save_current():
            return
        service = self._service()
        self._book = (
            service.move(section_id, offset)
            if action == "move"
            else service.indent(section_id)
            if action == "indent"
            else service.outdent(section_id)
        )
        self._populate(section_id)

    def _navigate(self, offset: int) -> None:
        section_id = self._selected_id()
        if section_id is None:
            return
        position = self._book.spine.index(section_id) + offset
        if not 0 <= position < len(self._book.spine):
            return
        target = self._find_item(self._book.spine[position])
        if target is not None:
            self.tree.setCurrentItem(target)

    def _find_item(self, section_id: str) -> QTreeWidgetItem | None:
        iterator = self.tree.invisibleRootItem()

        def find(parent: QTreeWidgetItem) -> QTreeWidgetItem | None:
            for index in range(parent.childCount()):
                item = parent.child(index)
                if str(item.data(0, _SECTION_ID_ROLE)) == section_id:
                    return item
                nested = find(item)
                if nested is not None:
                    return nested
            return None

        return find(iterator)

    def _save_and_close(self) -> None:
        if self._save_current():
            self._saved_for_later = True
            self.reject()

    def _cancel(self) -> None:
        self._saved_for_later = False
        self.reject()

    def _publish(self) -> None:
        if not self._save_current():
            return
        try:
            content = publish_book(self._book, self._artifacts, job_id=self._job_id)
            if self._publish_on_accept:
                if self._destination is None:
                    raise ValueError("Falta la ubicación del EPUB definitivo.")
                replace_binary_output(self._destination, content)
        except (ParsezenError, ValueError, OSError) as exc:
            QMessageBox.warning(self, "No se pudo generar el EPUB", str(exc))
            return
        self.accept()

    def _zoom_in(self) -> None:
        self._set_editor_zoom(self._zoom_percent + self.ZOOM_STEP)

    def _zoom_out(self) -> None:
        self._set_editor_zoom(self._zoom_percent - self.ZOOM_STEP)

    def _reset_zoom(self) -> None:
        self._set_editor_zoom(100)

    def _set_editor_zoom(self, target: int) -> None:
        target = max(self.MIN_ZOOM, min(self.MAX_ZOOM, target))
        steps = (target - self._zoom_percent) // self.ZOOM_STEP
        if steps > 0:
            self.editor.zoomIn(steps)
        elif steps < 0:
            self.editor.zoomOut(-steps)
        self._zoom_percent = target
        self.zoom_reset_button.setText(f"{target} %")
        self.zoom_out_button.setEnabled(target > self.MIN_ZOOM)
        self.zoom_in_button.setEnabled(target < self.MAX_ZOOM)

    def _bold(self) -> None:
        weight = (
            QFont.Weight.Normal
            if self.editor.fontWeight() >= QFont.Weight.Bold
            else QFont.Weight.Bold
        )
        self.editor.setFontWeight(weight)

    def _italic(self) -> None:
        self.editor.setFontItalic(not self.editor.fontItalic())

    def _underline(self) -> None:
        self.editor.setFontUnderline(not self.editor.fontUnderline())

    def _bullet_list(self) -> None:
        self._create_list(QTextListFormat.Style.ListDisc)

    def _numbered_list(self) -> None:
        self._create_list(QTextListFormat.Style.ListDecimal)

    def _create_list(self, style: QTextListFormat.Style) -> None:
        cursor = self.editor.textCursor()
        list_format = QTextListFormat()
        list_format.setStyle(style)
        cursor.createList(list_format)
        self.editor.setTextCursor(cursor)

    def _alignment_changed(self, index: int) -> None:
        if self._loading or index < 0:
            return
        alignments = (
            Qt.AlignmentFlag.AlignLeft,
            Qt.AlignmentFlag.AlignCenter,
            Qt.AlignmentFlag.AlignRight,
            Qt.AlignmentFlag.AlignJustify,
        )
        self.editor.setAlignment(alignments[index])

    def _insert_link(self) -> None:
        cursor = self.editor.textCursor()
        if not cursor.hasSelection():
            QMessageBox.information(
                self,
                "Selecciona un texto",
                "Selecciona primero las palabras que quieres convertir en enlace.",
            )
            return
        address, accepted = QInputDialog.getText(
            self,
            "Añadir enlace",
            "Dirección:",
        )
        address = address.strip()
        if not accepted or not address:
            return
        link_format = QTextCharFormat()
        link_format.setAnchor(True)
        link_format.setAnchorHref(address)
        link_format.setFontUnderline(True)
        link_format.setForeground(QColor(COLORS.action_primary_hover))
        cursor.mergeCharFormat(link_format)

    def _clear_formatting(self) -> None:
        cursor = self.editor.textCursor()
        cursor.setCharFormat(QTextCharFormat())
        cursor.setBlockFormat(QTextBlockFormat())
        self.editor.setTextCursor(cursor)
        self.heading.setCurrentIndex(0)
        self.alignment.setCurrentIndex(0)

    def _heading_changed(self) -> None:
        if self._loading:
            return
        level = int(self.heading.currentData() or 0)
        cursor = self.editor.textCursor()
        block_format = cursor.blockFormat()
        block_format.setHeadingLevel(level)
        cursor.setBlockFormat(block_format)
        size = {0: 11, 1: 24, 2: 20, 3: 17, 4: 15, 5: 13, 6: 12}[level]
        character = QTextCharFormat()
        character.setFontPointSize(size)
        character.setFontWeight(QFont.Weight.Bold if level else QFont.Weight.Normal)
        cursor.mergeCharFormat(character)

    def _cursor_changed(self) -> None:
        block_format = self.editor.textCursor().blockFormat()
        level = block_format.headingLevel()
        self.heading.blockSignals(True)
        self.heading.setCurrentIndex(max(0, self.heading.findData(level)))
        self.heading.blockSignals(False)
        alignments = (
            Qt.AlignmentFlag.AlignLeft,
            Qt.AlignmentFlag.AlignCenter,
            Qt.AlignmentFlag.AlignRight,
            Qt.AlignmentFlag.AlignJustify,
        )
        alignment = block_format.alignment()
        alignment_index = next(
            (index for index, candidate in enumerate(alignments) if alignment & candidate),
            0,
        )
        self.alignment.blockSignals(True)
        self.alignment.setCurrentIndex(alignment_index)
        self.alignment.blockSignals(False)

    def _add_icon_tool(
        self,
        layout: QHBoxLayout,
        icon_name: str,
        callback: Callable[[], None] | None,
        tooltip: str,
    ) -> QToolButton:
        button = QToolButton(layout.parentWidget() or self)
        button.setIcon(editor_icon(icon_name))
        button.setIconSize(QSize(_EDITOR_ICON_SIZE, _EDITOR_ICON_SIZE))
        button.setFixedSize(_EDITOR_TOOL_CONTENT_SIZE, _EDITOR_TOOL_CONTENT_SIZE)
        button.setProperty("editorAction", icon_name)
        button.setProperty("editorIconName", icon_name)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        if callback is not None:
            button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def _add_toolbar_separator(self, layout: QHBoxLayout) -> None:
        separator = QFrame(layout.parentWidget() or self)
        separator.setObjectName("editorToolbarSeparator")
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setFixedSize(1, 20)
        layout.addWidget(separator)

    def _add_format_button(
        self,
        layout: QHBoxLayout,
        text: str,
        callback: Callable[[], None],
        tooltip: str,
        **font_options: bool,
    ) -> None:
        button = QToolButton(layout.parentWidget() or self)
        button.setText(text)
        button.setFixedSize(_EDITOR_TOOL_CONTENT_SIZE, _EDITOR_TOOL_CONTENT_SIZE)
        button.setProperty("editorAction", tooltip.casefold())
        font = button.font()
        font.setBold(font_options.get("bold", False))
        font.setItalic(font_options.get("italic", False))
        font.setUnderline(font_options.get("underline", False))
        button.setFont(font)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(callback)
        layout.addWidget(button)

    def _add_text_tool(
        self,
        layout: QHBoxLayout,
        text: str,
        callback: Callable[[], None] | None = None,
    ) -> QToolButton:
        button = QToolButton(layout.parentWidget() or self)
        button.setText(text)
        button.setFixedHeight(_EDITOR_TOOL_CONTENT_SIZE)
        button.setAccessibleName(text)
        button.setToolTip(text)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        if callback is not None:
            button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            f"""
            QFrame#bookStructurePane,
            QFrame#bookContentPane {{
                background-color: transparent;
                border: none;
            }}
            QPushButton#metadataToggle {{
                min-height: 32px;
                max-height: 32px;
                padding: 0 10px;
                color: {COLORS.text_primary};
                background-color: transparent;
                border: 1px solid transparent;
                border-radius: 6px;
            }}
            QPushButton#metadataToggle:hover,
            QPushButton#metadataToggle:checked {{
                border-color: transparent;
                background-color: {COLORS.action_primary_soft};
            }}
            QPushButton#metadataToggle:focus {{
                border-color: transparent;
                background-color: {COLORS.action_primary_soft};
            }}
            QFrame#bookMetadataPanel {{
                background-color: {COLORS.surface_subtle};
                border: none;
                border-radius: 6px;
            }}
            QLineEdit[compactEditorControl="true"],
            QComboBox[compactEditorControl="true"] {{
                min-height: 32px;
                max-height: 32px;
                padding-top: 0;
                padding-bottom: 0;
            }}
            QComboBox[editorToolbarControl="true"] {{
                min-height: {_EDITOR_TOOL_CONTENT_SIZE}px;
                max-height: {_EDITOR_TOOL_CONTENT_SIZE}px;
            }}
            QFrame#bookStructurePane QTreeWidget,
            QFrame#bookContentPane QTextEdit {{
                background-color: {COLORS.surface_raised};
                border: 1px solid {COLORS.divider};
                border-radius: 6px;
            }}
            QToolButton {{
                min-width: {_EDITOR_TOOL_CONTENT_SIZE}px;
                max-width: {_EDITOR_TOOL_CONTENT_SIZE}px;
                min-height: {_EDITOR_TOOL_CONTENT_SIZE}px;
                max-height: {_EDITOR_TOOL_CONTENT_SIZE}px;
                padding: 0;
                color: {COLORS.text_primary};
                background-color: transparent;
                border: 1px solid transparent;
                border-radius: 6px;
            }}
            QToolButton#zoomValue {{
                min-width: 56px;
                max-width: 56px;
                color: {COLORS.text_secondary};
                background-color: transparent;
            }}
            QFrame#editorToolbarSeparator {{
                color: {COLORS.divider};
                background-color: {COLORS.divider};
                border: none;
            }}
            QLabel#bookPosition {{
                color: {COLORS.text_secondary};
            }}
            QToolButton:hover,
            QToolButton:focus {{
                background-color: {COLORS.surface_hover};
            }}
            QToolButton:focus {{
                border-color: transparent;
            }}
            QTextEdit QScrollBar:vertical {{
                width: 10px;
                margin: 2px;
                background: {COLORS.canvas};
                border: none;
            }}
            QTextEdit QScrollBar:horizontal {{
                height: 10px;
                margin: 2px;
                background: {COLORS.canvas};
                border: none;
            }}
            QTextEdit QScrollBar::handle:vertical,
            QTextEdit QScrollBar::handle:horizontal {{
                min-width: 24px;
                min-height: 24px;
                background: {COLORS.border_strong};
                border-radius: 4px;
            }}
            QTextEdit QScrollBar::handle:vertical:hover,
            QTextEdit QScrollBar::handle:horizontal:hover {{
                background: {COLORS.text_muted};
            }}
            QTextEdit QScrollBar::add-line,
            QTextEdit QScrollBar::sub-line {{
                width: 0;
                height: 0;
                border: none;
                background: transparent;
            }}
            QTextEdit QScrollBar::add-page,
            QTextEdit QScrollBar::sub-page {{
                background: transparent;
            }}
            """
        )


def _body_from_qt_html(source: str) -> str:
    document = html.fromstring(source)
    bodies = document.xpath("//body")
    container = bodies[0] if bodies else document
    for element in tuple(container.xpath(f".//a[starts-with(@href, '{_EDITOR_ANCHOR_PREFIX}')]")):
        if element.text_content() != _EDITOR_ANCHOR_SENTINEL:
            continue
        anchor = unquote(element.attrib["href"][len(_EDITOR_ANCHOR_PREFIX) :])
        if not _valid_editor_anchor(anchor):
            raise ValueError("El capítulo contiene un ancla interna no válida.")
        replacement = etree.Element("span", id=anchor)
        replacement.tail = element.tail
        parent = element.getparent()
        if parent is not None:
            parent.replace(element, replacement)
    return (container.text or "") + "".join(
        etree.tostring(child, encoding="unicode", method="xml") for child in container
    )


def _html_for_qt_editor(source: str) -> str:
    container = html.fragment_fromstring(source or "<p></p>", create_parent="div")
    for element in tuple(container.iterdescendants()):
        anchor = element.attrib.pop("id", None)
        if anchor is None:
            continue
        if not _valid_editor_anchor(anchor):
            raise ValueError("El capítulo contiene un ancla interna no válida.")
        marker = etree.Element(
            "a",
            href=f"{_EDITOR_ANCHOR_PREFIX}{quote(anchor, safe='')}",
        )
        marker.text = _EDITOR_ANCHOR_SENTINEL
        tag = etree.QName(element).localname.casefold()
        if tag in _EDITOR_ANCHOR_EXTERNAL_TAGS:
            parent = element.getparent()
            if parent is None:
                raise ValueError("El capítulo contiene un ancla interna no válida.")
            parent.insert(parent.index(element), marker)
        else:
            original_text = element.text
            element.text = None
            element.insert(0, marker)
            marker.tail = original_text
    return (container.text or "") + "".join(
        etree.tostring(child, encoding="unicode", method="xml") for child in container
    )


def _valid_editor_anchor(anchor: str) -> bool:
    return (
        bool(anchor)
        and len(anchor) <= 512
        and not any(character.isspace() or ord(character) < 32 for character in anchor)
    )
