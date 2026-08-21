"""Small recent-activity view and truthful per-result summaries."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import cast

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from parsezen import __version__
from parsezen.application.preflight import format_duration
from parsezen.domain.attempt_activity import (
    AttemptEvent,
    AttemptEventStatus,
    AttemptPhase,
    FailureSnapshot,
    ReusableWork,
    is_safe_token,
)
from parsezen.domain.outcomes import OutcomeSummary
from parsezen.presentation.design_system import SPACING
from parsezen.recent_activity import RecentJob, RecentJobStatus

_PHASE_LABELS = {
    AttemptPhase.PREPARATION: "preparación",
    AttemptPhase.EARLY_CHECK: "comprobación temprana",
    AttemptPhase.TRANSLATION: "traducción",
    AttemptPhase.CORRECTION: "corrección",
    AttemptPhase.PERSONALIZATION: "personalización",
    AttemptPhase.PUBLICATION: "publicación",
    AttemptPhase.COMPLETION: "finalización",
    AttemptPhase.FAILURE: "error",
    AttemptPhase.CANCELLATION: "cancelación",
    AttemptPhase.PAUSE: "pausa",
}
_EVENT_STATUS_LABELS = {
    AttemptEventStatus.STARTED: "iniciada",
    AttemptEventStatus.COMPLETED: "completada",
    AttemptEventStatus.FAILED: "fallida",
    AttemptEventStatus.CANCELLED: "cancelada",
    AttemptEventStatus.PAUSED: "pausada",
}
_REUSABLE_WORK_LABELS = {
    ReusableWork.NONE: "No se conservó trabajo reutilizable de este intento.",
    ReusableWork.PREPARATION_CHECKPOINTS: (
        "Los checkpoints válidos de preparación se conservan y se reutilizarán al reintentar."
    ),
    ReusableWork.PREVIOUS_PHASES: (
        "Las fases anteriores y sus checkpoints válidos se conservan y se reutilizarán al "
        "reintentar."
    ),
}


class ActivityView(QWidget):
    """Expose a bounded local journal without behaving like a document library."""

    open_requested = Signal(object)
    folder_requested = Signal(object)
    return_to_document_requested = Signal(str)
    diagnostic_copied = Signal()
    clear_requested = Signal()

    def __init__(
        self,
        jobs: tuple[RecentJob, ...],
        *,
        allow_clear: bool = True,
        current_job_ids: Mapping[Path | str, str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("activityView")
        self.setAccessibleName("Actividad reciente")
        self._jobs = jobs
        self._compact = False
        self._allow_clear = allow_clear
        self._current_job_ids = {
            _source_key(Path(path)): job_id for path, job_id in (current_job_ids or {}).items()
        }
        self._feedback_timer = QTimer(self)
        self._feedback_timer.setSingleShot(True)

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
        description.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(description)

        self.panels = QSplitter(Qt.Orientation.Horizontal, self)
        self.panels.setObjectName("activityPanels")
        self.panels.setAccessibleName("Historial y detalle de actividad")
        self.panels.setChildrenCollapsible(False)
        self.panels.setHandleWidth(SPACING.md)

        self.history_panel = QFrame(self.panels)
        self.history_panel.setObjectName("activityHistoryPanel")
        history_layout = QVBoxLayout(self.history_panel)
        history_layout.setContentsMargins(SPACING.sm, SPACING.sm, SPACING.sm, SPACING.sm)
        history_layout.setSpacing(0)
        self.jobs_list = QListWidget(self.history_panel)
        self.jobs_list.setObjectName("recentJobsList")
        self.jobs_list.setAccessibleName("Operaciones recientes")
        self.jobs_list.setAccessibleDescription(
            "Usa las flechas para seleccionar una operación y sus detalles."
        )
        self.jobs_list.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.jobs_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.jobs_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.jobs_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.jobs_list.setWordWrap(False)
        self.jobs_list.setMinimumHeight(120)
        self.jobs_list.itemSelectionChanged.connect(self._selection_changed)
        self.jobs_list.itemDoubleClicked.connect(self._open_selected)
        history_layout.addWidget(self.jobs_list, 1)
        self.panels.addWidget(self.history_panel)

        self.empty_label = QLabel(
            "Todavía no hay actividad reciente.",
            self,
        )
        self.empty_label.setObjectName("activityEmpty")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        root.addWidget(self.empty_label, 1)

        self.details = QFrame(self.panels)
        self.details.setObjectName("activityDetails")
        details_layout = QVBoxLayout(self.details)
        details_layout.setContentsMargins(
            SPACING.lg,
            SPACING.md,
            SPACING.lg,
            SPACING.md,
        )
        details_layout.setSpacing(SPACING.sm)
        self.details_title = QLabel(self.details)
        self.details_title.setObjectName("activityTitle")
        self.details_title.setWordWrap(True)
        self._make_selectable(self.details_title)
        details_layout.addWidget(self.details_title)
        self.details_status = QLabel(self.details)
        self.details_status.setObjectName("activityStatus")
        self.details_status.setWordWrap(True)
        self._make_selectable(self.details_status)
        details_layout.addWidget(self.details_status)

        self.details_summary = QLabel(self.details)
        self.details_summary.setObjectName("activitySummary")
        self.details_summary.setWordWrap(True)
        self.details_summary.setTextFormat(Qt.TextFormat.RichText)
        self._make_selectable(self.details_summary)
        self.details_summary.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        details_layout.addWidget(self.details_summary)

        self.failure_heading = QLabel("Qué ocurrió", self.details)
        self.failure_heading.setObjectName("activityFailureHeading")
        self._make_selectable(self.failure_heading)
        details_layout.addWidget(self.failure_heading)
        self.failure_message = QLabel(self.details)
        self.failure_message.setObjectName("activityFailureMessage")
        self.failure_message.setWordWrap(True)
        self._make_selectable(self.failure_message)
        details_layout.addWidget(self.failure_message)

        self.reusable_work_heading = QLabel("Tu trabajo", self.details)
        self.reusable_work_heading.setObjectName("activityFailureHeading")
        self._make_selectable(self.reusable_work_heading)
        details_layout.addWidget(self.reusable_work_heading)
        self.reusable_work = QLabel(self.details)
        self.reusable_work.setObjectName("activityReusableWork")
        self.reusable_work.setWordWrap(True)
        self._make_selectable(self.reusable_work)
        details_layout.addWidget(self.reusable_work)

        self.timeline_heading = QLabel("Recorrido", self.details)
        self.timeline_heading.setObjectName("activityFailureHeading")
        self._make_selectable(self.timeline_heading)
        details_layout.addWidget(self.timeline_heading)
        self.timeline_text = QLabel(self.details)
        self.timeline_text.setObjectName("activityTimeline")
        self.timeline_text.setWordWrap(True)
        self._make_selectable(self.timeline_text)
        details_layout.addWidget(self.timeline_text)

        self.failure_reference = QLabel(self.details)
        self.failure_reference.setObjectName("activityReference")
        self.failure_reference.setWordWrap(True)
        self._make_selectable(self.failure_reference)
        details_layout.addWidget(self.failure_reference)

        self.recovery_note = QLabel(self.details)
        self.recovery_note.setObjectName("activityRecoveryNote")
        self.recovery_note.setWordWrap(True)
        self._make_selectable(self.recovery_note)
        details_layout.addWidget(self.recovery_note)
        details_layout.addStretch(1)

        self.actions_layout = QGridLayout()
        self.actions_layout.setContentsMargins(0, 0, 0, 0)
        self.actions_layout.setSpacing(SPACING.sm)
        self.open_button = QPushButton("Abrir resultado", self)
        self.open_button.setObjectName("primaryAction")
        self.open_button.setAccessibleName("Abrir resultado")
        self.open_button.clicked.connect(self._open_selected)
        self.folder_button = QPushButton("Abrir carpeta", self)
        self.folder_button.setAccessibleName("Abrir carpeta")
        self.folder_button.clicked.connect(self._open_folder)
        self.return_button = QPushButton("Volver al documento", self)
        self.return_button.setObjectName("primaryAction")
        self.return_button.setAccessibleName("Volver al documento")
        self.return_button.setAccessibleDescription(
            "Vuelve a la fila actual para usar sus acciones de recuperación."
        )
        self.return_button.clicked.connect(self._return_to_document)
        self.copy_diagnostic_button = QPushButton("Copiar diagnóstico", self)
        self.copy_diagnostic_button.setAccessibleName("Copiar diagnóstico")
        self.copy_diagnostic_button.setAccessibleDescription(
            "Copia un diagnóstico técnico local sin nombre, ruta ni contenido del documento."
        )
        self.copy_diagnostic_button.clicked.connect(self._copy_diagnostic)
        self.clear_button = QPushButton("Borrar actividad", self.details)
        self.clear_button.setAccessibleName("Borrar actividad")
        self.clear_button.setProperty("dangerAction", True)
        self.clear_button.clicked.connect(self.clear_requested)
        self.clear_button.setVisible(allow_clear)
        self.actions_layout.addWidget(self.open_button, 0, 0)
        self.actions_layout.addWidget(self.folder_button, 0, 1)
        self.actions_layout.addWidget(self.return_button, 0, 2)
        self.actions_layout.addWidget(self.copy_diagnostic_button, 0, 3)
        self.actions_layout.setColumnStretch(4, 1)
        self.actions_layout.addWidget(self.clear_button, 0, 5)
        details_layout.addLayout(self.actions_layout)

        self.copy_feedback = QLabel(self.details)
        self.copy_feedback.setObjectName("activityFeedback")
        self.copy_feedback.setWordWrap(True)
        self._make_selectable(self.copy_feedback)
        self.copy_feedback.hide()
        self._feedback_timer.timeout.connect(self.copy_feedback.hide)
        details_layout.addWidget(self.copy_feedback)

        self.panels.addWidget(self.details)
        self.panels.setStretchFactor(0, 0)
        self.panels.setStretchFactor(1, 1)
        self.panels.setSizes((360, 920))
        root.addWidget(self.panels, 1)

        # Start with the narrow layout so a small standalone view can negotiate
        # a compact minimum width before its first show/resize event.
        self.set_compact_mode(True)
        self.set_jobs(jobs)

    @property
    def jobs(self) -> tuple[RecentJob, ...]:
        return self._jobs

    def set_jobs(self, jobs: tuple[RecentJob, ...]) -> None:
        self._jobs = jobs
        self.jobs_list.clear()
        for index, job in enumerate(jobs):
            status = _job_status_text(job)
            finished = _local_time(job.finished_at)
            item = QListWidgetItem(f"{job.source_path.name}\n{status} · {finished}")
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setData(
                Qt.ItemDataRole.AccessibleTextRole,
                f"{job.source_path.name}. {status}. Finalizado {finished}.",
            )
            if job.status is RecentJobStatus.COMPLETED and job.result_path is not None:
                item.setToolTip(str(job.result_path))
            self.jobs_list.addItem(item)
        self.jobs_list.setVisible(bool(jobs))
        self.empty_label.setVisible(not jobs)
        self.panels.setVisible(bool(jobs))
        self.clear_button.setEnabled(bool(jobs))
        if jobs:
            self.jobs_list.setCurrentRow(0)
        self._selection_changed()

    def set_current_job_ids(self, current_job_ids: Mapping[Path | str, str]) -> None:
        """Refresh which failed history entries can return to the live queue."""

        self._current_job_ids = {
            _source_key(Path(path)): job_id for path, job_id in current_job_ids.items()
        }
        self._selection_changed()

    def set_compact_mode(self, compact: bool) -> None:
        if compact == self._compact:
            return
        self._compact = compact
        for widget in (
            self.open_button,
            self.folder_button,
            self.return_button,
            self.copy_diagnostic_button,
            self.clear_button,
        ):
            self.actions_layout.removeWidget(widget)
        if compact:
            self.panels.setOrientation(Qt.Orientation.Vertical)
            self.panels.setSizes((190, 430))
            self.actions_layout.addWidget(self.open_button, 0, 0)
            self.actions_layout.addWidget(self.folder_button, 0, 1)
            self.actions_layout.addWidget(self.return_button, 0, 0, 1, 2)
            self.actions_layout.addWidget(self.copy_diagnostic_button, 1, 0, 1, 2)
            self.actions_layout.addWidget(self.clear_button, 2, 0, 1, 2)
            self.actions_layout.setColumnStretch(2, 0)
            self.actions_layout.setColumnStretch(4, 0)
        else:
            self.panels.setOrientation(Qt.Orientation.Horizontal)
            self.panels.setSizes((360, 920))
            self.actions_layout.addWidget(self.open_button, 0, 0)
            self.actions_layout.addWidget(self.folder_button, 0, 1)
            self.actions_layout.addWidget(self.return_button, 0, 2)
            self.actions_layout.addWidget(self.copy_diagnostic_button, 0, 3)
            self.actions_layout.setColumnStretch(4, 1)
            self.actions_layout.addWidget(self.clear_button, 0, 5)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        compact = self.width() <= 760
        if compact != self._compact:
            self.set_compact_mode(compact)

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
            self._set_failure_widgets_visible(False)
            self._set_action_state(None)
            return
        self.details_title.setText(job.source_path.name)
        self.details_status.setText(_job_status_line(job))
        if job.status is RecentJobStatus.FAILED:
            self._show_failure_details(job)
        else:
            self._show_non_failure_details(job)

    def _show_non_failure_details(self, job: RecentJob) -> None:
        self.details_summary.setVisible(True)
        self.details_summary.setText(_summary_html(outcome_summary_lines(job.summary)))
        self._set_failure_widgets_visible(False)
        self._set_action_state(job)

    def _show_failure_details(self, job: RecentJob) -> None:
        failure = job.failure
        self.details_summary.clear()
        self.details_summary.hide()
        self._set_failure_widgets_visible(True)
        self.failure_heading.setText("Qué ocurrió")
        self.failure_message.setText(
            failure.safe_message
            if failure is not None
            else "No se registraron más detalles locales sobre este error."
        )
        reusable_work = failure.reusable_work if failure is not None else ReusableWork.NONE
        self.reusable_work.setText(_REUSABLE_WORK_LABELS[reusable_work])
        self.timeline_text.setText(_timeline_display(job.timeline))

        reference_parts: list[str] = []
        reference = failure.diagnostic_reference if failure is not None else None
        has_safe_reference = reference is not None and is_safe_token(reference, maximum=64)
        has_safe_attempt = is_safe_token(job.attempt_id or "")
        if has_safe_reference and reference is not None and len(reference) <= 16:
            reference_parts.append(f"Referencia local: {reference}")
        elif has_safe_reference or has_safe_attempt:
            reference_parts.append("La referencia técnica se incluye al copiar el diagnóstico.")
        self.failure_reference.setText(" · ".join(reference_parts))
        self.failure_reference.setVisible(bool(reference_parts))

        current_job_id = self._current_job_ids.get(_source_key(job.source_path))
        if current_job_id is not None:
            self.recovery_note.setText(
                "Este documento sigue en la cola. Puedes volver a él para usar sus acciones "
                "de recuperación."
            )
        else:
            self.recovery_note.setText(
                "Las acciones de recuperación solo están disponibles mientras el documento "
                "fallido permanece en la cola."
            )
        self._set_action_state(job, current_job_id=current_job_id)

    def _set_failure_widgets_visible(self, visible: bool) -> None:
        for widget in (
            self.failure_heading,
            self.failure_message,
            self.reusable_work_heading,
            self.reusable_work,
            self.timeline_heading,
            self.timeline_text,
            self.recovery_note,
        ):
            widget.setVisible(visible)
        if not visible:
            self.failure_reference.hide()

    def _set_action_state(
        self,
        job: RecentJob | None,
        *,
        current_job_id: str | None = None,
    ) -> None:
        is_failure = job is not None and job.status is RecentJobStatus.FAILED
        result_available = bool(
            job is not None
            and job.status is RecentJobStatus.COMPLETED
            and job.result_path is not None
            and job.result_path.is_file()
        )
        self.open_button.setVisible(job is not None and not is_failure)
        self.folder_button.setVisible(job is not None and not is_failure)
        self.open_button.setEnabled(result_available)
        self.folder_button.setEnabled(result_available)
        self.return_button.setVisible(is_failure and current_job_id is not None)
        self.return_button.setEnabled(is_failure and current_job_id is not None)
        self.copy_diagnostic_button.setVisible(is_failure)
        self.copy_diagnostic_button.setEnabled(is_failure)
        self.clear_button.setVisible(self._allow_clear)
        self.clear_button.setEnabled(job is not None)
        self.copy_feedback.hide()

    def _open_selected(self, *_args: object) -> None:
        job = self._selected_job()
        if (
            job is not None
            and job.status is RecentJobStatus.COMPLETED
            and job.result_path is not None
            and job.result_path.is_file()
        ):
            self.open_requested.emit(job.result_path)
        elif job is not None and job.status is RecentJobStatus.FAILED:
            self._return_to_document()

    def _open_folder(self) -> None:
        job = self._selected_job()
        if (
            job is not None
            and job.status is RecentJobStatus.COMPLETED
            and job.result_path is not None
            and job.result_path.is_file()
        ):
            self.folder_requested.emit(job.result_path.parent)

    def _return_to_document(self) -> None:
        job = self._selected_job()
        if job is None or job.status is not RecentJobStatus.FAILED:
            return
        current_job_id = self._current_job_ids.get(_source_key(job.source_path))
        if current_job_id is not None:
            self.return_to_document_requested.emit(current_job_id)

    def _copy_diagnostic(self) -> None:
        job = self._selected_job()
        if job is None or job.status is not RecentJobStatus.FAILED:
            return
        clipboard = QApplication.clipboard()
        clipboard.setText(build_failure_diagnostic(job))
        self.copy_feedback.setText("Diagnóstico copiado.")
        self.copy_feedback.show()
        self._feedback_timer.start(2500)
        self.diagnostic_copied.emit()

    @staticmethod
    def _make_selectable(widget: QLabel) -> None:
        widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)


def _summary_html(lines: tuple[str, ...]) -> str:
    """Give each result fact a quiet label/value hierarchy without adding copy."""

    blocks: list[str] = []
    for line in lines:
        label, separator, value = line.partition(":")
        if separator and value.strip():
            blocks.append(
                '<p style="margin:0 0 9px 0;">'
                f"<b>{escape(label.strip())}</b><br>{escape(value.strip())}</p>"
            )
        else:
            blocks.append(f'<p style="margin:0 0 9px 0;">{escape(line)}</p>')
    return "".join(blocks)


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

    if summary.linguistic_review_mode is not None:
        mode_text = {
            "not_reviewed": "sin revisión semántica",
            "corrected_during_translation": "corrección integrada durante la traducción",
            "independent_bilingual": "verificación bilingüe independiente",
            "targeted_bilingual": "verificación bilingüe dirigida",
        }.get(summary.linguistic_review_mode, "revisión lingüística registrada")
        coverage = (
            f"{summary.translation_checked_blocks} bloques comprobados automáticamente · "
            f"{summary.translation_reviewed_blocks} revisados semánticamente · "
            f"{summary.translation_unreviewed_blocks} sin revisión semántica"
        )
        independence = (
            f" · {summary.translation_independent_blocks} con verificación independiente"
            if summary.translation_independent_blocks
            else " · sin segunda verificación bilingüe independiente"
        )
        remaining = f" · {summary.translation_issues} " + (
            "incidencia pendiente" if summary.translation_issues == 1 else "incidencias pendientes"
        )
        lines.append(f"Confianza lingüística: {mode_text} · {coverage}{independence}{remaining}.")

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

    if summary.ai_review_recommended:
        lines.append(
            f"Revisión con IA sugerida: {summary.ai_review_signals} "
            f"{'señal' if summary.ai_review_signals == 1 else 'señales'} en "
            f"{summary.ai_review_blocks} "
            f"{'bloque' if summary.ai_review_blocks == 1 else 'bloques'}. "
            "No se ejecutó automáticamente."
        )

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


def build_failure_diagnostic(job: RecentJob) -> str:
    """Build a support-safe diagnostic with no document-derived text."""

    failure = job.failure
    phase = _failure_phase(job, failure)
    error_code = failure.error_code if failure is not None else "unknown"
    phase_token = phase.value if phase is not None else "unknown"
    lines = [
        f"Parsezen versión: {__version__}",
        f"Fase de error: {phase_token}",
        f"Código: {error_code}",
        f"Hora del evento: {_utc_time(job.finished_at)}",
    ]
    if is_safe_token(job.attempt_id or ""):
        lines.append(f"Intento: {job.attempt_id}")
    if failure is not None and failure.diagnostic_reference:
        if is_safe_token(failure.diagnostic_reference, maximum=64):
            lines.append(f"Referencia: {failure.diagnostic_reference}")
    lines.append("Recorrido:")
    for event in _deduplicated_events(job.timeline.events):
        lines.append(f"{event.phase.value} · {event.status.value} · {_utc_time(event.timestamp)}")
    return "\n".join(lines)


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


def _job_status_text(job: RecentJob) -> str:
    if job.status is RecentJobStatus.COMPLETED:
        return "Completado"
    if job.status is RecentJobStatus.CANCELLED:
        return "Cancelado"
    phase = _failure_phase(job, job.failure)
    return f"Error en {_phase_label(phase)}" if phase is not None else "Con error"


def _job_status_line(job: RecentJob) -> str:
    return f"{_job_status_text(job)} · {_local_time(job.finished_at)}"


def _failure_phase(
    job: RecentJob,
    failure: FailureSnapshot | None,
) -> AttemptPhase | None:
    if failure is not None:
        return failure.phase
    for event in reversed(_deduplicated_events(job.timeline.events)):
        if event.status is AttemptEventStatus.FAILED and event.phase not in {
            AttemptPhase.FAILURE,
            AttemptPhase.CANCELLATION,
            AttemptPhase.PAUSE,
            AttemptPhase.COMPLETION,
        }:
            return event.phase
    return None


def _timeline_display(timeline: object) -> str:
    events = getattr(timeline, "events", ())
    if not isinstance(events, tuple):
        return "No hay recorrido disponible para esta operación."
    display = [
        f"{_phase_label(event.phase).capitalize()} · "
        f"{_EVENT_STATUS_LABELS[event.status]} · {_local_time(event.timestamp)}"
        for event in _deduplicated_events(events)
    ]
    return "\n".join(display) if display else "No hay recorrido disponible para esta operación."


def _deduplicated_events(events: tuple[AttemptEvent, ...]) -> tuple[AttemptEvent, ...]:
    """Keep display and support output stable when old journals repeat a transition."""

    seen: set[tuple[AttemptPhase, AttemptEventStatus, datetime]] = set()
    result: list[AttemptEvent] = []
    for event in sorted(events, key=lambda candidate: candidate.timestamp):
        key = (event.phase, event.status, event.timestamp)
        if key in seen:
            continue
        seen.add(key)
        result.append(event)
    return tuple(result)


def _phase_label(phase: AttemptPhase) -> str:
    return _PHASE_LABELS.get(phase, "fase desconocida")


def _local_time(value: datetime) -> str:
    return value.astimezone().strftime("%d/%m/%Y · %H:%M")


def _utc_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _source_key(path: Path) -> str:
    return str(path.resolve(strict=False)).casefold()
