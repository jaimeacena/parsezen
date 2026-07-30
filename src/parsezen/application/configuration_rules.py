"""Pure compatibility and validation rules for one document configuration."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from parsezen.domain.jobs import (
    CoverStrategy,
    DocumentFormat,
    DocumentSource,
    JobConfiguration,
    TranslationMethod,
    effective_ai_profile,
)

SUPPORTED_OUTPUTS: dict[DocumentFormat, frozenset[DocumentFormat]] = {
    DocumentFormat.TEXT: frozenset(
        {DocumentFormat.TEXT, DocumentFormat.MARKDOWN, DocumentFormat.EPUB}
    ),
    DocumentFormat.MARKDOWN: frozenset({DocumentFormat.MARKDOWN, DocumentFormat.EPUB}),
    DocumentFormat.DOCX: frozenset(
        {DocumentFormat.DOCX, DocumentFormat.MARKDOWN, DocumentFormat.EPUB}
    ),
    DocumentFormat.PDF: frozenset({DocumentFormat.MARKDOWN, DocumentFormat.EPUB}),
    DocumentFormat.EPUB: frozenset({DocumentFormat.EPUB, DocumentFormat.MARKDOWN}),
}


class ConfigurationSection(StrEnum):
    RESULT = "result"
    TRANSLATION = "translation"
    REFINEMENT = "refinement"
    STRUCTURE = "structure"
    PERSONALIZATION = "personalization"
    AI = "ai"


@dataclass(frozen=True, slots=True)
class ConfigurationIssue:
    section: ConfigurationSection
    message: str


def requires_ai(configuration: JobConfiguration) -> bool:
    """Whether at least one enabled phase needs the shared Ollama profile."""

    return (
        (
            configuration.translation.enabled
            and configuration.translation.method is TranslationMethod.LOCAL_AI
        )
        or configuration.refinement.enabled
        or configuration.structure.enabled
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
        issues.append(
            ConfigurationIssue(
                ConfigurationSection.RESULT,
                "Configura el resultado antes de procesar este documento.",
            )
        )
        return tuple(issues)
    if output.format not in SUPPORTED_OUTPUTS[source.format]:
        issues.append(
            ConfigurationIssue(
                ConfigurationSection.RESULT,
                "Selecciona un formato de salida compatible.",
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
    if configuration.structure.enabled and output.format is not DocumentFormat.EPUB:
        issues.append(
            ConfigurationIssue(
                ConfigurationSection.STRUCTURE,
                "La organización de capítulos solo está disponible para resultados EPUB.",
            )
        )
    if requires_ai(configuration) and effective_ai_profile(configuration).model is None:
        issues.append(
            ConfigurationIssue(
                ConfigurationSection.AI,
                "Elige un modelo de IA para las operaciones activadas.",
            )
        )
    if (
        not allow_noop_same_format
        and source.format is output.format
        and source.format is not DocumentFormat.EPUB
        and not (
            configuration.translation.enabled
            or configuration.refinement.enabled
            or configuration.structure.enabled
        )
    ):
        issues.append(
            ConfigurationIssue(
                ConfigurationSection.RESULT,
                "Activa al menos una mejora cuando la entrada y la salida tienen el mismo formato.",
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
    if output.directory_is_custom and output.directory is None:
        issues.append(
            ConfigurationIssue(
                ConfigurationSection.RESULT,
                "Elige una carpeta personalizada o vuelve al destino general.",
            )
        )
    return tuple(issues)
