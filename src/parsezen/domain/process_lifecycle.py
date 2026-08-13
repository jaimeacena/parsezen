"""Declarative mapping from physical stages to stable lifecycle vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from parsezen.domain.attempt_activity import AttemptPhase
from parsezen.domain.stages import StageKind


class ProcessStage(StrEnum):
    """Observable stages backed by real processor work."""

    VALIDATING = "validating"
    READING = "reading"
    CONVERTING = "converting"
    OCR = "ocr"
    PRESERVING_IMAGES = "preserving_images"
    STRUCTURING = "structuring"
    PREPARING_TRANSLATION = "preparing_translation"
    IMPROVING = "improving"
    REVIEWING_CONTENT = "reviewing_content"
    ORGANIZING_STRUCTURE = "organizing_structure"
    TRANSLATING = "translating"
    BUILDING_EPUB = "building_epub"
    WRITING = "writing"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class ProcessStageLifecycle:
    """Stable domain, activity and diagnostic projection for one physical stage."""

    domain_stage: StageKind
    attempt_phase: AttemptPhase
    diagnostic_label: str


PROCESS_STAGE_LIFECYCLE = {
    ProcessStage.VALIDATING: ProcessStageLifecycle(
        StageKind.PREPARE, AttemptPhase.PREPARATION, "validación"
    ),
    ProcessStage.READING: ProcessStageLifecycle(
        StageKind.PREPARE, AttemptPhase.PREPARATION, "lectura"
    ),
    ProcessStage.CONVERTING: ProcessStageLifecycle(
        StageKind.PREPARE, AttemptPhase.PREPARATION, "conversión"
    ),
    ProcessStage.OCR: ProcessStageLifecycle(StageKind.PREPARE, AttemptPhase.PREPARATION, "OCR"),
    ProcessStage.PRESERVING_IMAGES: ProcessStageLifecycle(
        StageKind.PREPARE, AttemptPhase.PREPARATION, "imágenes"
    ),
    ProcessStage.STRUCTURING: ProcessStageLifecycle(
        StageKind.PREPARE, AttemptPhase.PREPARATION, "estructura inicial"
    ),
    ProcessStage.PREPARING_TRANSLATION: ProcessStageLifecycle(
        StageKind.TRANSLATE, AttemptPhase.TRANSLATION, "preparación de traducción"
    ),
    ProcessStage.TRANSLATING: ProcessStageLifecycle(
        StageKind.TRANSLATE, AttemptPhase.TRANSLATION, "traducción"
    ),
    ProcessStage.IMPROVING: ProcessStageLifecycle(
        StageKind.REFINE, AttemptPhase.CORRECTION, "traducción o mejora"
    ),
    ProcessStage.REVIEWING_CONTENT: ProcessStageLifecycle(
        StageKind.REFINE, AttemptPhase.CORRECTION, "revisión de contenido"
    ),
    ProcessStage.ORGANIZING_STRUCTURE: ProcessStageLifecycle(
        StageKind.STRUCTURE, AttemptPhase.PERSONALIZATION, "personalización"
    ),
    ProcessStage.BUILDING_EPUB: ProcessStageLifecycle(
        StageKind.PUBLISH, AttemptPhase.PUBLICATION, "creación del EPUB"
    ),
    ProcessStage.WRITING: ProcessStageLifecycle(
        StageKind.PUBLISH, AttemptPhase.PUBLICATION, "guardado"
    ),
    ProcessStage.COMPLETED: ProcessStageLifecycle(
        StageKind.PUBLISH, AttemptPhase.COMPLETION, "finalización"
    ),
}


def lifecycle_for_process_stage(stage: ProcessStage | None) -> ProcessStageLifecycle:
    """Return the complete stable mapping, defaulting an absent stage to preparation."""

    return PROCESS_STAGE_LIFECYCLE[stage or ProcessStage.VALIDATING]


def stage_kind_from_process_stage(stage: ProcessStage | None) -> StageKind:
    return lifecycle_for_process_stage(stage).domain_stage


def phase_for_process_stage(stage: ProcessStage | None) -> AttemptPhase:
    return lifecycle_for_process_stage(stage).attempt_phase


def diagnostic_label_for_process_stage(value: str | None) -> str | None:
    if value == "not_started":
        return "inicio"
    try:
        stage = ProcessStage(value) if value is not None else None
    except ValueError:
        return None
    return lifecycle_for_process_stage(stage).diagnostic_label if stage is not None else None
