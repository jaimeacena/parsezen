"""Pure compatibility and validation rules for one document configuration."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from parsezen.component_catalog import REVIEW_COMPONENT_MANIFEST, TRANSLATION_COMPONENT_MANIFEST
from parsezen.domain.jobs import (
    CoverStrategy,
    DocumentFormat,
    DocumentSource,
    JobConfiguration,
    ProcessingPlan,
    TranslationMethod,
)
from parsezen.local_ai_policy import ComponentCapability, ComponentManifest

# Parsezen deliberately publishes only its two canonical, reviewable formats.
SUPPORTED_OUTPUTS: dict[DocumentFormat, frozenset[DocumentFormat]] = {
    source: frozenset({DocumentFormat.MARKDOWN, DocumentFormat.EPUB}) for source in DocumentFormat
}


class ConfigurationSection(StrEnum):
    RESULT = "result"
    TRANSLATION = "translation"
    PLAN = "plan"
    PERSONALIZATION = "personalization"
    AI = "ai"


@dataclass(frozen=True, slots=True)
class ConfigurationIssue:
    section: ConfigurationSection
    message: str


def requires_ai(configuration: JobConfiguration) -> bool:
    """Whether the selected plan or translation engine needs global Ollama."""

    return configuration.plan is ProcessingPlan.LOCAL_AI_REVIEWED or (
        configuration.translation.enabled
        and configuration.translation.method is TranslationMethod.LOCAL_AI
    )


def configuration_issues(
    source: DocumentSource,
    configuration: JobConfiguration,
    *,
    allow_noop_same_format: bool = False,
) -> tuple[ConfigurationIssue, ...]:
    """Return all user-actionable problems without depending on Qt or the runtime."""

    output = configuration.output
    issues: list[ConfigurationIssue] = []
    if not output.configured:
        return (
            ConfigurationIssue(
                ConfigurationSection.RESULT,
                "Configura el resultado antes de procesar este documento.",
            ),
        )
    if output.format not in SUPPORTED_OUTPUTS[source.format]:
        issues.append(
            ConfigurationIssue(
                ConfigurationSection.RESULT,
                "Selecciona Markdown o EPUB como formato de salida.",
            )
        )
    if configuration.page_range is not None and source.format is not DocumentFormat.PDF:
        issues.append(
            ConfigurationIssue(
                ConfigurationSection.RESULT,
                "Solo los documentos PDF permiten elegir un intervalo de páginas.",
            )
        )
    if configuration.translation.enabled and not configuration.translation.target_language:
        issues.append(
            ConfigurationIssue(
                ConfigurationSection.TRANSLATION,
                "Elige el idioma al que quieres traducir.",
            )
        )
    translation_ai = (
        configuration.translation.enabled
        and configuration.translation.method is TranslationMethod.LOCAL_AI
    )
    review_ai = configuration.plan is ProcessingPlan.LOCAL_AI_REVIEWED
    if translation_ai and configuration.ai.effective_translation_model is None:
        issues.append(
            ConfigurationIssue(
                ConfigurationSection.AI,
                "Prepara el componente de traducción local antes de traducir con IA local.",
            )
        )
    if review_ai and configuration.ai.effective_review_model is None:
        issues.append(
            ConfigurationIssue(
                ConfigurationSection.AI,
                "Prepara el componente de revisión local antes de usar la revisión semántica.",
            )
        )
    for required, capability, component, manifest in (
        (
            translation_ai,
            ComponentCapability.TRANSLATION,
            configuration.ai.components.translation,
            TRANSLATION_COMPONENT_MANIFEST,
        ),
        (
            review_ai,
            ComponentCapability.REVIEW,
            configuration.ai.components.review,
            REVIEW_COMPONENT_MANIFEST,
        ),
    ):
        if (
            required
            and component is not None
            and not _matches_product_manifest(component, manifest)
        ):
            issues.append(
                ConfigurationIssue(
                    ConfigurationSection.AI,
                    f"La identidad del componente de {capability.value} ya no es válida; "
                    "actualiza sus estados.",
                )
            )
    if (
        not allow_noop_same_format
        and source.format is output.format
        and source.format is not DocumentFormat.EPUB
        and not (
            configuration.translation.enabled
            or configuration.plan is ProcessingPlan.LOCAL_AI_REVIEWED
        )
    ):
        issues.append(
            ConfigurationIssue(
                ConfigurationSection.RESULT,
                "Usa el plan revisado o activa la traducción para volver a generar el formato.",
            )
        )
    if (
        output.format is DocumentFormat.EPUB
        and output.cover_strategy is CoverStrategy.CUSTOM
        and output.cover_path is None
    ):
        issues.append(
            ConfigurationIssue(
                ConfigurationSection.PERSONALIZATION,
                "Elige la imagen que se usará como portada.",
            )
        )
    return tuple(issues)


def _matches_product_manifest(component: object, manifest: ComponentManifest) -> bool:
    return (
        getattr(component, "policy_version", None) == manifest.policy_version
        and getattr(component, "model", None) == manifest.model_name
        and getattr(component, "digest", None) == manifest.ollama_digest
        and getattr(component, "context_window", None) == manifest.context_window
    )
