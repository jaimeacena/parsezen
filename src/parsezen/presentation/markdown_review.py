"""Presentation surface for local, read-only generated-Markdown review."""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast
from urllib.parse import quote, unquote

from PySide6.QtCore import QSize, Qt, QUrl
from PySide6.QtGui import (
    QDesktopServices,
    QImage,
    QPainter,
    QPaintEvent,
    QPixmap,
    QResizeEvent,
    QTextCursor,
    QTextDocument,
)
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from parsezen.document_model import (
    RESOURCE_REFERENCE_PREFIX,
    ConvertedResource,
)
from parsezen.errors import ReviewUnavailableError
from parsezen.pdf_conversion import PdfQualityReport
from parsezen.presentation.design_system import BREAKPOINTS, COLORS, SPACING
from parsezen.translation_quality import TranslationQualityReport

MAX_REVIEW_CHARACTERS = 2_000_000
_INTERNAL_PDF_MARKER_PATTERN = re.compile(
    r"^\s*<!--\s*PZDOC PDF PAGE \d+\s*-->\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def read_markdown_for_review(path: Path) -> str:
    """Read bounded UTF-8 Markdown without exposing its content outside the process."""
    if not path.is_file():
        raise ReviewUnavailableError(
            f"Ya no existe {path.name}. Procesa de nuevo si necesitas revisarlo."
        )
    try:
        with path.open(encoding="utf-8") as markdown_file:
            text = markdown_file.read(MAX_REVIEW_CHARACTERS + 1)
    except (OSError, UnicodeError) as exc:
        raise ReviewUnavailableError(f"No se pudo leer {path.name} para revisarlo.") from exc
    if len(text) > MAX_REVIEW_CHARACTERS:
        raise ReviewUnavailableError(
            "El Markdown es demasiado grande para revisarlo dentro de Parsezen. "
            "Ábrelo con el botón Abrir Markdown."
        )
    return text


def read_markdown_preview(path: Path, max_characters: int) -> tuple[str, bool]:
    """Read only the small prefix needed by the main-window preview."""
    if not path.is_file():
        raise ReviewUnavailableError(
            f"Ya no existe {path.name}. Procesa de nuevo si necesitas revisarlo."
        )
    try:
        with path.open(encoding="utf-8") as markdown_file:
            text = markdown_file.read(max_characters + 1)
    except (OSError, UnicodeError) as exc:
        raise ReviewUnavailableError(f"No se pudo leer {path.name} para revisarlo.") from exc
    truncated = len(text) > max_characters
    preview = text[:max_characters]
    if truncated:
        boundary = preview.rfind("\n\n", max_characters // 2)
        if boundary > 0:
            preview = preview[:boundary]
        if preview.count("```") % 2:
            preview = f"{preview.rstrip()}\n```"
    return preview, truncated


def _markdown_document_style() -> str:
    """Return preview CSS from the currently resolved semantic theme."""

    return f"""
body {{ color: {COLORS.text_primary}; font-family: sans-serif; line-height: 1.45; }}
p {{ margin: 0 0 0.9em 0; }}
h1 {{ font-size: 1.75em; margin: 0.8em 0 0.45em; }}
h2 {{ font-size: 1.45em; margin: 0.75em 0 0.4em; }}
h3 {{ font-size: 1.2em; margin: 0.7em 0 0.35em; }}
h1, h2, h3, h4, h5, h6 {{ color: {COLORS.text_primary}; font-weight: 700; }}
table {{ border-collapse: collapse; margin: 0.8em 0; }}
th {{ background-color: {COLORS.table_header}; color: {COLORS.text_primary}; font-weight: 700; }}
th, td {{ border: 1px solid {COLORS.divider}; padding: 0.35em 0.55em; }}
blockquote {{
    border-left: 3px solid {COLORS.action_primary};
    color: {COLORS.text_secondary};
    margin-left: 0;
    padding-left: 0.8em;
}}
code, pre {{ background-color: {COLORS.surface_subtle}; font-family: monospace; }}
pre {{ padding: 0.7em; }}
a {{ color: {COLORS.info}; }}
img {{ max-width: 100%; }}
"""


class LocalMarkdownView(QTextBrowser):
    """Render GitHub-flavoured Markdown while allowing only sibling local resources."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._allowed_root: Path | None = None
        self._embedded_images: dict[str, QImage] = {}
        self.setReadOnly(True)
        self.setOpenExternalLinks(False)
        self.setOpenLinks(False)

    def set_markdown(
        self,
        markdown: str,
        source_path: Path | None = None,
        resources: tuple[ConvertedResource, ...] = (),
    ) -> None:
        self._embedded_images = {}
        # Page markers are useful anchors in the editable source, but they are
        # application metadata rather than book content.
        rendered_markdown = _INTERNAL_PDF_MARKER_PATTERN.sub("", markdown)
        for resource in resources:
            image = QImage.fromData(resource.content)
            if image.isNull():
                continue
            path = resource.relative_path.as_posix()
            self._embedded_images[path] = image
            rendered_markdown = rendered_markdown.replace(
                f"{RESOURCE_REFERENCE_PREFIX}{path}",
                f"parsezen-resource:///{quote(path, safe='/._-~')}",
            )
        self._allowed_root = (
            source_path.parent.resolve(strict=False) if source_path is not None else None
        )
        if self._allowed_root is not None:
            base = QUrl.fromLocalFile(f"{self._allowed_root.as_posix()}/")
            self.document().setBaseUrl(base)
        else:
            self.document().setBaseUrl(QUrl())
        self.document().setDefaultStyleSheet(_markdown_document_style())
        self.document().setMarkdown(
            rendered_markdown,
            QTextDocument.MarkdownFeature.MarkdownDialectGitHub
            | QTextDocument.MarkdownFeature.MarkdownNoHTML,
        )
        self.moveCursor(QTextCursor.MoveOperation.Start)

    def set_plain_text(self, text: str) -> None:
        """Show TXT results literally instead of interpreting accidental Markdown."""
        self._allowed_root = None
        self._embedded_images = {}
        self.document().setBaseUrl(QUrl())
        self.setPlainText(text)
        self.moveCursor(QTextCursor.MoveOperation.Start)

    def loadResource(self, resource_type: int, name: QUrl | str) -> object | None:  # noqa: N802
        root = self._allowed_root
        resource_url = name if isinstance(name, QUrl) else QUrl(name)
        if resource_url.scheme().lower() == "parsezen-resource":
            path = unquote(resource_url.path().lstrip("/"))
            return self._embedded_images.get(path)
        if root is None or resource_url.scheme().lower() not in {"", "file"}:
            return None
        resolved_url = self.document().baseUrl().resolved(resource_url)
        if not resolved_url.isLocalFile():
            return None
        resource_path = Path(resolved_url.toLocalFile()).resolve(strict=False)
        if not resource_path.is_relative_to(root) or not resource_path.is_file():
            return None
        return cast(object, super().loadResource(resource_type, resolved_url))


class MarkdownReviewDialog(QDialog):
    """Display a result and its optional pre-AI Markdown without editing either file."""

    def __init__(
        self,
        result_text: str,
        result_name: str,
        *,
        original_text: str | None = None,
        original_name: str | None = None,
        result_path: Path | None = None,
        original_path: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("markdownReviewDialog")
        self.setWindowTitle("Revisar Markdown")
        self.setModal(True)
        self.setMinimumSize(320, 400)
        self.resize(960, 680)

        layout = QVBoxLayout(self)
        self.root_layout = layout
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(14)

        title = QLabel("Revisión del Markdown", self)
        title.setObjectName("reviewTitle")
        layout.addWidget(title)

        help_label = QLabel(
            "Vista local de solo lectura. Compara el contenido con el documento original antes "
            "de utilizarlo.",
            self,
        )
        help_label.setObjectName("reviewHelp")
        help_label.setWordWrap(True)
        layout.addWidget(help_label)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setObjectName("reviewSplitter")
        self.splitter.setChildrenCollapsible(False)

        self.original_editor: LocalMarkdownView | None = None
        if original_text is not None:
            original_title = f"Antes de IA · {original_name or 'Markdown original'}"
            original_pane, self.original_editor = self._review_pane(
                original_title,
                original_text,
                "Markdown anterior a la IA",
                original_path,
            )
            self.splitter.addWidget(original_pane)

        result_pane, self.result_editor = self._review_pane(
            f"Resultado · {result_name}",
            result_text,
            "Markdown resultante",
            result_path,
        )
        self.splitter.addWidget(result_pane)
        if original_text is not None:
            self.splitter.setSizes([1, 1])
        layout.addWidget(self.splitter, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=self)
        self.close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
        self.close_button.setText("Cerrar")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        compact = event.size().width() <= BREAKPOINTS.compact
        self.splitter.setOrientation(
            Qt.Orientation.Vertical if compact else Qt.Orientation.Horizontal
        )
        self.root_layout.setContentsMargins(
            *((SPACING.md, SPACING.md, SPACING.md, SPACING.md) if compact else (24, 22, 24, 20))
        )

    def _review_pane(
        self,
        title_text: str,
        markdown: str,
        accessible_name: str,
        source_path: Path | None,
    ) -> tuple[QWidget, LocalMarkdownView]:
        pane = QWidget(self.splitter)
        pane_layout = QVBoxLayout(pane)
        pane_layout.setContentsMargins(0, 0, 0, 0)
        pane_layout.setSpacing(8)

        title = QLabel(title_text, pane)
        title.setObjectName("reviewPaneTitle")
        pane_layout.addWidget(title)

        editor = LocalMarkdownView(pane)
        editor.setObjectName("reviewText")
        editor.setAccessibleName(accessible_name)
        editor.set_markdown(markdown, source_path)
        pane_layout.addWidget(editor, 1)
        return pane, editor


class TranslationQualityReviewDialog(QDialog):
    """Guide the user through bounded, non-blocking translation warnings."""

    def __init__(
        self,
        report: TranslationQualityReport,
        parent: QWidget | None = None,
    ) -> None:
        if not report.issues:
            raise ValueError("A translation review requires at least one visible issue.")
        super().__init__(parent)
        self.setObjectName("translationQualityReviewDialog")
        self.setWindowTitle("Revisar traducción")
        self.setModal(True)
        self.setMinimumSize(320, 460)
        self.resize(980, 640)
        self._report = report
        self._index = 0

        layout = QVBoxLayout(self)
        self.root_layout = layout
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)

        title = QLabel("Fragmentos que conviene revisar", self)
        title.setObjectName("reviewTitle")
        layout.addWidget(title)

        visible_count = len(report.issues)
        summary = f"{report.checked_segments} fragmentos comprobados · {report.total_issues} avisos"
        if visible_count < report.total_issues:
            summary += f" · se muestran los primeros {visible_count}"
        self.summary_label = QLabel(summary, self)
        self.summary_label.setObjectName("qualitySummary")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        navigation = QHBoxLayout()
        self.previous_button = QPushButton("Anterior", self)
        self.previous_button.clicked.connect(self._show_previous)
        self.position_label = QLabel(self)
        self.position_label.setObjectName("reviewPaneTitle")
        self.position_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.next_button = QPushButton("Siguiente", self)
        self.next_button.clicked.connect(self._show_next)
        navigation.addWidget(self.previous_button)
        navigation.addWidget(self.position_label, 1)
        navigation.addWidget(self.next_button)
        layout.addLayout(navigation)

        self.issue_label = QLabel(self)
        self.issue_label.setObjectName("qualityIssueMessage")
        self.issue_label.setWordWrap(True)
        layout.addWidget(self.issue_label)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setObjectName("qualitySplitter")
        self.splitter.setChildrenCollapsible(False)
        original_pane, self.original_editor = self._text_pane(
            "Original",
            "Fragmento original",
        )
        translated_pane, self.translated_editor = self._text_pane(
            "Traducción",
            "Fragmento traducido",
        )
        self.splitter.addWidget(original_pane)
        self.splitter.addWidget(translated_pane)
        self.splitter.setSizes([1, 1])
        layout.addWidget(self.splitter, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=self)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("Cerrar")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._show_issue(0)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        compact = event.size().width() <= BREAKPOINTS.compact
        self.splitter.setOrientation(
            Qt.Orientation.Vertical if compact else Qt.Orientation.Horizontal
        )
        self.root_layout.setContentsMargins(
            *((SPACING.md, SPACING.md, SPACING.md, SPACING.md) if compact else (24, 22, 24, 20))
        )

    def _text_pane(self, title_text: str, accessible_name: str) -> tuple[QWidget, QPlainTextEdit]:
        pane = QWidget(self.splitter)
        pane_layout = QVBoxLayout(pane)
        pane_layout.setContentsMargins(0, 0, 0, 0)
        pane_layout.setSpacing(8)
        title = QLabel(title_text, pane)
        title.setObjectName("reviewPaneTitle")
        pane_layout.addWidget(title)
        editor = QPlainTextEdit(pane)
        editor.setObjectName("reviewText")
        editor.setAccessibleName(accessible_name)
        editor.setReadOnly(True)
        editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        pane_layout.addWidget(editor, 1)
        return pane, editor

    def _show_issue(self, index: int) -> None:
        self._index = index
        issue = self._report.issues[index]
        location = (
            "Comprobación general"
            if issue.segment_number == 0
            else f"Fragmento {issue.segment_number}"
        )
        self.position_label.setText(f"{location} · {index + 1} de {len(self._report.issues)}")
        self.issue_label.setText(issue.message)
        self.original_editor.setPlainText(issue.original_excerpt)
        self.translated_editor.setPlainText(issue.translated_excerpt)
        self.original_editor.moveCursor(QTextCursor.MoveOperation.Start)
        self.translated_editor.moveCursor(QTextCursor.MoveOperation.Start)
        self.previous_button.setEnabled(index > 0)
        self.next_button.setEnabled(index < len(self._report.issues) - 1)

    def _show_previous(self) -> None:
        if self._index > 0:
            self._show_issue(self._index - 1)

    def _show_next(self) -> None:
        if self._index < len(self._report.issues) - 1:
            self._show_issue(self._index + 1)


class _PdfPagePreview(QLabel):
    """Render one local PDF page and keep it fitted inside the review pane."""

    def __init__(self, source_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("pdfPagePreview")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(260, 320)
        self._document = QPdfDocument(self)
        error = self._document.load(str(source_path))
        if error != QPdfDocument.Error.None_:
            raise ReviewUnavailableError(f"No se pudo mostrar {source_path.name} para revisarlo.")
        self._page_pixmap = QPixmap()

    def show_page(self, page_number: int) -> None:
        page_index = page_number - 1
        if page_index < 0 or page_index >= self._document.pageCount():
            raise ReviewUnavailableError(
                f"La página {page_number} ya no está disponible en el PDF."
            )
        point_size = self._document.pagePointSize(page_index)
        render_width = 1_000
        render_height = max(1, round(render_width * point_size.height() / point_size.width()))
        image = self._document.render(page_index, QSize(render_width, render_height))
        if image.isNull():
            raise ReviewUnavailableError(f"No se pudo mostrar la página {page_number} del PDF.")
        self._page_pixmap = QPixmap.fromImage(image)
        self.update()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        super().paintEvent(event)
        if self._page_pixmap.isNull():
            return
        painter = QPainter(self)
        target = self._page_pixmap.scaled(
            max(1, self.width() - 20),
            max(1, self.height() - 20),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = (self.width() - target.width()) // 2
        y = (self.height() - target.height()) // 2
        painter.drawPixmap(x, y, target)


class PdfQualityReviewDialog(QDialog):
    """Guide the user through PDF pages explicitly marked for comparison."""

    def __init__(
        self,
        source_path: Path,
        report: PdfQualityReport,
        *,
        allow_retry: bool,
        parent: QWidget | None = None,
    ) -> None:
        if not report.issues:
            raise ValueError("A guided PDF review requires at least one issue.")
        super().__init__(parent)
        self.setObjectName("pdfQualityReviewDialog")
        self.setWindowTitle("Revisar páginas")
        self.setModal(True)
        self.setMinimumSize(320, 540)
        self.resize(1_080, 720)
        self._source_path = source_path
        self._issues = report.issues
        self._index = 0
        self.retry_page: int | None = None

        layout = QVBoxLayout(self)
        self.root_layout = layout
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)

        title = QLabel("Páginas que necesitan revisión", self)
        title.setObjectName("reviewTitle")
        layout.addWidget(title)

        self.summary_label = QLabel(_quality_summary(report), self)
        self.summary_label.setObjectName("qualitySummary")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        navigation = QHBoxLayout()
        self.previous_button = QPushButton("Anterior", self)
        self.previous_button.clicked.connect(self._show_previous)
        self.page_label = QLabel(self)
        self.page_label.setObjectName("reviewPaneTitle")
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.next_button = QPushButton("Siguiente", self)
        self.next_button.clicked.connect(self._show_next)
        navigation.addWidget(self.previous_button)
        navigation.addWidget(self.page_label, 1)
        navigation.addWidget(self.next_button)
        layout.addLayout(navigation)

        self.issue_label = QLabel(self)
        self.issue_label.setObjectName("qualityIssueMessage")
        self.issue_label.setWordWrap(True)
        layout.addWidget(self.issue_label)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setObjectName("qualitySplitter")
        self.splitter.setChildrenCollapsible(False)

        original_pane = QWidget(self.splitter)
        original_layout = QVBoxLayout(original_pane)
        original_layout.setContentsMargins(0, 0, 0, 0)
        original_layout.setSpacing(7)
        original_title = QLabel("Página original", original_pane)
        original_title.setObjectName("reviewPaneTitle")
        original_layout.addWidget(original_title)
        self.page_preview = _PdfPagePreview(source_path, original_pane)
        self.page_preview.setAccessibleName("Página original del PDF")
        original_layout.addWidget(self.page_preview, 1)

        markdown_pane = QWidget(self.splitter)
        markdown_layout = QVBoxLayout(markdown_pane)
        markdown_layout.setContentsMargins(0, 0, 0, 0)
        markdown_layout.setSpacing(7)
        markdown_title = QLabel("Markdown convertido", markdown_pane)
        markdown_title.setObjectName("reviewPaneTitle")
        markdown_layout.addWidget(markdown_title)
        self.markdown_preview = LocalMarkdownView(markdown_pane)
        self.markdown_preview.setObjectName("qualityMarkdown")
        self.markdown_preview.setAccessibleName("Markdown convertido de la página")
        self.markdown_preview.setOpenExternalLinks(False)
        markdown_layout.addWidget(self.markdown_preview, 1)

        self.splitter.addWidget(original_pane)
        self.splitter.addWidget(markdown_pane)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([500, 500])
        layout.addWidget(self.splitter, 1)

        actions = QGridLayout()
        self.actions_layout = actions
        self.open_original_button = QPushButton("Abrir PDF original", self)
        self.open_original_button.clicked.connect(self._open_original)
        self.retry_button = QPushButton("Reintentar OCR", self)
        self.retry_button.setEnabled(allow_retry)
        self.retry_button.setToolTip(
            "Prepara esta página para un análisis OCR más completo"
            if allow_retry
            else "El OCR más completo ya se utilizó en este resultado"
        )
        self.retry_button.clicked.connect(self._request_retry)
        self.close_button = QPushButton("Cerrar", self)
        self.close_button.clicked.connect(self.reject)
        actions.addWidget(self.open_original_button, 0, 0)
        actions.setColumnStretch(1, 1)
        actions.addWidget(self.retry_button, 0, 2)
        actions.addWidget(self.close_button, 0, 3)
        layout.addLayout(actions)

        self._show_issue(0)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        compact = event.size().width() <= BREAKPOINTS.compact
        self.splitter.setOrientation(
            Qt.Orientation.Vertical if compact else Qt.Orientation.Horizontal
        )
        if compact:
            self.root_layout.setContentsMargins(
                SPACING.md,
                SPACING.md,
                SPACING.md,
                SPACING.md,
            )
            self.actions_layout.addWidget(self.open_original_button, 0, 0, 1, 2)
            self.actions_layout.addWidget(self.retry_button, 1, 0)
            self.actions_layout.addWidget(self.close_button, 1, 1)
        else:
            self.root_layout.setContentsMargins(24, 22, 24, 20)
            self.actions_layout.addWidget(self.open_original_button, 0, 0)
            self.actions_layout.setColumnStretch(1, 1)
            self.actions_layout.addWidget(self.retry_button, 0, 2)
            self.actions_layout.addWidget(self.close_button, 0, 3)

    def _show_issue(self, index: int) -> None:
        self._index = index
        issue = self._issues[index]
        self.page_label.setText(
            f"Página {issue.page_number} del PDF · {index + 1} de {len(self._issues)}"
        )
        self.issue_label.setText(issue.message)
        markdown = issue.markdown.strip() or (
            "*No se reconoció texto legible en esta página. Compárala con el PDF original.*"
        )
        self.markdown_preview.set_markdown(markdown)
        self.markdown_preview.verticalScrollBar().setValue(0)
        self.page_preview.show_page(issue.page_number)
        self.previous_button.setEnabled(index > 0)
        self.next_button.setEnabled(index < len(self._issues) - 1)

    def _show_previous(self) -> None:
        if self._index > 0:
            self._show_issue(self._index - 1)

    def _show_next(self) -> None:
        if self._index < len(self._issues) - 1:
            self._show_issue(self._index + 1)

    def _request_retry(self) -> None:
        self.retry_page = self._issues[self._index].page_number
        self.accept()

    def _open_original(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._source_path)))


def _quality_summary(report: PdfQualityReport) -> str:
    processed = len(report.processed_pages)
    ocr = len(report.ocr_pages)
    issues = len(report.issues)
    page_text = "1 página procesada" if processed == 1 else f"{processed} páginas procesadas"
    if ocr == 0:
        ocr_text = "OCR no necesario"
    elif ocr == 1:
        ocr_text = "1 analizada con OCR"
    else:
        ocr_text = f"{ocr} analizadas con OCR"
    issue_text = "1 necesita revisión" if issues == 1 else f"{issues} necesitan revisión"
    return f"{page_text} · {ocr_text} · {issue_text}"
