"""Explain the effective document route without exposing processor internals."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from parsezen.domain.execution_plan import ExecutionStep, compile_execution_plan
from parsezen.domain.jobs import (
    DocumentFormat,
    DocumentSource,
    JobConfiguration,
    TranslationMethod,
)
from parsezen.domain.stages import StageKind
from parsezen.translation_quality import (
    TARGET_LANGUAGE_CODES,
    LinguisticReviewCoverage,
    LinguisticReviewMode,
)

_FORMAT_LABELS = {
    DocumentFormat.TEXT: "TXT",
    DocumentFormat.MARKDOWN: "Markdown",
    DocumentFormat.DOCX: "DOCX",
    DocumentFormat.PDF: "PDF",
    DocumentFormat.EPUB: "EPUB",
}


class HumanReviewPolicy(StrEnum):
    """Expected user involvement after the automatic document route."""

    NONE = "none"
    IF_CHANGES = "if_changes"
    BEFORE_PUBLISHING = "before_publishing"


@dataclass(frozen=True, slots=True)
class ProcessingFlow:
    """One shared projection for compact and expanded workflow explanations."""

    compact_steps: tuple[str, ...]
    detailed_steps: tuple[str, ...]
    step_stages: tuple[tuple[StageKind, ...], ...]
    human_review: HumanReviewPolicy

    @property
    def human_review_step(self) -> str | None:
        return {
            HumanReviewPolicy.NONE: None,
            HumanReviewPolicy.IF_CHANGES: "Tu revisión si hay cambios",
            HumanReviewPolicy.BEFORE_PUBLISHING: "Tu revisión final",
        }[self.human_review]

    @property
    def compact_sequence(self) -> tuple[str, ...]:
        """Return the complete route, including the user's decision when required."""

        return self.compact_steps + ((self.human_review_step,) if self.human_review_step else ())

    @property
    def detailed_sequence(self) -> tuple[str, ...]:
        """Return the accessible route with the same semantics as the compact one."""

        return self.detailed_steps + ((self.human_review_step,) if self.human_review_step else ())

    @property
    def sequence_stages(self) -> tuple[tuple[StageKind, ...], ...]:
        """Map every visible step to its real execution phases."""

        return self.step_stages + (((),) if self.human_review_step else ())


def processing_flow(
    source_format: DocumentFormat,
    configuration: JobConfiguration,
) -> ProcessingFlow:
    """Describe the automatic route and any final human decision."""

    plan = compile_execution_plan(
        DocumentSource(Path("document"), source_format, 0, 0),
        configuration,
    )
    reviewed = plan.includes(ExecutionStep.REVIEW_CONTENT)
    translation = configuration.translation
    output_format = configuration.output.format
    compact_steps: list[str] = []
    detailed_steps: list[str] = []
    step_stages: list[tuple[StageKind, ...]] = []

    def add_step(
        compact: str,
        detailed: str | None = None,
        *,
        stages: tuple[StageKind, ...],
    ) -> None:
        compact_steps.append(compact)
        detailed_steps.append(detailed or compact)
        step_stages.append(stages)

    if source_format is DocumentFormat.PDF and configuration.force_pdf_ocr:
        add_step("OCR", stages=(StageKind.PREPARE,))

    automatic_transformations = 0
    if plan.translates:
        target = _display_language(translation.target_language)
        suffix = f" a {target}" if target else ""
        if plan.includes(ExecutionStep.TRANSLATE_OFFLINE):
            add_step(
                f"Traducir{suffix}",
                f"Traducir con Argos{suffix}",
                stages=(StageKind.TRANSLATE,),
            )
            automatic_transformations += 1
            if reviewed:
                add_step(
                    "Verificar traducción",
                    "Verificar traducción con IA local",
                    stages=(StageKind.REFINE,),
                )
                automatic_transformations += 1
        else:
            add_step(
                f"Traducir{suffix}",
                f"Traducir con IA local{suffix}",
                stages=(StageKind.TRANSLATE,),
            )
            automatic_transformations += 1
            if reviewed:
                add_step(
                    "Verificar traducción",
                    "Verificar traducción con IA local",
                    stages=(StageKind.REFINE,),
                )
                automatic_transformations += 1
    elif plan.includes(ExecutionStep.REVIEW_CONTENT):
        add_step(
            "Corregir contenido",
            "Corregir contenido con IA local",
            stages=(StageKind.REFINE,),
        )
        automatic_transformations += 1

    if plan.includes(ExecutionStep.REVIEW_STRUCTURE):
        add_step(
            "Organizar EPUB",
            "Organizar EPUB con IA local",
            stages=(StageKind.STRUCTURE,),
        )
        automatic_transformations += 1
    elif plan.includes(ExecutionStep.PRESERVE_EPUB) and automatic_transformations == 0:
        add_step("Personalizar EPUB", stages=(StageKind.STRUCTURE,))
        automatic_transformations += 1

    if automatic_transformations == 0:
        convert_stages = (
            (StageKind.PUBLISH,)
            if compact_steps and compact_steps[-1] == "OCR"
            else (StageKind.PREPARE, StageKind.PUBLISH)
        )
        add_step("Convertir", stages=convert_stages)

    human_review = (
        HumanReviewPolicy.BEFORE_PUBLISHING
        if output_format is DocumentFormat.EPUB
        else HumanReviewPolicy.IF_CHANGES
        if reviewed
        else HumanReviewPolicy.NONE
    )
    return ProcessingFlow(
        tuple(compact_steps),
        tuple(detailed_steps),
        tuple(step_stages),
        human_review,
    )


def processing_flow_steps(
    source_format: DocumentFormat,
    configuration: JobConfiguration,
) -> tuple[str, ...]:
    """Return the real user-visible transformations in execution order."""

    flow = processing_flow(source_format, configuration)
    return (
        _FORMAT_LABELS[source_format],
        *flow.detailed_steps,
        _FORMAT_LABELS[configuration.output.format],
    )


def processing_pass_summary(
    configuration: JobConfiguration,
    source_format: DocumentFormat = DocumentFormat.TEXT,
) -> str:
    """Explain material text passes and the review consequence of one configuration."""

    plan = compile_execution_plan(
        DocumentSource(Path("document"), source_format, 0, 0),
        configuration,
    )
    reviewed = plan.includes(ExecutionStep.REVIEW_CONTENT)
    translation = configuration.translation
    epub = configuration.output.format is DocumentFormat.EPUB

    if not translation.enabled:
        if not reviewed:
            return (
                "Sin pasadas de IA; las comprobaciones automáticas pueden sugerir después "
                "una revisión dirigida, pero nunca la ejecutan solas."
            )
        if epub:
            return (
                "2 pasadas de IA: revisión semántica y estructura; "
                "las propuestas se confirman antes de publicar."
            )
        return (
            "1 pasada de IA para la revisión semántica; "
            "las propuestas se confirman antes de publicar."
        )

    if translation.method is TranslationMethod.OFFLINE:
        if not reviewed:
            return (
                "1 pasada de traducción con Argos; no usa Ollama. Las comprobaciones pueden "
                "sugerir después una revisión dirigida."
            )
        if epub:
            return (
                "3 pasadas: Argos, revisión bilingüe y estructura; las dos últimas usan IA local."
            )
        return (
            "2 pasadas: Argos y revisión bilingüe con IA local; "
            "las correcciones quedan como propuestas."
        )

    if not reviewed:
        return (
            "1 pasada de IA para traducir; no añade una revisión semántica posterior. "
            "Las comprobaciones pueden sugerir una revisión dirigida."
        )
    if epub:
        return (
            "3 pasadas de IA: traducción, verificación bilingüe y estructura. "
            "Las correcciones quedan como propuestas para tu revisión final."
        )
    return (
        "2 pasadas de IA: traducción y verificación bilingüe. "
        "Las correcciones quedan como propuestas."
    )


def linguistic_review_summary(coverage: LinguisticReviewCoverage | None) -> str | None:
    """Explain completed language coverage without overstating automatic diagnostics."""

    if coverage is None:
        return None
    mode = {
        LinguisticReviewMode.NOT_REVIEWED: "Sin revisión semántica posterior.",
        LinguisticReviewMode.CORRECTED_DURING_TRANSLATION: (
            "La corrección se hizo durante la traducción; no fue una segunda verificación."
        ),
        LinguisticReviewMode.INDEPENDENT_BILINGUAL: (
            "La traducción recibió una verificación bilingüe independiente."
        ),
        LinguisticReviewMode.TARGETED_BILINGUAL: (
            "La verificación bilingüe independiente se limitó a bloques con señales concretas."
        ),
    }[coverage.mode]
    return (
        f"{mode} {coverage.automatically_checked_blocks} bloques comprobados, "
        f"{coverage.semantically_reviewed_blocks} revisados semánticamente, "
        f"{coverage.semantically_unreviewed_blocks} sin revisión semántica y "
        f"{coverage.remaining_issues} incidencias pendientes."
    )


def _display_language(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    normalized = value.strip()
    for display_name, code in TARGET_LANGUAGE_CODES.items():
        if normalized.casefold() in {display_name.casefold(), code.casefold()}:
            return display_name.casefold()
    return normalized
