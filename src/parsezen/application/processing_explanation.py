"""Explain the effective document route without exposing processor internals."""

from __future__ import annotations

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


def processing_flow_steps(
    source_format: DocumentFormat,
    configuration: JobConfiguration,
) -> tuple[str, ...]:
    """Return the real user-visible transformations in execution order."""

    reviewed = configuration.plan is ProcessingPlan.LOCAL_AI_REVIEWED
    translation = configuration.translation
    output_format = configuration.output.format
    steps = [_FORMAT_LABELS[source_format]]

    if translation.enabled:
        target = _display_language(translation.target_language)
        suffix = f" a {target}" if target else ""
        if translation.method is TranslationMethod.OFFLINE:
            steps.append(f"Traducir con Argos{suffix}")
            if reviewed:
                steps.append("Revisión bilingüe con IA local")
        elif reviewed:
            steps.append(f"Traducir y corregir con IA local{suffix}")
        else:
            steps.append(f"Traducir con IA local{suffix}")
    elif reviewed:
        steps.append("Revisión semántica con IA local")

    if reviewed and output_format is DocumentFormat.EPUB:
        steps.append("Revisión de estructura con IA local")
    elif (
        source_format is DocumentFormat.EPUB
        and output_format is DocumentFormat.EPUB
        and len(steps) == 1
    ):
        steps.append("Personalizar")

    steps.append(_FORMAT_LABELS[output_format])
    return tuple(steps)


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
