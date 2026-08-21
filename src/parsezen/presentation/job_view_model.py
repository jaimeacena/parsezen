"""Pure presentation projections for the Parsezen document queue."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from parsezen.domain.jobs import DocumentFormat, DocumentJob, JobStatus
from parsezen.domain.stages import StageKind, StageState, StageStatus


class JobAction(StrEnum):
    CONFIGURE = "configure"
    REVIEW = "review"
    OPEN_RESULT = "open_result"
    SHOW_ERROR = "show_error"
    REVIEW_WITH_AI = "review_with_ai"


@dataclass(frozen=True, slots=True)
class NextStepView:
    label: str
    tone: str
    action: JobAction | None = None
    action_label: str | None = None
    stage: StageKind | None = None
    progress: float | None = None


@dataclass(frozen=True, slots=True)
class QueueHeaderView:
    summary: str
    total: int
    review_count: int = 0
    primary_mode: str | None = None
    primary_label: str | None = None


_STAGE_ACTIVITY = {
    StageKind.PREPARE: "Preparando",
    StageKind.TRANSLATE: "Traduciendo",
    StageKind.REFINE: "Corrigiendo",
    StageKind.STRUCTURE: "Personalizando",
    StageKind.PUBLISH: "Publicando",
}


def focus_stage(job: DocumentJob) -> StageState:
    """Return the single stage that explains the job's current presentation."""

    participating = tuple(stage for stage in job.stages if stage.participates)
    for status in (
        StageStatus.FAILED,
        StageStatus.BLOCKED_FOR_REVIEW,
        StageStatus.RUNNING,
        StageStatus.PAUSED,
        StageStatus.CANCELLED,
        StageStatus.INVALIDATED,
    ):
        match = next((stage for stage in participating if stage.status is status), None)
        if match is not None:
            return match
    pending = next(
        (stage for stage in participating if stage.status is not StageStatus.COMPLETED),
        None,
    )
    return pending or participating[-1]


def next_step_view(job: DocumentJob) -> NextStepView:
    """Map domain state to one stable, actionable next-step presentation."""

    if not job.is_configured:
        return NextStepView(
            "Pendiente",
            "pending",
            JobAction.CONFIGURE,
            "Configurar",
            StageKind.PUBLISH,
        )
    if job.status is JobStatus.COMPLETED:
        if job.review_recommendation is not None:
            return NextStepView(
                "Revisión sugerida",
                "review",
                JobAction.REVIEW_WITH_AI,
                "Revisar con IA",
                StageKind.REFINE,
            )
        return NextStepView(
            "Listo",
            "completed",
            JobAction.OPEN_RESULT,
            "Abrir resultado",
            StageKind.PUBLISH,
        )

    stage = focus_stage(job)
    if stage.status is StageStatus.BLOCKED_FOR_REVIEW:
        action_label = (
            "Revisar y publicar"
            if job.configuration.output.format is DocumentFormat.EPUB
            else "Revisar cambios"
        )
        return NextStepView(
            "Necesita tu revisión",
            "review",
            JobAction.REVIEW,
            action_label,
            stage.kind,
        )
    if stage.status is StageStatus.RUNNING:
        progress = stage.progress_ratio
        label = _STAGE_ACTIVITY[stage.kind]
        if progress is not None:
            label = f"{label} · {round(progress * 100)} %"
        elif stage.progress_message and stage.kind is not StageKind.PREPARE:
            label = stage.progress_message
        return NextStepView(label, "running", stage=stage.kind, progress=progress)
    if stage.status is StageStatus.FAILED:
        return NextStepView(
            "Fallido",
            "error",
            JobAction.SHOW_ERROR,
            "Ver error",
            stage.kind,
        )
    if stage.status is StageStatus.PAUSED:
        return NextStepView("Pausado", "warning", stage=stage.kind)
    if stage.status is StageStatus.CANCELLED:
        return NextStepView("Cancelado", "warning", stage=stage.kind)
    if stage.status is StageStatus.INVALIDATED:
        return NextStepView("Debe repetirse", "warning", stage=stage.kind)
    if stage.status is StageStatus.READY:
        return NextStepView("Listo para procesar", "pending", stage=stage.kind)
    return NextStepView("En cola", "pending", stage=stage.kind)


def queue_header_view(jobs: tuple[DocumentJob, ...]) -> QueueHeaderView:
    """Derive the header summary and its only contextual primary action."""

    total = len(jobs)
    document_word = "documento" if total == 1 else "documentos"
    reviews = tuple(job for job in jobs if next_step_view(job).action is JobAction.REVIEW)
    completed = sum(job.status is JobStatus.COMPLETED for job in jobs)
    failed = sum(job.status is JobStatus.FAILED for job in jobs)
    summary_parts = [f"{total} {document_word}"]
    if completed:
        summary_parts.append(f"{completed} {'listo' if completed == 1 else 'listos'}")
    if reviews:
        summary_parts.append(f"{len(reviews)} por revisar")
    if failed:
        summary_parts.append(f"{failed} con error")
    summary = " · ".join(summary_parts)

    running = any(job.status is JobStatus.RUNNING for job in jobs)
    if running:
        return QueueHeaderView(summary, total, len(reviews), "pause", "Pausar")
    if reviews:
        pending_word = "pendiente" if len(reviews) == 1 else "pendientes"
        return QueueHeaderView(
            summary,
            total,
            len(reviews),
            "review",
            f"Revisar {len(reviews)} {pending_word}",
        )

    runnable = tuple(
        job
        for job in jobs
        if job.is_configured
        and job.status in {JobStatus.QUEUED, JobStatus.PAUSED, JobStatus.FAILED}
    )
    if runnable:
        runnable_word = "documento" if len(runnable) == 1 else "documentos"
        return QueueHeaderView(
            summary,
            total,
            0,
            "process",
            f"Procesar {len(runnable)} {runnable_word}",
        )
    return QueueHeaderView(summary, total, len(reviews))
