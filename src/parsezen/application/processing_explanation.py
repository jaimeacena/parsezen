"""Explain the effective document route without exposing processor internals."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from parsezen.domain.jobs import (
    DocumentFormat,
    JobConfiguration,
    ProcessingPlan,
    TranslationMethod,
)
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
    human_review: HumanReviewPolicy

    @property
    def human_review_note(self) -> str | None:
        return {
            HumanReviewPolicy.NONE: None,
            HumanReviewPolicy.IF_CHANGES: "Tu revisión si hay cambios",
            HumanReviewPolicy.BEFORE_PUBLISHING: "Tu revisión antes de publicar",
        }[self.human_review]


def processing_flow(
    source_format: DocumentFormat,
    configuration: JobConfiguration,
) -> ProcessingFlow:
    """Describe automatic actions separately from the later human decision."""

    reviewed = configuration.plan is ProcessingPlan.LOCAL_AI_REVIEWED
    translation = configuration.translation
    output_format = configuration.output.format
    compact_steps: list[str] = []
    detailed_steps: list[str] = []

    def add_step(compact: str, detailed: str | None = None) -> None:
        compact_steps.append(compact)
        detailed_steps.append(detailed or compact)

    if source_format is DocumentFormat.PDF and configuration.force_pdf_ocr:
        add_step("OCR")

    automatic_transformations = 0
    if translation.enabled:
        target = _display_language(translation.target_language)
        suffix = f" a {target}" if target else ""
        if translation.method is TranslationMethod.OFFLINE:
            add_step(
                f"Traducir{suffix}",
                f"Traducir con Argos{suffix}",
            )
            automatic_transformations += 1
            if reviewed:
                add_step(
                    "Verificar traducción",
                    "Verificar traducción con IA local",
                )
                automatic_transformations += 1
        elif reviewed:
            add_step(
                f"Traducir y corregir{suffix}",
                f"Traducir y corregir con IA local{suffix}",
            )
            automatic_transformations += 1
        else:
            add_step(
                f"Traducir{suffix}",
                f"Traducir con IA local{suffix}",
            )
            automatic_transformations += 1
    elif reviewed:
        add_step("Corregir contenido", "Corregir contenido con IA local")
        automatic_transformations += 1

    if reviewed and output_format is DocumentFormat.EPUB:
        add_step("Organizar EPUB", "Organizar EPUB con IA local")
        automatic_transformations += 1
    elif (
        source_format is DocumentFormat.EPUB
        and output_format is DocumentFormat.EPUB
        and automatic_transformations == 0
    ):
        add_step("Personalizar EPUB")
        automatic_transformations += 1

    if automatic_transformations == 0:
        add_step("Convertir")

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


def processing_pass_summary(configuration: JobConfiguration) -> str:
    """Explain material text passes and the review consequence of one configuration."""

    reviewed = configuration.plan is ProcessingPlan.LOCAL_AI_REVIEWED
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
            "2 pasadas de IA: traducción y corrección combinadas, y después estructura. "
            "La corrección textual queda integrada en la traducción."
        )
    return (
        "1 pasada principal de IA que combina traducción y corrección. "
        "La corrección queda integrada en la traducción."
    )


def translation_route_summary(
    method: TranslationMethod,
    *,
    reviewed: bool,
    epub: bool,
) -> str:
    """Give configuration UI a short, truthful route and qualitative cost preview."""

    if method is TranslationMethod.OFFLINE:
        if reviewed and epub:
            return (
                "Coste aproximado alto · Argos traduce; la IA hace una verificación bilingüe "
                "independiente y después planifica la estructura (3 pasadas)."
            )
        if reviewed:
            return (
                "Coste aproximado alto · Argos traduce y la IA realiza después una verificación "
                "bilingüe independiente (2 pasadas)."
            )
        return (
            "Coste aproximado bajo · Argos traduce sin Ollama (1 pasada); hay comprobaciones "
            "automáticas, pero no revisión semántica."
        )
    if reviewed and epub:
        return (
            "Coste aproximado alto · la IA traduce y corrige en la misma pasada; después planifica "
            "la estructura. No hay una segunda verificación bilingüe (2 pasadas)."
        )
    if reviewed:
        return (
            "Coste aproximado alto · la IA traduce y corrige en una sola pasada. La corrección "
            "no es una verificación bilingüe independiente."
        )
    return (
        "Coste aproximado medio · la IA traduce en una pasada; hay comprobaciones automáticas, "
        "pero no una revisión semántica posterior."
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
