"""Compact confirmation for long or delicate processing plans."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from parsezen.application.preflight import (
    PreflightSeverity,
    QueuePreflight,
    format_duration_range,
)
from parsezen.presentation.design_system import SPACING


class PreflightDialog(QDialog):
    """Give the user a useful last check without exposing technical internals."""

    def __init__(
        self,
        preflight: QueuePreflight,
        document_names: dict[str, str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("preflightDialog")
        self.setWindowTitle("Antes de empezar · Parsezen")
        self.setMinimumWidth(320)
        self.resize(720, min(720, 280 + len(preflight.documents) * 130))

        root = QVBoxLayout(self)
        root.setContentsMargins(SPACING.xl, SPACING.xl, SPACING.xl, SPACING.xl)
        root.setSpacing(SPACING.md)

        title = QLabel("Comprueba el plan", self)
        title.setObjectName("dialogTitle")
        root.addWidget(title)
        count = len(preflight.documents)
        noun = "documento" if count == 1 else "documentos"
        summary = QLabel(
            f"{count} {noun} · Tiempo automático aproximado: "
            f"{format_duration_range(preflight.estimate)}",
            self,
        )
        summary.setObjectName("preflightSummary")
        summary.setWordWrap(True)
        root.addWidget(summary)
        help_text = QLabel(
            "La estimación no incluye el tiempo que dediques a revisar o editar. "
            f"{preflight.estimate.confidence_label}.",
            self,
        )
        help_text.setProperty("secondary", True)
        help_text.setWordWrap(True)
        root.addWidget(help_text)

        viewport = QScrollArea(self)
        viewport.setWidgetResizable(True)
        viewport.setFrameShape(QFrame.Shape.NoFrame)
        viewport.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget(viewport)
        documents_layout = QVBoxLayout(host)
        documents_layout.setContentsMargins(0, 0, 0, 0)
        documents_layout.setSpacing(0)
        for document in preflight.documents:
            row = QFrame(host)
            row.setObjectName("preflightDocument")
            row_layout = QVBoxLayout(row)
            row_layout.setContentsMargins(0, SPACING.md, 0, SPACING.md)
            row_layout.setSpacing(SPACING.xs)
            name = QLabel(document_names.get(document.job_id, "Documento"), row)
            name.setObjectName("preflightDocumentName")
            name.setWordWrap(True)
            row_layout.addWidget(name)
            detail = QLabel(
                f"{document.workload_label} · aprox. "
                f"{format_duration_range(document.estimate)} · "
                f"{document.estimate.confidence_label}",
                row,
            )
            detail.setProperty("secondary", True)
            detail.setWordWrap(True)
            row_layout.addWidget(detail)
            for finding in document.findings:
                marker = (
                    "Requiere atención"
                    if finding.severity is PreflightSeverity.HIGH
                    else "Ten en cuenta"
                    if finding.severity is PreflightSeverity.ATTENTION
                    else "Plan"
                )
                finding_label = QLabel(
                    f"{marker}: {finding.title}. {finding.detail}",
                    row,
                )
                finding_label.setObjectName("preflightFinding")
                finding_label.setProperty("severity", finding.severity.value)
                finding_label.setWordWrap(True)
                row_layout.addWidget(finding_label)
            if document.expected_reviews:
                review_label = QLabel(
                    "Revisión prevista: " + ", ".join(document.expected_reviews) + ".",
                    row,
                )
                review_label.setProperty("secondary", True)
                review_label.setWordWrap(True)
                row_layout.addWidget(review_label)
            documents_layout.addWidget(row)
        documents_layout.addStretch(1)
        viewport.setWidget(host)
        root.addWidget(viewport, 1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = QPushButton("Volver", self)
        cancel.setAccessibleName("Volver a la cola sin iniciar el procesamiento")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        start = QPushButton("Empezar", self)
        start.setObjectName("primaryAction")
        start.setAccessibleName("Aceptar el plan y empezar a procesar")
        start.clicked.connect(self.accept)
        start.setDefault(True)
        actions.addWidget(start)
        root.addLayout(actions)
