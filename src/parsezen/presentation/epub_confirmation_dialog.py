"""Lightweight final confirmation shown before every EPUB publication."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from parsezen.application.artifact_repository import ArtifactRepository
from parsezen.application.book_editor import BookEditor
from parsezen.domain.books import BookDocument
from parsezen.presentation.design_system import COLORS, SPACING


class EpubConfirmationDialog(QDialog):
    """Confirm essential book facts without forcing the full chapter editor."""

    def __init__(
        self,
        book: BookDocument,
        artifacts: ArtifactRepository,
        *,
        job_id: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._book = book
        self._artifacts = artifacts
        self._job_id = job_id
        self._open_editor_requested = False
        self._saved_for_later = False
        self.setWindowTitle("Confirmar libro EPUB · Parsezen")
        self.setObjectName("epubConfirmationDialog")
        self.resize(640, 430)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACING.lg, SPACING.lg, SPACING.lg, SPACING.lg)
        layout.setSpacing(SPACING.md)
        title = QLabel("Confirma el libro antes de publicarlo", self)
        title.setObjectName("dialogTitle")
        layout.addWidget(title)
        explanation = QLabel(
            "Puedes corregir los datos básicos aquí. El contenido y los recursos no se "
            "modifican salvo que abras el editor completo.",
            self,
        )
        explanation.setObjectName("confirmationHelp")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.title_input = QLineEdit(book.metadata.title, self)
        self.author_input = QLineEdit(book.metadata.author or "", self)
        self.language_input = QLineEdit(book.metadata.language, self)
        self.language_input.setMaxLength(40)
        self.cover_value = QLabel(
            "Incluida" if book.cover_resource_id is not None else "Sin portada",
            self,
        )
        self.chapter_value = QLabel(str(len(book.spine)), self)
        form.addRow("Título", self.title_input)
        form.addRow("Autor", self.author_input)
        form.addRow("Idioma", self.language_input)
        form.addRow("Portada", self.cover_value)
        form.addRow("Capítulos", self.chapter_value)
        layout.addLayout(form)

        note = QLabel(
            "Para cambiar la portada, reorganizar capítulos o editar el contenido, abre el "
            "editor completo.",
            self,
        )
        note.setObjectName("confirmationHelp")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)

        actions = QHBoxLayout()
        self.save_later_button = QPushButton("Guardar y salir", self)
        self.editor_button = QPushButton("Abrir editor completo", self)
        self.publish_button = QPushButton("Publicar EPUB", self)
        self.publish_button.setObjectName("primaryButton")
        actions.addWidget(self.save_later_button)
        actions.addStretch(1)
        actions.addWidget(self.editor_button)
        actions.addWidget(self.publish_button)
        layout.addLayout(actions)

        self.save_later_button.clicked.connect(self._save_and_close)
        self.editor_button.clicked.connect(self._open_editor)
        self.publish_button.clicked.connect(self._publish)
        self.setStyleSheet(
            f"""
            QDialog#epubConfirmationDialog {{ background: {COLORS.canvas}; }}
            QLabel#confirmationHelp {{ color: {COLORS.text_secondary}; }}
            """
        )

    @property
    def book(self) -> BookDocument:
        return self._book

    @property
    def open_editor_requested(self) -> bool:
        return self._open_editor_requested

    @property
    def saved_for_later(self) -> bool:
        return self._saved_for_later

    def reject(self) -> None:
        """Closing the page preserves the draft instead of publishing it."""

        self._save_and_close()

    def _update_metadata(self) -> bool:
        try:
            self._book = BookEditor(
                self._book,
                self._artifacts,
                job_id=self._job_id,
            ).update_metadata(
                title=self.title_input.text(),
                author=self.author_input.text(),
                language=self.language_input.text(),
            )
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Datos no válidos", str(exc))
            return False
        return True

    def _publish(self) -> None:
        if self._update_metadata():
            self._saved_for_later = False
            self.accept()

    def _open_editor(self) -> None:
        if self._update_metadata():
            self._open_editor_requested = True
            self._saved_for_later = False
            self.accept()

    def _save_and_close(self) -> None:
        if not self._update_metadata():
            return
        self._saved_for_later = True
        super().reject()
