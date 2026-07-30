"""Small recent-activity view and truthful per-result summaries."""

from __future__ import annotations

from typing import cast

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from parsezen.application.preflight import format_duration
from parsezen.domain.outcomes import OutcomeSummary
from parsezen.job_sessions import RecentJob, RecentJobStatus
from parsezen.presentation.design_system import SPACING


class ActivityView(QWidget):
    """Expose a bounded local journal without behaving like a document library."""

    open_requested = Signal(object)
    folder_requested = Signal(object)
    clear_requested = Signal()

    def __init__(
        self,
        jobs: tuple[RecentJob, ...],
        *,
        allow_clear: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("activityView")
        self._jobs = jobs
        self._compact = False

        root = QVBoxLayout(self)
        root.setContentsMargins(SPACING.lg, SPACING.md, SPACING.lg, SPACING.lg)
        root.setSpacing(SPACING.md)
        description = QLabel(
            "Hasta 20 operaciones recientes de este equipo. "
            "No se guarda el contenido de los documentos.",
            self,
        )
        description.setObjectName("activityHelp")
        description.setWordWrap(True)
        root.addWidget(description)

        self.jobs_list = QListWidget(self)
        self.jobs_list.setObjectName("recentJobsList")
        self.jobs_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.jobs_list.setMinimumHeight(120)
        self.jobs_list.itemSelectionChanged.connect(self._selection_changed)
        self.jobs_list.itemDoubleClicked.connect(self._open_selected)
        root.addWidget(self.jobs_list, 1)

        self.empty_label = QLabel(
            "Todavía no hay actividad reciente.",
            self,
        )
        self.empty_label.setObjectName("activityEmpty")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        root.addWidget(self.empty_label, 1)

        self.details = QFrame(self)
        self.details.setObjectName("activityDetails")
        details_layout = QVBoxLayout(self.details)
        details_layout.setContentsMargins(0, SPACING.md, 0, 0)
        details_layout.setSpacing(SPACING.xs)
        self.details_title = QLabel(self.details)
        self.details_title.setObjectName("activityTitle")
        self.details_title.setWordWrap(True)
        details_layout.addWidget(self.details_title)
        self.details_status = QLabel(self.details)
        self.details_status.setObjectName("activityStatus")
        self.details_status.setWordWrap(True)
        details_layout.addWidget(self.details_status)
        self.details_summary = QLabel(self.details)
        self.details_summary.setObjectName("activitySummary")
        self.details_summary.setWordWrap(True)
        self.details_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details_summary.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        details_layout.addWidget(self.details_summary)
        root.addWidget(self.details)

        self.actions_layout = QGridLayout()
        self.actions_layout.setContentsMargins(0, 0, 0, 0)
        self.actions_layout.setSpacing(SPACING.sm)
        self.open_button = QPushButton("Abrir resultado", self)
        self.open_button.setObjectName("primaryAction")
        self.open_button.clicked.connect(self._open_selected)
        self.folder_button = QPushButton("Abrir carpeta", self)
        self.folder_button.clicked.connect(self._open_folder)
        self.clear_button = QPushButton("Borrar actividad", self)
        self.clear_button.setProperty("dangerAction", True)
        self.clear_button.clicked.connect(self.clear_requested)
        self.clear_button.setVisible(allow_clear)
        self.actions_layout.addWidget(self.open_button, 0, 0)
        self.actions_layout.addWidget(self.folder_button, 0, 1)
        self.actions_layout.setColumnStretch(2, 1)
        self.actions_layout.addWidget(self.clear_button, 0, 3)
        root.addLayout(self.actions_layout)

        self.set_jobs(jobs)

    @property
    def jobs(self) -> tuple[RecentJob, ...]:
        return self._jobs

    def set_jobs(self, jobs: tuple[RecentJob, ...]) -> None:
        self._jobs = jobs
        self.jobs_list.clear()
        for index, job in enumerate(jobs):
            item = QListWidgetItem(
                f"{job.source_path.name}\n"
                f"{_status_text(job.status)} · "
                f"{job.finished_at.astimezone().strftime('%d/%m/%Y · %H:%M')}"
            )
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setToolTip(str(job.result_path or job.source_path))
            self.jobs_list.addItem(item)
        self.jobs_list.setVisible(bool(jobs))
        self.empty_label.setVisible(not jobs)
        self.details.setVisible(bool(jobs))
        self.clear_button.setEnabled(bool(jobs))
        if jobs:
            self.jobs_list.setCurrentRow(0)
        self._selection_changed()

    def set_compact_mode(self, compact: bool) -> None:
        if compact == self._compact:
            return
        self._compact = compact
        for widget in (self.open_button, self.folder_button, self.clear_button):
            self.actions_layout.removeWidget(widget)
        if compact:
            self.actions_layout.addWidget(self.open_button, 0, 0, 1, 2)
            self.actions_layout.addWidget(self.folder_button, 1, 0, 1, 2)
            self.actions_layout.addWidget(self.clear_button, 2, 0, 1, 2)
        else:
            self.actions_layout.addWidget(self.open_button, 0, 0)
            self.actions_layout.addWidget(self.folder_button, 0, 1)
            self.actions_layout.addWidget(self.clear_button, 0, 3)

    def _selected_job(self) -> RecentJob | None:
        selected = self.jobs_list.selectedItems()
        if not selected:
            return None
        item = selected[0]
        index = cast(object, item.data(Qt.ItemDataRole.UserRole))
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index < len(self._jobs)
        ):
            return None
        return self._jobs[index]

    def _selection_changed(self) -> None:
        job = self._selected_job()
        if job is None:
            self.details_title.clear()
            self.details_status.clear()
            self.details_summary.clear()
            self.open_button.setEnabled(False)
            self.folder_button.setEnabled(False)
            return
        self.details_title.setText(job.source_path.name)
        self.details_status.setText(
            f"{_status_text(job.status)} · "
            f"{job.finished_at.astimezone().strftime('%d/%m/%Y · %H:%M')}"
        )
        lines = outcome_summary_lines(job.summary)
        self.details_summary.setText("\n".join(lines))
        result_available = bool(job.result_path is not None and job.result_path.is_file())
        self.open_button.setEnabled(result_available)
        self.folder_button.setEnabled(result_available)

    def _open_selected(self, *_args: object) -> None:
        job = self._selected_job()
        if job is not None and job.result_path is not None and job.result_path.is_file():
            self.open_requested.emit(job.result_path)

    def _open_folder(self) -> None:
        job = self._selected_job()
        if job is not None and job.result_path is not None and job.result_path.is_file():
            self.folder_requested.emit(job.result_path.parent)


def outcome_summary_lines(summary: OutcomeSummary | None) -> tuple[str, ...]:
    """Render integrity, incidents and human review as distinct statements."""

    if summary is None:
        return ("El resumen detallado no está disponible para esta operación anterior.",)

    integrity_text = (
        f"Integridad técnica: comprobada mediante {summary.integrity_checks} "
        f"{'control' if summary.integrity_checks == 1 else 'controles'}"
        if summary.integrity_verified
        else "Integridad técnica: no consta una comprobación final detallada"
    )
    if summary.integrity_warnings:
        integrity_text += (
            f" · {summary.integrity_warnings} "
            f"{'aviso' if summary.integrity_warnings == 1 else 'avisos'}"
        )
    lines = [
        f"Flujo: {' → '.join(summary.operations)} · Resultado {summary.output_format}.",
        integrity_text + ".",
    ]
    incident_parts = []
    if summary.conversion_issues:
        incident_parts.append(f"{summary.conversion_issues} de conversión u OCR")
    if summary.translation_issues:
        incident_parts.append(f"{summary.translation_issues} de traducción")
    if summary.preserved_segments:
        incident_parts.append(f"{summary.preserved_segments} fragmentos conservados por seguridad")
    if incident_parts:
        lines.append("Incidencias detectadas: " + ", ".join(incident_parts) + ".")
    else:
        lines.append("Incidencias detectadas: ninguna señal registrada.")

    if summary.review_units:
        lines.append(
            f"Revisión manual: {summary.review_units} decisiones · "
            f"{summary.review_changes} cambios aplicados · "
            f"{summary.review_originals} originales conservados"
            + (f" · {summary.review_edits} ediciones propias." if summary.review_edits else ".")
        )
    elif summary.editor_completed:
        lines.append("Revisión manual: editor EPUB final completado.")
    elif summary.manual_review_expected and not incident_parts:
        lines.append("Revisión manual: no hubo incidencias que exigieran una decisión.")
    elif summary.manual_review_expected:
        lines.append("Revisión manual: no consta una decisión manual en este resumen.")
    else:
        lines.append("Revisión manual: no estaba activada para este flujo.")

    facts = []
    if summary.processed_pages:
        facts.append(f"{summary.processed_pages} páginas")
    if summary.ocr_pages:
        facts.append(f"{summary.ocr_pages} con OCR")
    if summary.preserved_images:
        facts.append(f"{summary.preserved_images} imágenes")
    if summary.chapters:
        facts.append(f"{summary.chapters} capítulos")
    if facts:
        lines.append("Contenido: " + " · ".join(facts) + ".")
    if summary.early_check_pages:
        lines.append(
            f"Comprobación temprana: {summary.early_check_pages} páginas"
            + (
                f" · {summary.early_check_warnings} con avisos."
                if summary.early_check_warnings
                else " · sin riesgos materiales."
            )
        )
    if summary.duration_seconds is not None:
        duration = format_duration(summary.duration_seconds)
        comparison = _duration_comparison(summary)
        lines.append(f"Tiempo automático: {duration}{comparison}")
    return tuple(lines)


def _duration_comparison(summary: OutcomeSummary) -> str:
    lower = summary.estimate_lower_seconds
    upper = summary.estimate_upper_seconds
    actual = summary.duration_seconds
    if lower is None or upper is None or actual is None:
        return "."
    planned = f"{format_duration(lower)}–{format_duration(upper)}"
    if actual > upper:
        return f" · superó el intervalo previsto ({planned})."
    if actual < lower:
        return f" · terminó antes del intervalo previsto ({planned})."
    return f" · dentro del intervalo previsto ({planned})."


def _status_text(status: RecentJobStatus) -> str:
    return {
        RecentJobStatus.COMPLETED: "Completado",
        RecentJobStatus.FAILED: "Con error",
        RecentJobStatus.CANCELLED: "Cancelado",
    }[status]
