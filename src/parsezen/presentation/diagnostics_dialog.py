"""Presentation dialog for local diagnostics with a safe copy action."""

from __future__ import annotations

from PySide6.QtCore import Slot
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class DiagnosticsDialog(QDialog):
    """Display an already-sanitized report without performing diagnostics itself."""

    def __init__(self, report: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("diagnosticsDialog")
        self.setWindowTitle("Diagnóstico — Parsezen")
        self.resize(700, 560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)

        explanation = QLabel(
            "Resumen técnico local para comprobar la instalación o compartir un problema.",
            self,
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        self.report_view = QPlainTextEdit(self)
        self.report_view.setObjectName("diagnosticsReport")
        self.report_view.setReadOnly(True)
        self.report_view.setPlainText(report)
        layout.addWidget(self.report_view, 1)

        self.copy_status = QLabel("", self)
        self.copy_status.setObjectName("diagnosticsCopyStatus")
        layout.addWidget(self.copy_status)

        buttons = QDialogButtonBox(self)
        self.copy_button = QPushButton("Copiar diagnóstico", buttons)
        self.copy_button.clicked.connect(self._copy_report)
        buttons.addButton(self.copy_button, QDialogButtonBox.ButtonRole.ActionRole)
        close_button = buttons.addButton(QDialogButtonBox.StandardButton.Close)
        close_button.setText("Cerrar")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @Slot()
    def _copy_report(self) -> None:
        clipboard = QApplication.clipboard()
        clipboard.setText(self.report_view.toPlainText())
        self.copy_status.setText("Diagnóstico copiado. No contiene datos de tus documentos.")
