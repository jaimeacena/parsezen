"""Compact confirmation for long or delicate processing plans."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractScrollArea,
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
    DocumentPreflight,
    PreflightSeverity,
    QueuePreflight,
)
from parsezen.domain.estimates import DurationEstimate
from parsezen.presentation.components import StatusMessage
from parsezen.presentation.design_system import SPACING, chevron_icon


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
        self.setWindowTitle("Confirmar procesamiento · Parsezen")
        self.setMinimumWidth(320)
        self.resize(640, min(640, 430 + len(preflight.documents) * 55))

        root = QVBoxLayout(self)
        root.setContentsMargins(SPACING.xl, SPACING.xl, SPACING.xl, SPACING.xl)
        root.setSpacing(SPACING.md)

        title = QLabel("Antes de empezar", self)
        title.setObjectName("dialogTitle")
        root.addWidget(title)

        if len(preflight.documents) > 1:
            summary = QLabel(
                f"{len(preflight.documents)} documentos · Duración total aproximada: "
                f"{_friendly_duration_range(preflight.estimate)}",
                self,
            )
            summary.setObjectName("preflightSummary")
            summary.setWordWrap(True)
            root.addWidget(summary)

        self.viewport = QScrollArea(self)
        self.viewport.setObjectName("preflightScroll")
        self.viewport.setWidgetResizable(True)
        self.viewport.setFrameShape(QFrame.Shape.NoFrame)
        self.viewport.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.viewport.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustToContents)
        host = QWidget(self.viewport)
        documents_layout = QVBoxLayout(host)
        documents_layout.setContentsMargins(0, 0, 0, 0)
        documents_layout.setSpacing(SPACING.sm)
        for document in preflight.documents:
            documents_layout.addWidget(
                _DocumentSummary(
                    document,
                    document_names.get(document.job_id, "Documento"),
                    show_duration=len(preflight.documents) > 1,
                    parent=host,
                )
            )

        if len(preflight.documents) == 1:
            duration_title = _section_label("Duración aproximada", host)
            documents_layout.addWidget(duration_title)
            duration = QLabel(_friendly_duration_range(preflight.estimate), host)
            duration.setObjectName("preflightDuration")
            duration.setWordWrap(True)
            documents_layout.addWidget(duration)
            duration_help = QLabel(_duration_help(preflight.documents[0]), host)
            duration_help.setProperty("secondary", True)
            duration_help.setWordWrap(True)
            documents_layout.addWidget(duration_help)

        high_findings = tuple(
            finding
            for document in preflight.documents
            for finding in document.findings
            if finding.severity is PreflightSeverity.HIGH or finding.code == "forced-ocr"
        )
        if high_findings:
            warning = StatusMessage(host)
            warning.show_message(
                " ".join(f"{finding.title}. {finding.detail}" for finding in high_findings),
                tone="warning",
            )
            documents_layout.addWidget(warning)

        early_checks = sum(document.early_check_recommended for document in preflight.documents)
        if early_checks:
            documents_layout.addWidget(_section_label("Comprobación previa", host))
            check_text = (
                "Parsezen probará varias páginas representativas. Si no detecta problemas "
                "importantes, continuará automáticamente con el documento."
                if early_checks == 1
                else f"Parsezen probará páginas representativas de {early_checks} documentos. "
                "Si no detecta problemas importantes, continuará automáticamente."
            )
            check = QLabel(check_text, host)
            check.setWordWrap(True)
            documents_layout.addWidget(check)

        if any(document.expected_reviews for document in preflight.documents):
            documents_layout.addWidget(_section_label("Al finalizar", host))
            completion = QLabel(_completion_text(preflight.documents), host)
            completion.setWordWrap(True)
            documents_layout.addWidget(completion)

        self.details_button = QPushButton("Ver detalles técnicos", self)
        self.details_button.setObjectName("preflightDetailsToggle")
        self.details_button.setCheckable(True)
        self.details_button.setIcon(chevron_icon(expanded=False))
        self.details_button.setIconSize(QSize(16, 16))
        self.details_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.details_button.setAccessibleName("Mostrar detalles técnicos del plan")
        self.details_panel = _TechnicalDetails(
            preflight,
            document_names,
            host,
        )
        self.details_panel.hide()
        documents_layout.addWidget(self.details_panel)
        self.details_button.toggled.connect(self._toggle_details)
        documents_layout.addStretch(1)
        self.viewport.setWidget(host)
        root.addWidget(self.viewport, 1)

        actions = QHBoxLayout()
        actions.addWidget(self.details_button)
        actions.addStretch(1)
        cancel = QPushButton("Cambiar configuración", self)
        cancel.setAccessibleName("Volver a la cola para cambiar la configuración")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        start = QPushButton("Empezar", self)
        start.setObjectName("primaryAction")
        start.setAccessibleName("Aceptar el plan y empezar a procesar")
        start.clicked.connect(self.accept)
        start.setDefault(True)
        actions.addWidget(start)
        root.addLayout(actions)

    def _toggle_details(self, visible: bool) -> None:
        self.details_panel.setVisible(visible)
        self.details_button.setText(
            "Ocultar detalles técnicos" if visible else "Ver detalles técnicos"
        )
        self.details_button.setIcon(chevron_icon(expanded=visible))
        self.details_button.setAccessibleName(
            "Ocultar detalles técnicos del plan"
            if visible
            else "Mostrar detalles técnicos del plan"
        )
        if visible:
            QTimer.singleShot(
                0,
                lambda: self.viewport.ensureWidgetVisible(
                    self.details_panel,
                    0,
                    SPACING.sm,
                ),
            )
        else:
            self.viewport.verticalScrollBar().setValue(0)


class _DocumentSummary(QFrame):
    def __init__(
        self,
        document: DocumentPreflight,
        name: str,
        *,
        show_duration: bool,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("preflightDocument")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, SPACING.sm)
        layout.setSpacing(SPACING.xs)

        name_label = QLabel(name, self)
        name_label.setObjectName("preflightDocumentName")
        name_label.setWordWrap(True)
        layout.addWidget(name_label)

        descriptor_parts = [document.workload_label]
        if show_duration:
            descriptor_parts.append(_friendly_duration_range(document.estimate))
        descriptor = QLabel(" · ".join(descriptor_parts), self)
        descriptor.setProperty("secondary", True)
        descriptor.setWordWrap(True)
        layout.addWidget(descriptor)

        if document.flow_steps:
            flow = QLabel("  →  ".join(document.flow_steps), self)
            flow.setObjectName("preflightFlow")
            flow.setWordWrap(True)
            layout.addWidget(flow)


class _TechnicalDetails(QFrame):
    def __init__(
        self,
        preflight: QueuePreflight,
        document_names: dict[str, str],
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("preflightTechnicalDetails")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, SPACING.sm, 0, 0)
        layout.setSpacing(SPACING.sm)
        for document in preflight.documents:
            if len(preflight.documents) > 1:
                name = QLabel(document_names.get(document.job_id, "Documento"), self)
                name.setObjectName("preflightDocumentName")
                name.setWordWrap(True)
                layout.addWidget(name)
            basis = QLabel(
                f"Base de la estimación: {document.estimate.confidence_label}.",
                self,
            )
            basis.setProperty("secondary", True)
            basis.setWordWrap(True)
            layout.addWidget(basis)
            for finding in document.findings:
                marker = (
                    "Requiere atención" if finding.severity is PreflightSeverity.HIGH else "Detalle"
                )
                finding_label = QLabel(
                    f"{marker}: {finding.title}. {finding.detail}",
                    self,
                )
                finding_label.setObjectName("preflightFinding")
                finding_label.setProperty("severity", finding.severity.value)
                finding_label.setWordWrap(True)
                layout.addWidget(finding_label)
            if document.expected_reviews:
                review_label = QLabel(
                    "Revisión prevista: " + ", ".join(document.expected_reviews) + ".",
                    self,
                )
                review_label.setProperty("secondary", True)
                review_label.setWordWrap(True)
                layout.addWidget(review_label)


def _section_label(text: str, parent: QWidget) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName("preflightSectionTitle")
    return label


def _friendly_duration_range(estimate: DurationEstimate) -> str:
    lower = _friendly_duration(estimate.lower_seconds)
    upper = _friendly_duration(estimate.upper_seconds)
    return lower if lower == upper else f"Entre {lower} y {upper}"


def _friendly_duration(seconds: int) -> str:
    seconds = max(1, seconds)
    if seconds < 60:
        return "menos de 1 min"
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        rounded = max(5, round(minutes / 5) * 5) if minutes >= 10 else minutes
        return f"{rounded} min"
    rounded_minutes = max(60, ((minutes + 15) // 30) * 30)
    hours, remainder = divmod(rounded_minutes, 60)
    return f"{hours} h" if remainder == 0 else f"{hours} h {remainder} min"


def _duration_help(document: DocumentPreflight) -> str:
    local = any(finding.code == "long-local-ai" for finding in document.findings)
    prefix = "El proceso se realizará íntegramente en tu equipo. " if local else ""
    return prefix + "La estimación no incluye el tiempo que dediques a revisar o editar."


def _completion_text(documents: tuple[DocumentPreflight, ...]) -> str:
    epub_review = any("editor EPUB final" in document.expected_reviews for document in documents)
    manual_review = any(
        any(review != "editor EPUB final" for review in document.expected_reviews)
        for document in documents
    )
    if epub_review and manual_review:
        return (
            "Parsezen te mostrará las incidencias que requieran una decisión. Después podrás "
            "revisar y editar el EPUB antes de publicarlo."
        )
    if epub_review:
        return "Podrás revisar y editar el EPUB antes de publicarlo."
    return "Parsezen te mostrará las incidencias que requieran una decisión."
