"""Neutral policy for classifying failures into safe recovery actions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from parsezen.domain.stages import StageKind
from parsezen.errors import (
    ConversionError,
    EarlyCheckError,
    FinalIntegrityError,
    ImprovementError,
    LocalModelUnavailableError,
    OutputWriteError,
    ParsezenError,
    RequestValidationError,
    TranslationError,
    UnexpectedProcessingError,
)


class FailureKind(StrEnum):
    LOCAL_AI = "local_ai"
    OUTPUT = "output"
    INTEGRITY = "integrity"
    OCR = "ocr"
    CONFIGURATION = "configuration"
    TRANSFORMATION = "transformation"
    SOURCE = "source"
    EARLY_CHECK = "early_check"
    UNEXPECTED = "unexpected"


class RecoveryAction(StrEnum):
    RETRY = "retry"
    CONFIGURE = "configure"
    LOCAL_AI = "local_ai"


@dataclass(frozen=True, slots=True)
class ProcessingFailure:
    """Ephemeral failure details passed from a worker to the current UI session."""

    kind: FailureKind
    message: str
    error_type: str

    @classmethod
    def from_exception(cls, error: Exception) -> ProcessingFailure:
        kind = classify_failure(error)
        message = (
            str(error)
            if isinstance(error, ParsezenError)
            else "Se produjo un error inesperado durante el procesamiento."
        )
        return cls(
            kind,
            message,
            type(error).__name__,
        )

    @classmethod
    def from_saved(
        cls,
        error_code: str | None,
        message: str,
        *,
        stage: StageKind,
    ) -> ProcessingFailure:
        try:
            kind = (
                FailureKind(error_code)
                if error_code is not None
                else classify_saved_failure(
                    message,
                    stage,
                )
            )
        except (TypeError, ValueError):
            kind = classify_saved_failure(message, stage)
        return cls(kind, message, "SavedFailure")

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    """One explanation and the smallest useful set of recovery actions."""

    title: str
    explanation: str
    preserved_work: str
    primary_action: RecoveryAction
    primary_label: str
    secondary_action: RecoveryAction | None = None
    secondary_label: str | None = None


def classify_failure(error: Exception) -> FailureKind:
    if isinstance(error, EarlyCheckError):
        return FailureKind.EARLY_CHECK
    if isinstance(error, FinalIntegrityError):
        return FailureKind.INTEGRITY
    if isinstance(error, LocalModelUnavailableError):
        return FailureKind.LOCAL_AI
    if isinstance(error, OutputWriteError):
        return FailureKind.OUTPUT
    if isinstance(error, RequestValidationError):
        return FailureKind.CONFIGURATION
    if isinstance(error, (TranslationError, ImprovementError)):
        return FailureKind.TRANSFORMATION
    if isinstance(error, ConversionError):
        return FailureKind.OCR if "ocr" in str(error).casefold() else FailureKind.SOURCE
    if isinstance(error, UnexpectedProcessingError) or not isinstance(error, ParsezenError):
        return FailureKind.UNEXPECTED
    return FailureKind.TRANSFORMATION


def classify_saved_failure(message: str, stage: StageKind) -> FailureKind:
    normalized = message.casefold()
    if "control final" in normalized or "integridad" in normalized:
        return FailureKind.INTEGRITY
    if any(value in normalized for value in ("ollama", "modelo", "ia local")):
        return FailureKind.LOCAL_AI
    if any(value in normalized for value in ("guardar", "escribir", "destino", "espacio")):
        return FailureKind.OUTPUT
    if "ocr" in normalized:
        return FailureKind.OCR
    if any(value in normalized for value in ("rango", "página seleccionada", "configur")):
        return FailureKind.CONFIGURATION
    if "comprobación temprana" in normalized or "páginas representativas" in normalized:
        return FailureKind.EARLY_CHECK
    if stage in {StageKind.TRANSLATE, StageKind.REFINE, StageKind.STRUCTURE}:
        return FailureKind.TRANSFORMATION
    if stage is StageKind.PUBLISH:
        return FailureKind.OUTPUT
    return FailureKind.SOURCE


def recovery_plan(
    failure: ProcessingFailure,
    *,
    stage: StageKind,
) -> RecoveryPlan:
    preserved = _preserved_work(stage)
    if failure.kind is FailureKind.LOCAL_AI:
        return RecoveryPlan(
            "La IA local necesita atención",
            failure.message,
            preserved,
            RecoveryAction.LOCAL_AI,
            "Abrir IA local",
            RecoveryAction.RETRY,
            "Reintentar",
        )
    if failure.kind is FailureKind.EARLY_CHECK:
        return RecoveryPlan(
            "La muestra necesita atención",
            failure.message,
            "El recorrido completo no llegó a comenzar. La extracción y el OCR ya comprobados "
            "se reutilizarán al reintentar.",
            RecoveryAction.CONFIGURE,
            "Revisar flujo",
            RecoveryAction.RETRY,
            "Comprobar de nuevo",
        )
    if failure.kind is FailureKind.INTEGRITY:
        return RecoveryPlan(
            "El control final detuvo la publicación",
            failure.message,
            "No se sustituyó el resultado definitivo. El borrador y las decisiones "
            "aprobadas siguen disponibles.",
            RecoveryAction.RETRY,
            "Reintentar esta fase",
            RecoveryAction.CONFIGURE,
            "Revisar configuración",
        )
    if failure.kind is FailureKind.OUTPUT:
        return RecoveryPlan(
            "No se pudo guardar el resultado",
            failure.message,
            preserved,
            RecoveryAction.CONFIGURE,
            "Revisar destino",
            RecoveryAction.RETRY,
            "Reintentar",
        )
    if failure.kind is FailureKind.OCR:
        return RecoveryPlan(
            "El OCR no pudo completar la preparación",
            failure.message,
            preserved,
            RecoveryAction.CONFIGURE,
            "Revisar OCR e intervalo",
            RecoveryAction.RETRY,
            "Reintentar",
        )
    if failure.kind is FailureKind.CONFIGURATION:
        return RecoveryPlan(
            "La configuración necesita un ajuste",
            failure.message,
            preserved,
            RecoveryAction.CONFIGURE,
            "Revisar configuración",
            RecoveryAction.RETRY,
            "Reintentar",
        )
    if failure.kind is FailureKind.TRANSFORMATION:
        return RecoveryPlan(
            "La transformación no pudo completarse",
            failure.message,
            preserved,
            RecoveryAction.RETRY,
            "Reintentar esta fase",
            RecoveryAction.CONFIGURE,
            "Revisar configuración",
        )
    if failure.kind is FailureKind.SOURCE:
        return RecoveryPlan(
            "No se pudo preparar el documento",
            failure.message,
            preserved,
            RecoveryAction.CONFIGURE,
            "Revisar configuración",
            RecoveryAction.RETRY,
            "Reintentar",
        )
    return RecoveryPlan(
        "El procesamiento se interrumpió",
        failure.message,
        preserved,
        RecoveryAction.RETRY,
        "Reintentar esta fase",
        RecoveryAction.CONFIGURE,
        "Revisar configuración",
    )


def _preserved_work(stage: StageKind) -> str:
    if stage is StageKind.PREPARE:
        return "Los checkpoints válidos ya creados se reutilizarán al reintentar."
    return (
        "Las fases anteriores, sus checkpoints y las decisiones aprobadas permanecen conservadas."
    )
