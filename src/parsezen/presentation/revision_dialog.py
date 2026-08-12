"""Presentation dialog for AI-proposed content and structure changes."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast

from PySide6.QtCore import QPointF, QRectF, QRegularExpression, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPaintEvent,
    QPalette,
    QPen,
    QResizeEvent,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLayout,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from parsezen.document_model import ConvertedResource
from parsezen.epub_builder import EpubPlan
from parsezen.epub_structure import EditableEpubStructure
from parsezen.pdf_conversion import PdfQualityReport, PdfReviewIssue
from parsezen.presentation.design_system import BREAKPOINTS, COLORS, SPACING
from parsezen.presentation.markdown_review import LocalMarkdownView, _PdfPagePreview
from parsezen.revision import (
    RevisionChange,
    RevisionDecision,
    RevisionDraft,
    RevisionKind,
    build_revision_draft,
    set_heading_level,
)
from parsezen.translation_quality import TranslationQualityIssue, TranslationQualityReport

_ROLE_IDENTIFIER = int(Qt.ItemDataRole.UserRole)
_ROLE_KIND = _ROLE_IDENTIFIER + 1
_ROLE_BLOCKING = _ROLE_IDENTIFIER + 2
_ROLE_CHAPTER = _ROLE_IDENTIFIER + 3
_ROLE_LINE = _ROLE_IDENTIFIER + 4


class _StructureActionButton(QPushButton):
    """Small DPI-aware action icon for the EPUB division tree."""

    def __init__(
        self,
        action: str,
        label: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._action = action
        self.setObjectName("structureActionButton")
        self.setProperty("structureAction", action)
        self.setFixedSize(28, 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(label)
        self.setToolTip(label)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        group = QPalette.ColorGroup.Active if self.isEnabled() else QPalette.ColorGroup.Disabled
        color = self.palette().color(group, QPalette.ColorRole.ButtonText)
        pen = QPen(color, 1.55)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        center = QPointF(self.width() / 2, self.height() / 2)
        painter.translate(center)
        if self._action in {"up", "down", "left", "right"}:
            horizontal = self._action in {"left", "right"}
            direction = -1 if self._action in {"up", "left"} else 1
            if horizontal:
                painter.drawLine(QPointF(-5 * direction, 0), QPointF(5 * direction, 0))
                painter.drawLine(
                    QPointF(-5 * direction, 0),
                    QPointF(-1 * direction, -4),
                )
                painter.drawLine(
                    QPointF(-5 * direction, 0),
                    QPointF(-1 * direction, 4),
                )
            else:
                painter.drawLine(QPointF(0, -5 * direction), QPointF(0, 5 * direction))
                painter.drawLine(
                    QPointF(0, -5 * direction),
                    QPointF(-4, -1 * direction),
                )
                painter.drawLine(
                    QPointF(0, -5 * direction),
                    QPointF(4, -1 * direction),
                )
        elif self._action == "split":
            painter.drawLine(QPointF(-6, -4), QPointF(6, -4))
            painter.drawLine(QPointF(-6, 4), QPointF(6, 4))
            painter.drawLine(QPointF(-2, -7), QPointF(-2, -1))
            painter.drawLine(QPointF(2, 1), QPointF(2, 7))
        elif self._action == "rename":
            painter.drawLine(QPointF(-5, 5), QPointF(4, -4))
            painter.drawLine(QPointF(2.5, -5.5), QPointF(5.5, -2.5))
            painter.drawLine(QPointF(-6, 6), QPointF(-2.5, 5.2))
        elif self._action == "remove":
            painter.drawLine(QPointF(-6, -5), QPointF(6, -5))
            painter.drawLine(QPointF(-2.5, -7.5), QPointF(2.5, -7.5))
            painter.drawRoundedRect(QRectF(-4.5, -2.5, 9, 10), 1.2, 1.2)
            painter.drawLine(QPointF(-1.5, 0), QPointF(-1.5, 5))
            painter.drawLine(QPointF(1.5, 0), QPointF(1.5, 5))


class EpubStructureEditor(QWidget):
    """Final, non-technical editor for the exact EPUB chapter plan."""

    changed = Signal()

    def __init__(
        self,
        markdown: str,
        title: str,
        resources: tuple[ConvertedResource, ...],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("epubStructureEditor")
        self._title = title
        self._resources = resources
        self._model = EditableEpubStructure(markdown, title)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(9)
        intro = QLabel(
            "Define las divisiones y la jerarquía del libro. Quitar una división nunca "
            "borra texto: une ese contenido con el capítulo anterior.",
            self,
        )
        intro.setObjectName("reviewHelp")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setObjectName("revisionSplitter")
        splitter.setChildrenCollapsible(False)
        tree_pane = QFrame(splitter)
        tree_pane.setObjectName("epubStructurePane")
        tree_layout = QVBoxLayout(tree_pane)
        tree_layout.setContentsMargins(10, 10, 10, 10)
        tree_layout.setSpacing(7)
        tree_title = QLabel("Divisiones y jerarquía", tree_pane)
        tree_title.setObjectName("reviewPaneTitle")
        tree_layout.addWidget(tree_title)
        self.tree = QTreeWidget(tree_pane)
        self.tree.setObjectName("revisionEpubOutline")
        self.tree.setHeaderHidden(True)
        self.tree.setAccessibleName("Estructura editable del EPUB")
        self.tree.currentItemChanged.connect(self._selection_changed)
        self.tree.itemDoubleClicked.connect(lambda _item, _column: self._rename())
        tree_layout.addWidget(self.tree, 1)

        # Compatibility handles remain available for automation, while the
        # visible controls live next to the division they affect.
        self.rename_button = QPushButton("Renombrar", tree_pane)
        self.add_button = QPushButton("Añadir capítulo", tree_pane)
        self.split_button = QPushButton("Crear división", tree_pane)
        self.merge_button = QPushButton("Quitar división", tree_pane)
        self.move_up_button = QPushButton("Subir capítulo", tree_pane)
        self.move_down_button = QPushButton("Bajar capítulo", tree_pane)
        self.promote_button = QPushButton("Elevar apartado", tree_pane)
        self.demote_button = QPushButton("Anidar apartado", tree_pane)
        for compatibility_button in (
            self.rename_button,
            self.add_button,
            self.split_button,
            self.merge_button,
            self.move_up_button,
            self.move_down_button,
            self.promote_button,
            self.demote_button,
        ):
            compatibility_button.setVisible(False)

        preview_pane = QFrame(splitter)
        preview_pane.setObjectName("epubPreviewPane")
        preview_layout = QVBoxLayout(preview_pane)
        preview_layout.setContentsMargins(10, 10, 10, 10)
        preview_layout.setSpacing(7)
        self.preview_title = QLabel("Vista del libro", preview_pane)
        self.preview_title.setObjectName("reviewPaneTitle")
        preview_layout.addWidget(self.preview_title)
        self.preview = LocalMarkdownView(preview_pane)
        self.preview.setObjectName("revisionPreview")
        self.preview.setAccessibleName("Vista del capítulo seleccionado")
        preview_layout.addWidget(self.preview, 1)
        splitter.addWidget(tree_pane)
        splitter.addWidget(preview_pane)
        splitter.setSizes([480, 620])
        layout.addWidget(splitter, 1)

        self.rename_button.clicked.connect(self._rename)
        self.add_button.clicked.connect(self._add_chapter)
        self.split_button.clicked.connect(self._split)
        self.merge_button.clicked.connect(self._merge)
        self.move_up_button.clicked.connect(lambda: self._move(-1))
        self.move_down_button.clicked.connect(lambda: self._move(1))
        self.promote_button.clicked.connect(lambda: self._adjust_level(-1))
        self.demote_button.clicked.connect(lambda: self._adjust_level(1))
        self._populate()

    def reset_markdown(self, markdown: str) -> None:
        self._model = EditableEpubStructure(markdown, self._title)
        self._populate()

    def final_markdown(self) -> str:
        return self._model.markdown()

    def _populate(
        self,
        *,
        chapter_index: int = 0,
        line_number: int | None = None,
    ) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        selected: QTreeWidgetItem | None = None
        for index in range(self._model.chapter_count):
            chapter = QTreeWidgetItem([self._model.chapter_title(index)])
            chapter.setData(0, _ROLE_KIND, "chapter")
            chapter.setData(0, _ROLE_CHAPTER, index)
            font = QFont(chapter.font(0))
            font.setBold(True)
            chapter.setFont(0, font)
            chapter.setForeground(0, QBrush(QColor(0, 0, 0, 0)))
            self.tree.addTopLevelItem(chapter)
            self.tree.setItemWidget(
                chapter,
                0,
                self._tree_row(
                    chapter,
                    self._model.chapter_title(index),
                    (
                        ("rename", "Renombrar división", True, self._rename),
                        ("up", "Mover capítulo hacia arriba", index > 0, lambda: self._move(-1)),
                        (
                            "down",
                            "Mover capítulo hacia abajo",
                            index + 1 < self._model.chapter_count,
                            lambda: self._move(1),
                        ),
                        (
                            "remove",
                            "Quitar esta división sin borrar su contenido",
                            index > 0,
                            self._merge,
                        ),
                    ),
                ),
            )
            if index == chapter_index and line_number is None:
                selected = chapter
            stack: list[tuple[int, QTreeWidgetItem]] = []
            for heading_number, heading in enumerate(self._model.headings(index)):
                if heading_number == 0 and heading.title == chapter.text(0):
                    continue
                item = QTreeWidgetItem([heading.title])
                item.setData(0, _ROLE_KIND, "heading")
                item.setData(0, _ROLE_CHAPTER, index)
                item.setData(0, _ROLE_LINE, heading.line_number)
                item.setToolTip(0, f"Nivel {heading.level}")
                item.setForeground(0, QBrush(QColor(0, 0, 0, 0)))
                while stack and stack[-1][0] >= heading.level:
                    stack.pop()
                (stack[-1][1] if stack else chapter).addChild(item)
                stack.append((heading.level, item))
                self.tree.setItemWidget(
                    item,
                    0,
                    self._tree_row(
                        item,
                        heading.title,
                        (
                            ("rename", "Renombrar apartado", True, self._rename),
                            (
                                "left",
                                "Elevar un nivel",
                                heading.level > 1,
                                lambda: self._adjust_level(-1),
                            ),
                            (
                                "right",
                                "Anidar un nivel",
                                heading.level < 6,
                                lambda: self._adjust_level(1),
                            ),
                            (
                                "split",
                                "Crear una división de capítulo aquí",
                                heading.line_number > 1,
                                self._split,
                            ),
                        ),
                    ),
                )
                if index == chapter_index and line_number == heading.line_number:
                    selected = item
        self.tree.collapseAll()
        self.tree.blockSignals(False)
        if selected is None and self.tree.topLevelItemCount():
            selected = self.tree.topLevelItem(min(chapter_index, self.tree.topLevelItemCount() - 1))
        if selected is not None:
            self.tree.setCurrentItem(selected)
        self._selection_changed(selected, None)

    def _tree_row(
        self,
        item: QTreeWidgetItem,
        title: str,
        actions: tuple[tuple[str, str, bool, Callable[[], None]], ...],
    ) -> QWidget:
        row = QWidget(self.tree)
        row.setObjectName("structureTreeRow")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(4, 1, 3, 1)
        row_layout.setSpacing(2)
        label = QLabel(title, row)
        label.setObjectName("structureTreeTitle")
        label.setToolTip(f"{title}\nDoble clic para renombrar")
        row_layout.addWidget(label, 1)
        for action, accessible_name, enabled, callback in actions:
            button = _StructureActionButton(action, accessible_name, row)
            button.setEnabled(enabled)
            button.setVisible(enabled or action == "rename")
            button.clicked.connect(
                lambda _checked=False, selected=item, function=callback: self._run_action(
                    selected,
                    function,
                )
            )
            row_layout.addWidget(button)
        return row

    def _run_action(
        self,
        item: QTreeWidgetItem,
        callback: Callable[[], None],
    ) -> None:
        self.tree.setCurrentItem(item)
        callback()

    def _selection(self) -> tuple[str, int, int | None] | None:
        item = cast(QTreeWidgetItem | None, self.tree.currentItem())
        if item is None:
            return None
        return (
            str(item.data(0, _ROLE_KIND)),
            int(item.data(0, _ROLE_CHAPTER)),
            int(item.data(0, _ROLE_LINE)) if item.data(0, _ROLE_LINE) is not None else None,
        )

    def _selection_changed(
        self,
        current: QTreeWidgetItem | None,
        _previous: QTreeWidgetItem | None,
    ) -> None:
        selection = self._selection()
        if selection is None:
            return
        kind, chapter, line = selection
        self.preview_title.setText(f"Vista · {self._model.chapter_title(chapter)}")
        self.preview.set_markdown(self._model.chapter_markdown(chapter), resources=self._resources)
        if line is not None:
            heading = next(
                (item for item in self._model.headings(chapter) if item.line_number == line),
                None,
            )
            if heading is not None:
                cursor = self.preview.document().find(heading.title)
                if not cursor.isNull():
                    self.preview.setTextCursor(cursor)
                    self.preview.ensureCursorVisible()
        is_chapter = kind == "chapter"
        self.rename_button.setEnabled(True)
        self.split_button.setEnabled(kind == "heading" and line is not None and line > 1)
        self.merge_button.setEnabled(is_chapter and chapter > 0)
        self.move_up_button.setEnabled(is_chapter and chapter > 0)
        self.move_down_button.setEnabled(is_chapter and chapter + 1 < self._model.chapter_count)
        self.promote_button.setEnabled(kind == "heading")
        self.demote_button.setEnabled(kind == "heading")

    def _rename(self) -> None:
        selection = self._selection()
        if selection is None:
            return
        _kind, chapter, line = selection
        current = self.tree.currentItem().text(0)
        title, accepted = QInputDialog.getText(self, "Renombrar", "Nuevo título:", text=current)
        if not accepted:
            return
        try:
            self._model.rename(chapter, line, title)
        except ValueError as exc:
            QMessageBox.warning(self, "Título no válido", str(exc))
            return
        self._populate(chapter_index=chapter, line_number=line)
        self.changed.emit()

    def _add_chapter(self) -> None:
        selection = self._selection()
        after = selection[1] if selection is not None else self._model.chapter_count - 1
        title, accepted = QInputDialog.getText(
            self, "Añadir capítulo", "Título del nuevo capítulo:"
        )
        if not accepted:
            return
        try:
            added = self._model.add_chapter(after, title)
        except ValueError as exc:
            QMessageBox.warning(self, "Título no válido", str(exc))
            return
        self._populate(chapter_index=added)
        self.changed.emit()

    def _split(self) -> None:
        selection = self._selection()
        if selection is None or selection[2] is None:
            return
        try:
            chapter = self._model.split_at_heading(selection[1], selection[2])
        except ValueError as exc:
            QMessageBox.warning(self, "No se puede dividir", str(exc))
            return
        self._populate(chapter_index=chapter)
        self.changed.emit()

    def _merge(self) -> None:
        selection = self._selection()
        if selection is None:
            return
        try:
            chapter = self._model.merge_with_previous(selection[1])
        except ValueError as exc:
            QMessageBox.warning(self, "No se puede unir", str(exc))
            return
        self._populate(chapter_index=chapter)
        self.changed.emit()

    def _move(self, offset: int) -> None:
        selection = self._selection()
        if selection is None:
            return
        chapter = self._model.move_chapter(selection[1], offset)
        self._populate(chapter_index=chapter)
        self.changed.emit()

    def _adjust_level(self, delta: int) -> None:
        selection = self._selection()
        if selection is None or selection[2] is None:
            return
        try:
            self._model.adjust_heading_level(selection[1], selection[2], delta)
        except ValueError as exc:
            QMessageBox.warning(self, "No se puede cambiar el nivel", str(exc))
            return
        self._populate(chapter_index=selection[1], line_number=selection[2])
        self.changed.emit()


class _PdfMarkerHighlighter(QSyntaxHighlighter):
    """Keep internal page anchors visible but clearly separate from book text."""

    _pattern = QRegularExpression(r"<!--\s*PZDOC PDF PAGE \d+\s*-->")

    def __init__(self, document: QTextDocument) -> None:
        super().__init__(document)
        self._format = QTextCharFormat()
        self._format.setForeground(QColor(COLORS.text_muted))
        self._format.setFontItalic(True)

    def highlightBlock(self, text: str) -> None:  # noqa: N802
        match_iterator = self._pattern.globalMatch(text)
        while match_iterator.hasNext():
            match = match_iterator.next()
            self.setFormat(match.capturedStart(), match.capturedLength(), self._format)


class RevisionReviewDialog(QDialog):
    """Let the user accept, reject and manually refine one local revision."""

    preview_updated = Signal()

    def __init__(
        self,
        draft: RevisionDraft | None,
        document_name: str,
        *,
        plain_text: bool = False,
        initial_text: str | None = None,
        pdf_report: PdfQualityReport | None = None,
        translation_report: TranslationQualityReport | None = None,
        source_path: Path | None = None,
        epub_title: str | None = None,
        resources: tuple[ConvertedResource, ...] = (),
        preserved_translation_chunks: tuple[int, ...] = (),
        allow_pdf_retry: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("revisionReviewDialog")
        self.setWindowTitle(f"Revisar cambios — {document_name}")
        self.setModal(True)
        self.setMinimumSize(320, 520)
        self.resize(1180, 760)
        if draft is None:
            if initial_text is None:
                raise ValueError("A review requires a draft or editable text.")
            draft = build_revision_draft(initial_text, initial_text, kinds=frozenset())
        self._draft = draft
        self._plain_text = plain_text
        self._pdf_report = pdf_report
        self._translation_report = translation_report
        self._source_path = source_path
        self._epub_title = epub_title
        self._resources = resources
        self._preserved_translation_chunks = preserved_translation_chunks
        self._syncing = False
        self._manual_edits_started = False
        self._edited_review_ids: set[str] = set()
        self._change_cursors: dict[str, QTextCursor] = {}
        self._comparison_cursor: QTextCursor | None = None
        self._comparison_identifier: str | None = None
        self._comparison_kind = ""
        self._comparison_original = ""
        self._comparison_proposal = ""
        self._comparison_suggested = ""
        self._comparison_prefix = ""
        self._structure_loaded = False
        self._resolved_review_ids: set[str] = set()
        self._reviewed_ids: set[str] = set()
        self._review_choices: dict[str, str] = {}
        self.retry_page: int | None = None
        self._decisions = {
            change.identifier: change.recommended_decision for change in draft.changes
        }
        self.final_text = (
            initial_text if initial_text is not None else draft.render(self._decisions)
        )

        layout = QVBoxLayout(self)
        self.root_layout = layout
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)

        title = QLabel("Revisa los cambios antes de guardarlos", self)
        title.setObjectName("reviewTitle")
        layout.addWidget(title)

        # Compatibility alias for existing automation; the redundant
        # explanatory line is intentionally absent from the visible layout.
        self.help_label = QLabel("", self)
        self.help_label.setObjectName("reviewHelp")
        self.help_label.setWordWrap(True)
        self.help_label.setVisible(False)

        # Kept as an invisible compatibility API for bulk shortcuts and tests;
        # the guided flow deliberately avoids presenting risky global actions.
        self.accept_all_button = QPushButton("Aceptar todo", self)
        self.reject_all_button = QPushButton("Rechazar todo", self)
        self.accept_all_button.clicked.connect(lambda: self._set_all(True))
        self.reject_all_button.clicked.connect(lambda: self._set_all(False))
        self.accept_all_button.setVisible(False)
        self.reject_all_button.setVisible(False)

        progress_row = QHBoxLayout()
        progress_row.setContentsMargins(0, 0, 0, 0)
        progress_row.setSpacing(10)
        self.review_progress_label = QLabel(self)
        self.review_progress_label.setObjectName("reviewProgressLabel")
        self.review_progress_bar = QProgressBar(self)
        self.review_progress_bar.setObjectName("reviewProgressBar")
        self.review_progress_bar.setTextVisible(False)
        progress_row.addWidget(self.review_progress_label)
        progress_row.addWidget(self.review_progress_bar, 1)
        layout.addLayout(progress_row)

        self.review_category_label = QLabel(self)
        self.review_category_label.setObjectName("reviewCategoryLabel")
        layout.addWidget(self.review_category_label)
        self.change_detail = QLabel("", self)
        self.change_detail.setObjectName("reviewHelp")
        self.change_detail.setWordWrap(True)
        layout.addWidget(self.change_detail)

        # These compatibility controls remain available to older keyboard/UI
        # tests, but structural editing now lives in the dedicated final EPUB step.
        self.heading_label = QLabel("Línea actual", self)
        self.heading_combo = QComboBox(self)
        self.heading_combo.setObjectName("revisionHeadingCombo")
        self.heading_combo.addItem("Texto normal", None)
        for level in range(1, 7):
            self.heading_combo.addItem(f"Título {level}", level)
        self.apply_heading_button = QPushButton("Aplicar", self)
        self.apply_heading_button.clicked.connect(self._apply_heading_level)
        for widget in (
            self.heading_label,
            self.heading_combo,
            self.apply_heading_button,
        ):
            widget.setVisible(False)

        self.changes_list = QListWidget(self)
        self.changes_list.setObjectName("revisionChangesList")
        self.changes_list.setAccessibleName("Cambios propuestos por la IA")
        self.changes_list.itemChanged.connect(self._change_decision_updated)
        self.changes_list.setVisible(False)
        self.changes_list.currentRowChanged.connect(self._show_change_detail)

        # The complete document is kept in a hidden canonical editor. The user
        # only edits the current proposal, which prevents accidental changes to
        # unrelated parts of a long book.
        self.editor = QPlainTextEdit(self)
        self.editor.setObjectName("revisionEditor")
        self.editor.setAccessibleName("Documento completo en revisión")
        self.editor.setPlainText(self.final_text)
        self.editor.textChanged.connect(self._editor_changed)
        self.editor.setVisible(False)
        self._pdf_marker_highlighter = _PdfMarkerHighlighter(self.editor.document())

        self.result_tabs = QTabWidget(self)
        self.result_tabs.setObjectName("revisionResultTabs")
        # Only one contextual view is ever relevant in a guided step. Hiding
        # the tab strip avoids presenting navigation choices that do nothing.
        self.result_tabs.tabBar().setVisible(False)
        self.comparison_tab = QWidget(self.result_tabs)
        comparison_layout = QVBoxLayout(self.comparison_tab)
        comparison_layout.setContentsMargins(0, 8, 0, 0)
        comparison_layout.setSpacing(7)
        self.comparison_splitter = QSplitter(Qt.Orientation.Horizontal, self.comparison_tab)
        self.comparison_splitter.setObjectName("revisionSplitter")
        self.comparison_splitter.setChildrenCollapsible(False)

        self.original_pane = QFrame(self.comparison_splitter)
        self.original_pane.setObjectName("revisionOriginalPane")
        original_layout = QVBoxLayout(self.original_pane)
        original_layout.setContentsMargins(10, 10, 10, 10)
        original_layout.setSpacing(7)
        original_header = QGridLayout()
        self.original_header = original_header
        self.original_title = QLabel("Original", self.original_pane)
        self.original_title.setObjectName("reviewPaneTitle")
        self.original_choice_button = QPushButton("Elegir original", self.original_pane)
        self.original_choice_button.setObjectName("reviewChoiceButton")
        self.original_choice_button.setCheckable(True)
        self.original_choice_button.clicked.connect(
            lambda: self._select_comparison_choice("original")
        )
        self.locate_original_button = QPushButton("Volver al original", self.original_pane)
        self.locate_original_button.setObjectName("reviewLocateButton")
        self.locate_original_button.clicked.connect(self._focus_original)
        original_header.addWidget(self.original_title, 0, 0)
        original_header.setColumnStretch(1, 1)
        original_header.addWidget(self.locate_original_button, 0, 2)
        original_header.addWidget(self.original_choice_button, 0, 3)
        original_layout.addLayout(original_header)
        self.original_stack = QStackedWidget(self.original_pane)
        self.original_editor = QPlainTextEdit(self.original_stack)
        self.original_editor.setObjectName("revisionOriginalText")
        self.original_editor.setReadOnly(True)
        self.original_editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.original_stack.addWidget(self.original_editor)
        original_layout.addWidget(self.original_stack, 1)

        self.proposal_pane = QFrame(self.comparison_splitter)
        self.proposal_pane.setObjectName("revisionProposalPane")
        proposal_layout = QVBoxLayout(self.proposal_pane)
        proposal_layout.setContentsMargins(10, 10, 10, 10)
        proposal_layout.setSpacing(7)
        proposal_header = QGridLayout()
        self.proposal_header = proposal_header
        self.proposal_title = QLabel("Corrección propuesta", self.proposal_pane)
        self.proposal_title.setObjectName("reviewPaneTitle")
        self.locate_proposal_button = QPushButton("Restaurar propuesta", self.proposal_pane)
        self.locate_proposal_button.setObjectName("reviewLocateButton")
        self.locate_proposal_button.clicked.connect(self._restore_current_proposal)
        self.proposal_choice_button = QPushButton("Elegir esta versión", self.proposal_pane)
        self.proposal_choice_button.setObjectName("reviewChoiceButton")
        self.proposal_choice_button.setCheckable(True)
        self.proposal_choice_button.clicked.connect(
            lambda: self._select_comparison_choice("proposal")
        )
        proposal_header.addWidget(self.proposal_title, 0, 0)
        proposal_header.setColumnStretch(1, 1)
        proposal_header.addWidget(self.locate_proposal_button, 0, 2)
        proposal_header.addWidget(self.proposal_choice_button, 0, 3)
        proposal_layout.addLayout(proposal_header)
        self.proposal_editor = QPlainTextEdit(self.proposal_pane)
        self.proposal_editor.setObjectName("revisionProposalText")
        self.proposal_editor.setAccessibleName("Corrección propuesta editable")
        self.proposal_editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.proposal_editor.textChanged.connect(self._comparison_changed)
        proposal_layout.addWidget(self.proposal_editor, 1)
        self.comparison_splitter.addWidget(self.original_pane)
        self.comparison_splitter.addWidget(self.proposal_pane)
        self.comparison_splitter.setSizes([1, 1])
        self.comparison_choice_group = QButtonGroup(self)
        self.comparison_choice_group.setExclusive(True)
        self.comparison_choice_group.addButton(self.original_choice_button)
        self.comparison_choice_group.addButton(self.proposal_choice_button)
        comparison_layout.addWidget(self.comparison_splitter, 1)
        self.editor_tab = self.comparison_tab
        self.editor_tab_index = self.result_tabs.addTab(self.comparison_tab, "Comparar")

        structure_source = self.editor.toPlainText()
        self.structure_editor = EpubStructureEditor(
            structure_source,
            epub_title or document_name,
            resources,
            self.result_tabs,
        )
        self.structure_editor.changed.connect(self._structure_changed)
        self.outline = self.structure_editor.tree
        self.preview = self.structure_editor.preview
        self.outline_tab_index = self.result_tabs.addTab(self.structure_editor, "Estructura EPUB")
        self.result_tabs.setTabVisible(self.outline_tab_index, False)

        # Compatibility aliases: translation and PDF context now use the same
        # focused comparison instead of adding distracting tabs.
        self.translation_context = self.original_editor
        self.translation_context_tab_index = self.editor_tab_index
        self.page_preview: _PdfPagePreview | None = None
        self.page_tab_index: int | None = None
        if (
            source_path is not None
            and source_path.suffix.lower() == ".pdf"
            and pdf_report is not None
        ):
            self.page_preview = _PdfPagePreview(source_path, self.original_stack)
            self.original_stack.addWidget(self.page_preview)
            self.page_tab_index = self.editor_tab_index
        layout.addWidget(self.result_tabs, 1)

        # Hidden compatibility controls keep older automation from breaking.
        # The visible flow uses the two marked panes and one forward action.
        self.keep_original_button = QPushButton("Mantener original", self)
        self.use_proposal_button = QPushButton("Usar propuesta", self)
        self.confirm_review_button = QPushButton("Está bien · continuar", self)
        self.keep_original_button.clicked.connect(lambda: self._decide_current_revision(False))
        self.use_proposal_button.clicked.connect(lambda: self._decide_current_revision(True))
        self.confirm_review_button.clicked.connect(self._confirm_current_issue)
        self.keep_original_button.setVisible(False)
        self.use_proposal_button.setVisible(False)
        self.confirm_review_button.setVisible(False)

        actions = QGridLayout()
        self.actions_layout = actions
        self.retry_ocr_button = QPushButton("Reintentar OCR de esta página", self)
        self.retry_ocr_button.setVisible(pdf_report is not None)
        self.retry_ocr_button.setEnabled(allow_pdf_retry)
        self.retry_ocr_button.setToolTip(
            "Analiza otra vez la página con el OCR local más completo."
            if allow_pdf_retry
            else "Esta página ya se analizó con el OCR local más completo."
        )
        self.retry_ocr_button.clicked.connect(self._retry_current_pdf_page)
        actions.addWidget(self.retry_ocr_button, 0, 0)
        self.previous_button = QPushButton("Anterior", self)
        self.previous_button.clicked.connect(self._show_previous_item)
        actions.addWidget(self.previous_button, 0, 1)
        actions.setColumnStretch(2, 1)
        self.later_button = QPushButton("Revisar más tarde", self)
        self.save_button = QPushButton("Siguiente", self)
        self.save_button.setObjectName("process_button")
        self.later_button.clicked.connect(self.reject)
        self.save_button.clicked.connect(self._continue_review)
        actions.addWidget(self.later_button, 0, 3)
        actions.addWidget(self.save_button, 0, 4)
        layout.addLayout(actions)

        self._compact = False
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(140)
        self._preview_timer.timeout.connect(self._refresh_preview)
        self._populate_changes()
        self._refresh_preview()
        self._refresh_guided_state()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.set_compact_mode(event.size().width() <= BREAKPOINTS.compact)

    def set_compact_mode(self, compact: bool) -> None:
        if compact == self._compact:
            return
        self._compact = compact
        for layout, widgets in (
            (
                self.original_header,
                (
                    self.original_title,
                    self.locate_original_button,
                    self.original_choice_button,
                ),
            ),
            (
                self.proposal_header,
                (
                    self.proposal_title,
                    self.locate_proposal_button,
                    self.proposal_choice_button,
                ),
            ),
            (
                self.actions_layout,
                (
                    self.retry_ocr_button,
                    self.previous_button,
                    self.later_button,
                    self.save_button,
                ),
            ),
        ):
            for widget in widgets:
                layout.removeWidget(widget)
        if compact:
            self.root_layout.setContentsMargins(
                SPACING.md,
                SPACING.md,
                SPACING.md,
                SPACING.md,
            )
            self.comparison_splitter.setOrientation(Qt.Orientation.Vertical)
            self.comparison_splitter.setSizes([280, 280])
            self.original_header.addWidget(self.original_title, 0, 0, 1, 2)
            self.original_header.addWidget(self.locate_original_button, 1, 0)
            self.original_header.addWidget(self.original_choice_button, 1, 1)
            self.proposal_header.addWidget(self.proposal_title, 0, 0, 1, 2)
            self.proposal_header.addWidget(self.locate_proposal_button, 1, 0)
            self.proposal_header.addWidget(self.proposal_choice_button, 1, 1)
            self.actions_layout.addWidget(self.retry_ocr_button, 0, 0, 1, 2)
            self.actions_layout.addWidget(self.previous_button, 1, 0)
            self.actions_layout.addWidget(self.save_button, 1, 1)
            self.actions_layout.addWidget(self.later_button, 2, 0, 1, 2)
        else:
            self.root_layout.setContentsMargins(22, 20, 22, 18)
            self.comparison_splitter.setOrientation(Qt.Orientation.Horizontal)
            self.comparison_splitter.setSizes([1, 1])
            self.original_header.addWidget(self.original_title, 0, 0)
            self.original_header.setColumnStretch(1, 1)
            self.original_header.addWidget(self.locate_original_button, 0, 2)
            self.original_header.addWidget(self.original_choice_button, 0, 3)
            self.proposal_header.addWidget(self.proposal_title, 0, 0)
            self.proposal_header.setColumnStretch(1, 1)
            self.proposal_header.addWidget(self.locate_proposal_button, 0, 2)
            self.proposal_header.addWidget(self.proposal_choice_button, 0, 3)
            self.actions_layout.addWidget(self.retry_ocr_button, 0, 0)
            self.actions_layout.addWidget(self.previous_button, 0, 1)
            self.actions_layout.setColumnStretch(2, 1)
            self.actions_layout.addWidget(self.later_button, 0, 3)
            self.actions_layout.addWidget(self.save_button, 0, 4)

    @property
    def decisions(self) -> dict[str, RevisionDecision]:
        """Return a copy so dialog state cannot be mutated externally."""
        return dict(self._decisions)

    def _populate_changes(self) -> None:
        self._syncing = True
        try:
            self.changes_list.clear()
            for index, change in enumerate(self._draft.changes, start=1):
                kind = "Estructura" if change.kind is RevisionKind.STRUCTURE else "Contenido"
                item = QListWidgetItem(f"{index}. {kind} · {change.summary}")
                item.setData(_ROLE_IDENTIFIER, change.identifier)
                item.setData(_ROLE_KIND, "revision")
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked)
                self._review_choices[change.identifier] = "proposal"
                self.changes_list.addItem(item)
            if self._pdf_report is not None:
                for issue in self._pdf_report.issues:
                    label = f"Página {issue.page_number} · {issue.message}"
                    self._add_review_issue_item(label, "pdf", issue.identifier, issue.blocking)
            if self._preserved_translation_chunks:
                count = len(self._preserved_translation_chunks)
                self._add_review_issue_item(
                    (
                        "Traducción · 1 fragmento conservó el texto original"
                        if count == 1
                        else f"Traducción · {count} fragmentos conservaron el texto original"
                    ),
                    "translation-preserved",
                    "translation-preserved",
                    True,
                )
            if self._translation_report is not None:
                for translation_issue in self._translation_report.issues:
                    location = (
                        "Comprobación general"
                        if translation_issue.segment_number == 0
                        else f"Fragmento {translation_issue.segment_number}"
                    )
                    self._add_review_issue_item(
                        f"Traducción · {location} · {translation_issue.message}",
                        "translation",
                        translation_issue.identifier,
                        False,
                    )
            if self._epub_title is not None:
                self._add_review_issue_item(
                    "Estructura final del EPUB",
                    "epub-structure",
                    "epub-structure",
                    False,
                )
            if self.changes_list.count():
                self.changes_list.setCurrentRow(0)
        finally:
            self._syncing = False

    def _add_review_issue_item(
        self,
        label: str,
        kind: str,
        identifier: str,
        blocking: bool,
    ) -> None:
        item = QListWidgetItem(label)
        item.setData(_ROLE_IDENTIFIER, identifier)
        item.setData(_ROLE_KIND, kind)
        item.setData(_ROLE_BLOCKING, blocking)
        self._review_choices.setdefault(identifier, "proposal")
        if blocking:
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setToolTip("Márcalo cuando lo hayas corregido o decidido conservar.")
        self.changes_list.addItem(item)

    def _set_all(self, accepted: bool) -> None:
        state = Qt.CheckState.Checked if accepted else Qt.CheckState.Unchecked
        self._syncing = True
        try:
            for row in range(self.changes_list.count()):
                item = self.changes_list.item(row)
                if item.data(_ROLE_KIND) != "revision":
                    continue
                item.setCheckState(state)
                identifier = item.data(_ROLE_IDENTIFIER)
                self._reviewed_ids.add(str(identifier))
                self._decisions[str(identifier)] = (
                    RevisionDecision.ACCEPTED if accepted else RevisionDecision.REJECTED
                )
        finally:
            self._syncing = False
        self._render_decisions()
        self._refresh_guided_state()

    def _change_decision_updated(self, item: QListWidgetItem) -> None:
        if self._syncing:
            return
        identifier = str(item.data(_ROLE_IDENTIFIER))
        item_kind = str(item.data(_ROLE_KIND))
        if item_kind != "revision":
            if bool(item.data(_ROLE_BLOCKING)):
                if item.checkState() is Qt.CheckState.Checked:
                    self._resolved_review_ids.add(identifier)
                    self._reviewed_ids.add(identifier)
                else:
                    self._resolved_review_ids.discard(identifier)
                    self._reviewed_ids.discard(identifier)
                self._refresh_guided_state()
            return
        if self._manual_edits_started:
            # Never rebuild the whole editor after the user has started typing:
            # doing so used to erase their corrections. Keep the previous
            # decision and explain that further changes should be made directly.
            previous = self._decisions[identifier]
            self._syncing = True
            item.setCheckState(
                Qt.CheckState.Checked
                if previous is RevisionDecision.ACCEPTED
                else Qt.CheckState.Unchecked
            )
            self._syncing = False
            self.change_detail.setText(
                "Tus cambios manuales están protegidos. Ajusta esta propuesta directamente "
                "en el texto editable."
            )
            self._refresh_guided_state()
            return
        self._decisions[identifier] = (
            RevisionDecision.ACCEPTED
            if item.checkState() is Qt.CheckState.Checked
            else RevisionDecision.REJECTED
        )
        self._render_decisions()

    def _current_item(self) -> QListWidgetItem | None:
        row = self.changes_list.currentRow()
        return self.changes_list.item(row) if 0 <= row < self.changes_list.count() else None

    def _load_text_comparison(
        self,
        identifier: str,
        kind: str,
        original: str,
        proposal: str,
        *,
        original_title: str = "Original",
        proposal_title: str = "Corrección propuesta",
        cursor: QTextCursor | None = None,
    ) -> None:
        self._comparison_identifier = identifier
        self._comparison_kind = kind
        self._comparison_original = original
        self._comparison_proposal = proposal
        self._comparison_suggested = proposal
        self._comparison_prefix = ""
        self._comparison_cursor = cursor
        self.original_title.setText(original_title)
        self.proposal_title.setText(proposal_title)
        self.original_stack.setCurrentWidget(self.original_editor)
        self.locate_original_button.setText("Volver al original")
        self._syncing = True
        try:
            self.original_editor.setPlainText(original)
            self.proposal_editor.setPlainText(proposal)
        finally:
            self._syncing = False
        self._select_comparison_choice(
            self._review_choices.get(identifier, "proposal"),
            focus=False,
        )
        self._focus_proposal(select=False)

    def _load_pdf_comparison(
        self,
        issue: PdfReviewIssue,
        proposal: str,
        cursor: QTextCursor,
    ) -> None:
        self._comparison_identifier = issue.identifier
        self._comparison_kind = "pdf"
        marker = issue.target_marker or ""
        visible_proposal = proposal
        self._comparison_prefix = ""
        if marker and marker in visible_proposal:
            visible_proposal = visible_proposal.replace(marker, "", 1).lstrip("\r\n")
            self._comparison_prefix = f"{marker}\n\n"
        self._comparison_original = visible_proposal
        self._comparison_proposal = visible_proposal
        self._comparison_suggested = visible_proposal
        self._comparison_cursor = cursor
        self.original_title.setText(f"Página {issue.page_number} del PDF")
        self.proposal_title.setText("Texto reconocido · editable")
        self.locate_original_button.setText("Volver a la página")
        if self.page_preview is not None:
            self.page_preview.show_page(issue.page_number)
            self.original_stack.setCurrentWidget(self.page_preview)
        else:
            self.original_stack.setCurrentWidget(self.original_editor)
            self.original_editor.setPlainText(
                "No se puede mostrar la página original, pero puedes corregir el texto reconocido."
            )
        self._syncing = True
        try:
            self.proposal_editor.setPlainText(visible_proposal)
        finally:
            self._syncing = False
        self._select_comparison_choice("proposal", focus=False)
        self._focus_proposal(select=False)

    def _commit_comparison_edit(self) -> None:
        identifier = self._comparison_identifier
        cursor = self._comparison_cursor
        if identifier is None or cursor is None:
            return
        visible_replacement = self.proposal_editor.toPlainText()
        replacement = self._comparison_prefix + visible_replacement
        if cursor.hasSelection():
            start = cursor.selectionStart()
            self._syncing = True
            try:
                cursor.beginEditBlock()
                cursor.insertText(replacement)
                cursor.endEditBlock()
            finally:
                self._syncing = False
            selected = QTextCursor(self.editor.document())
            selected.setPosition(start)
            selected.setPosition(start + len(replacement), QTextCursor.MoveMode.KeepAnchor)
            self._comparison_cursor = selected
            if self._comparison_kind == "revision":
                self._change_cursors[identifier] = selected
        self._comparison_proposal = visible_replacement
        self._schedule_preview()

    def _comparison_changed(self) -> None:
        if self._syncing or self._comparison_identifier is None:
            return
        self._manual_edits_started = True
        self._edited_review_ids.add(self._comparison_identifier)
        self._select_comparison_choice("proposal", focus=False)
        self._refresh_guided_state()

    def _select_comparison_choice(self, choice: str, *, focus: bool = True) -> None:
        if choice not in {"original", "proposal"}:
            return
        identifier = self._comparison_identifier
        if identifier is not None:
            self._review_choices[identifier] = choice
        self.original_choice_button.setChecked(choice == "original")
        self.proposal_choice_button.setChecked(choice == "proposal")
        self.original_pane.setProperty("selected", choice == "original")
        self.proposal_pane.setProperty("selected", choice == "proposal")
        for pane in (self.original_pane, self.proposal_pane):
            pane.style().unpolish(pane)
            pane.style().polish(pane)
        if focus:
            if choice == "original":
                self._focus_original(select=False)
            else:
                self._focus_proposal(select=False)

    def _focus_original(self, *, select: bool = True) -> None:
        if self.original_stack.currentWidget() is self.original_editor:
            cursor = self.original_editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            if select:
                cursor.movePosition(
                    QTextCursor.MoveOperation.End,
                    QTextCursor.MoveMode.KeepAnchor,
                )
            self.original_editor.setTextCursor(cursor)
            self.original_editor.ensureCursorVisible()
            self.original_editor.setFocus()
        elif self.page_preview is not None:
            self.page_preview.setFocus()

    def _focus_proposal(self, *, select: bool = True) -> None:
        cursor = self.proposal_editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        if select:
            cursor.movePosition(
                QTextCursor.MoveOperation.End,
                QTextCursor.MoveMode.KeepAnchor,
            )
        self.proposal_editor.setTextCursor(cursor)
        self.proposal_editor.ensureCursorVisible()
        self.proposal_editor.setFocus()

    def _restore_original_in_proposal(self) -> None:
        self.proposal_editor.setPlainText(self._comparison_original)
        self._focus_proposal(select=False)

    def _restore_ai_proposal(self) -> None:
        item = self._current_item()
        if item is None or item.data(_ROLE_KIND) != "revision":
            return
        identifier = str(item.data(_ROLE_IDENTIFIER))
        change = next(change for change in self._draft.changes if change.identifier == identifier)
        self.proposal_editor.setPlainText(change.proposed_markdown)
        self._focus_proposal(select=False)

    def _restore_current_proposal(self) -> None:
        item = self._current_item()
        if item is not None and item.data(_ROLE_KIND) == "revision":
            self._restore_ai_proposal()
        else:
            self.proposal_editor.setPlainText(self._comparison_suggested)
            self._focus_proposal(select=False)
        self._select_comparison_choice("proposal", focus=False)

    def _decide_current_revision(self, accepted: bool) -> None:
        item = self._current_item()
        if item is None or item.data(_ROLE_KIND) != "revision":
            return
        identifier = str(item.data(_ROLE_IDENTIFIER))
        change = next(change for change in self._draft.changes if change.identifier == identifier)
        decision = RevisionDecision.ACCEPTED if accepted else RevisionDecision.REJECTED
        replacement = change.proposed_markdown if accepted else change.original_markdown
        self._syncing = True
        self.proposal_editor.setPlainText(replacement)
        self._syncing = False
        self._commit_comparison_edit()
        self._syncing = True
        try:
            item.setCheckState(Qt.CheckState.Checked if accepted else Qt.CheckState.Unchecked)
        finally:
            self._syncing = False
        self._decisions[identifier] = decision
        self._edited_review_ids.discard(identifier)
        self._schedule_preview()
        self._reviewed_ids.add(identifier)
        self._advance_after_review()

    def _replace_review_change(
        self,
        change: RevisionChange,
        decision: RevisionDecision,
    ) -> None:
        cursor = self._change_cursors.get(change.identifier)
        if cursor is None or not cursor.hasSelection():
            current = (
                change.proposed_markdown
                if self._decisions[change.identifier] is RevisionDecision.ACCEPTED
                else change.original_markdown
            )
            cursor = self._find_editor_target(current)
        if cursor is None or not cursor.hasSelection():
            return
        replacement = (
            change.proposed_markdown
            if decision is RevisionDecision.ACCEPTED
            else change.original_markdown
        )
        start = cursor.selectionStart()
        cursor.beginEditBlock()
        cursor.insertText(replacement)
        cursor.endEditBlock()
        selected = QTextCursor(self.editor.document())
        selected.setPosition(start)
        selected.setPosition(start + len(replacement), QTextCursor.MoveMode.KeepAnchor)
        self._change_cursors[change.identifier] = selected
        self.editor.setTextCursor(selected)
        self.editor.ensureCursorVisible()

    def _confirm_current_issue(self) -> None:
        item = self._current_item()
        if item is None:
            return
        identifier = str(item.data(_ROLE_IDENTIFIER))
        if str(item.data(_ROLE_KIND)) == "epub-structure":
            self._structure_changed()
        else:
            self._commit_comparison_edit()
        self._reviewed_ids.add(identifier)
        if bool(item.data(_ROLE_BLOCKING)):
            self._resolved_review_ids.add(identifier)
            self._syncing = True
            try:
                item.setCheckState(Qt.CheckState.Checked)
            finally:
                self._syncing = False
        self._advance_after_review()

    def _continue_review(self) -> None:
        """Commit the visible choice and either advance or publish the review."""
        item = self._current_item()
        if item is None:
            self._save()
            return
        identifier = str(item.data(_ROLE_IDENTIFIER))
        kind = str(item.data(_ROLE_KIND))
        if kind == "revision":
            accepted = self._review_choices.get(identifier, "proposal") == "proposal"
            change = next(
                change for change in self._draft.changes if change.identifier == identifier
            )
            if not accepted:
                self._syncing = True
                try:
                    self.proposal_editor.setPlainText(change.original_markdown)
                finally:
                    self._syncing = False
            self._decisions[identifier] = (
                RevisionDecision.ACCEPTED if accepted else RevisionDecision.REJECTED
            )
            self._syncing = True
            try:
                item.setCheckState(Qt.CheckState.Checked if accepted else Qt.CheckState.Unchecked)
            finally:
                self._syncing = False
            self._commit_comparison_edit()
        elif kind == "epub-structure":
            self._structure_changed()
        else:
            self._commit_comparison_edit()
        self._reviewed_ids.add(identifier)
        if bool(item.data(_ROLE_BLOCKING)):
            self._resolved_review_ids.add(identifier)
            self._syncing = True
            try:
                item.setCheckState(Qt.CheckState.Checked)
            finally:
                self._syncing = False
        if self.changes_list.currentRow() + 1 < self.changes_list.count():
            self._advance_after_review()
            return
        self._refresh_guided_state()
        self._save()

    def _advance_after_review(self) -> None:
        self._commit_comparison_edit()
        row = self.changes_list.currentRow()
        if row + 1 < self.changes_list.count():
            self.changes_list.setCurrentRow(row + 1)
        else:
            self._refresh_guided_state()

    def _show_previous_item(self) -> None:
        item = self._current_item()
        if item is not None and item.data(_ROLE_KIND) == "epub-structure":
            self._structure_changed()
            self._structure_loaded = False
        else:
            self._commit_comparison_edit()
        row = self.changes_list.currentRow()
        if row > 0:
            self.changes_list.setCurrentRow(row - 1)

    def _refresh_guided_state(self) -> None:
        total = self.changes_list.count()
        row = self.changes_list.currentRow()
        reviewed = len(self._reviewed_ids)
        self.review_progress_bar.setRange(0, max(total, 1))
        self.review_progress_bar.setValue(reviewed)
        complete = total == 0 or reviewed >= total
        if complete:
            self.review_progress_label.setText(
                "Revisión completa" if total else "Sin avisos pendientes"
            )
        else:
            self.review_progress_label.setText(
                f"Paso {max(row + 1, 1)} de {total} · {reviewed} revisados"
            )
        item = self._current_item()
        kind = str(item.data(_ROLE_KIND)) if item is not None else ""
        revision_kind: RevisionKind | None = None
        if kind == "revision" and item is not None:
            identifier = str(item.data(_ROLE_IDENTIFIER))
            change = next(
                change for change in self._draft.changes if change.identifier == identifier
            )
            revision_kind = change.kind
        category = {
            "pdf": "Comprobar una página del PDF",
            "translation": "Comprobar la traducción",
            "translation-preserved": "Completar una traducción pendiente",
            "epub-structure": "Confirmar la estructura final del EPUB",
        }.get(kind)
        if revision_kind is RevisionKind.STRUCTURE:
            category = "Comprobar la estructura"
        elif revision_kind is RevisionKind.CONTENT:
            category = "Comprobar una corrección"
        self.review_category_label.setText(category or "Resultado final")

        selectable_original = kind in {"revision", "translation", "translation-preserved"}
        comparison_step = kind != "epub-structure"
        self.original_choice_button.setVisible(selectable_original)
        self.proposal_choice_button.setVisible(comparison_step)
        self.locate_original_button.setVisible(comparison_step)
        self.locate_proposal_button.setVisible(comparison_step)
        for widget in (self.heading_label, self.heading_combo, self.apply_heading_button):
            widget.setVisible(False)
        self.previous_button.setEnabled(row > 0)
        last_step = total == 0 or row + 1 >= total
        self.save_button.setEnabled(item is not None or total == 0)
        self.save_button.setText("Aplicar todos los cambios" if last_step else "Siguiente")
        self.retry_ocr_button.setVisible(kind == "pdf")
        structure_step = kind == "epub-structure"
        self.result_tabs.setTabVisible(self.editor_tab_index, not structure_step)
        self.result_tabs.setTabVisible(self.outline_tab_index, structure_step)
        self.result_tabs.setCurrentIndex(
            self.outline_tab_index if structure_step else self.editor_tab_index
        )

    def _render_decisions(self) -> None:
        rendered = self._draft.render(self._decisions)
        self._syncing = True
        try:
            self.editor.setPlainText(rendered)
            self._change_cursors.clear()
        finally:
            self._syncing = False
        self._schedule_preview()

    def _show_change_detail(self, row: int) -> None:
        if not 0 <= row < self.changes_list.count():
            self._set_change_detail("")
            self._refresh_guided_state()
            return
        item = self.changes_list.item(row)
        identifier = str(item.data(_ROLE_IDENTIFIER))
        kind = str(item.data(_ROLE_KIND))
        if kind == "revision":
            change = next(
                change for change in self._draft.changes if change.identifier == identifier
            )
            existing_cursor = self._change_cursors.get(identifier)
            if existing_cursor is None or not existing_cursor.hasSelection():
                selected_text = (
                    change.proposed_markdown
                    if self._decisions[identifier] is RevisionDecision.ACCEPTED
                    else change.original_markdown
                )
                existing_cursor = self._find_editor_target(selected_text)
                if existing_cursor is not None:
                    self._change_cursors[identifier] = existing_cursor
            proposal = (
                existing_cursor.selectedText().replace("\u2029", "\n")
                if existing_cursor is not None and existing_cursor.hasSelection()
                else change.proposed_markdown
            )
            self._set_change_detail("")
            self._load_text_comparison(
                identifier,
                "revision",
                change.original_markdown,
                proposal,
                original_title="Texto original",
                proposal_title="Corrección propuesta · editable",
                cursor=existing_cursor,
            )
            self._refresh_guided_state()
            return
        if kind == "pdf":
            issue = self._pdf_issue(identifier)
            if issue is None:
                return
            self._set_change_detail(issue.message)
            cursor = self._pdf_issue_cursor(issue)
            proposal = (
                cursor.selectedText().replace("\u2029", "\n")
                if cursor is not None and cursor.hasSelection()
                else issue.markdown
            )
            if cursor is None:
                cursor = QTextCursor(self.editor.document())
            self._load_pdf_comparison(issue, proposal, cursor)
            self._refresh_guided_state()
            return
        if kind == "translation-preserved":
            self._set_change_detail(
                "La IA no superó las comprobaciones en esta parte, así que Parsezen conservó "
                "el original. Corrige a la derecha lo que necesites o confirma que deseas "
                "conservarlo."
            )
            cursor = QTextCursor(self.editor.document())
            cursor.select(QTextCursor.SelectionType.Document)
            text = self.editor.toPlainText()
            self._load_text_comparison(
                identifier,
                kind,
                text,
                text,
                original_title="Documento conservado",
                proposal_title="Resultado · editable",
                cursor=cursor,
            )
            self._refresh_guided_state()
            return
        if kind == "epub-structure":
            self._comparison_identifier = None
            self._comparison_cursor = None
            self._comparison_prefix = ""
            self._set_change_detail("")
            if not self._structure_loaded:
                self.structure_editor.reset_markdown(self.editor.toPlainText())
                self._structure_loaded = True
            self._refresh_guided_state()
            return
        translation_issue = self._translation_issue(identifier)
        if translation_issue is None:
            return
        self._set_change_detail(translation_issue.message)
        cursor = self._find_editor_target(translation_issue.translated_excerpt)
        proposal = (
            cursor.selectedText().replace("\u2029", "\n")
            if cursor is not None and cursor.hasSelection()
            else translation_issue.translated_excerpt
        )
        self._load_text_comparison(
            identifier,
            "translation",
            translation_issue.original_excerpt,
            proposal,
            original_title="Texto original",
            proposal_title="Traducción · editable",
            cursor=cursor,
        )
        self._refresh_guided_state()

    def _set_change_detail(self, message: str) -> None:
        self.change_detail.setText(message)
        self.change_detail.setVisible(bool(message))

    def _pdf_issue(self, identifier: str) -> PdfReviewIssue | None:
        if self._pdf_report is None:
            return None
        return next(
            (issue for issue in self._pdf_report.issues if issue.identifier == identifier),
            None,
        )

    def _translation_issue(self, identifier: str) -> TranslationQualityIssue | None:
        if self._translation_report is None:
            return None
        return next(
            (issue for issue in self._translation_report.issues if issue.identifier == identifier),
            None,
        )

    def _select_pdf_issue(self, issue: PdfReviewIssue) -> None:
        cursor = self._pdf_issue_cursor(issue)
        if cursor is not None:
            self.editor.setTextCursor(cursor)
            self.editor.ensureCursorVisible()

    def _pdf_issue_cursor(self, issue: PdfReviewIssue) -> QTextCursor | None:
        marker = issue.target_marker
        if marker:
            text = self.editor.toPlainText()
            start = text.find(marker)
            if start >= 0:
                end = text.find("<!-- PZDOC PDF PAGE ", start + len(marker))
                cursor = QTextCursor(self.editor.document())
                cursor.setPosition(start)
                cursor.setPosition(
                    len(text) if end < 0 else end,
                    QTextCursor.MoveMode.KeepAnchor,
                )
                return cursor
        return self._find_editor_target(issue.markdown)

    def _select_editor_target(self, target: str, *, identifier: str | None = None) -> None:
        cursor = self._find_editor_target(target)
        if cursor is None:
            return
        if identifier is not None:
            self._change_cursors[identifier] = cursor
        self.editor.setTextCursor(cursor)
        self.editor.ensureCursorVisible()

    def _find_editor_target(self, target: str) -> QTextCursor | None:
        if not target.strip():
            return None
        text = self.editor.toPlainText()
        selected = target
        position = text.find(selected)
        if position < 0:
            excerpt = target.strip()[:160].strip()
            position = text.find(excerpt) if excerpt else -1
            selected = excerpt
        if position >= 0:
            cursor = QTextCursor(self.editor.document())
            cursor.setPosition(position)
            cursor.setPosition(position + len(selected), QTextCursor.MoveMode.KeepAnchor)
            return cursor
        return None

    def _select_editor_range(self, start: int, end: int) -> None:
        cursor = self.editor.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        self.editor.setTextCursor(cursor)
        self.editor.ensureCursorVisible()

    def _editor_changed(self) -> None:
        if not self._syncing:
            self._manual_edits_started = True
            item = self._current_item()
            if item is not None and item.data(_ROLE_KIND) == "revision":
                self._edited_review_ids.add(str(item.data(_ROLE_IDENTIFIER)))
            self._preview_timer.start()
            self._refresh_guided_state()

    def _structure_changed(self) -> None:
        if not self._structure_loaded:
            return
        self._syncing = True
        try:
            self.editor.setPlainText(self.structure_editor.final_markdown())
        finally:
            self._syncing = False
        self._manual_edits_started = True
        self._schedule_preview()

    def _schedule_preview(self) -> None:
        self._preview_timer.start()

    def _refresh_preview(self) -> None:
        text = self.editor.toPlainText()
        if self._epub_title is None:
            if self._plain_text:
                self.preview.set_plain_text(text)
            else:
                self.preview.set_markdown(text, resources=self._resources)
        self.preview_updated.emit()

    def _populate_epub_outline(self, plan: EpubPlan) -> None:
        self.outline.clear()
        chapter_items: dict[int, QTreeWidgetItem] = {}
        for number, chapter in enumerate(plan.chapters, start=1):
            item = QTreeWidgetItem([chapter.title])
            font = QFont(item.font(0))
            font.setBold(True)
            item.setFont(0, font)
            item.setToolTip(0, chapter.filename)
            self.outline.addTopLevelItem(item)
            chapter_items[number] = item
        stacks: dict[int, list[tuple[int, QTreeWidgetItem]]] = {}
        for entry in plan.outline:
            parent = chapter_items[entry.chapter_number]
            stack = stacks.setdefault(entry.chapter_number, [])
            if entry.starts_chapter and entry.title == parent.text(0):
                continue
            while stack and stack[-1][0] >= entry.level:
                stack.pop()
            if stack:
                parent = stack[-1][1]
            item = QTreeWidgetItem([entry.title])
            item.setToolTip(0, f"Nivel Markdown H{entry.level}")
            parent.addChild(item)
            stack.append((entry.level, item))
        self.outline.collapseAll()

    def _apply_heading_level(self) -> None:
        cursor: QTextCursor = self.editor.textCursor()
        line_number = cursor.blockNumber() + 1
        level = self.heading_combo.currentData()
        updated = set_heading_level(self.editor.toPlainText(), line_number, level)
        self.editor.setPlainText(updated)
        target = self.editor.document().findBlockByNumber(line_number - 1)
        restored = self.editor.textCursor()
        restored.setPosition(target.position())
        self.editor.setTextCursor(restored)

    def _save(self) -> None:
        self._commit_comparison_edit()
        if self._structure_loaded:
            self._structure_changed()
        reviewed = self.editor.toPlainText()
        if not reviewed.strip() or "\0" in reviewed:
            QMessageBox.warning(
                self,
                "Resultado vacío",
                "El resultado debe contener texto antes de guardarlo.",
            )
            return
        unresolved = self._unresolved_blocking_items()
        if unresolved:
            QMessageBox.warning(
                self,
                "Revisión pendiente",
                "Revisa y marca los avisos importantes antes de guardar el resultado.",
            )
            self.changes_list.setCurrentItem(unresolved[0])
            return
        self.final_text = reviewed
        self.accept()

    def _retry_current_pdf_page(self) -> None:
        item = self.changes_list.currentItem()
        if item is None or item.data(_ROLE_KIND) != "pdf":
            QMessageBox.information(
                self,
                "Elige una página",
                "Selecciona primero uno de los avisos de página de la lista.",
            )
            return
        issue = self._pdf_issue(str(item.data(_ROLE_IDENTIFIER)))
        if issue is None:
            return
        self.retry_page = issue.page_number
        self.reject()

    def _unresolved_blocking_items(self) -> list[QListWidgetItem]:
        unresolved: list[QListWidgetItem] = []
        for row in range(self.changes_list.count()):
            item = self.changes_list.item(row)
            if not bool(item.data(_ROLE_BLOCKING)):
                continue
            identifier = str(item.data(_ROLE_IDENTIFIER))
            if identifier not in self._resolved_review_ids:
                unresolved.append(item)
        return unresolved
