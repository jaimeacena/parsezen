"""Deterministic, Qt-free execution route for one configured document."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from parsezen.domain.jobs import (
    CoverStrategy,
    DocumentFormat,
    DocumentSource,
    JobConfiguration,
    ProcessingPlan,
    TranslationMethod,
)


class ExecutionStep(StrEnum):
    """Closed set of material operations understood by Parsezen."""

    CONVERT = "Convert"
    TRANSLATE_AI = "TranslateAI"
    TRANSLATE_OFFLINE = "TranslateOffline"
    REPAIR_TRANSLATION = "RepairTranslation"
    REVIEW_CONTENT = "ReviewContent"
    REVIEW_STRUCTURE = "ReviewStructure"
    PRESERVE_EPUB = "PreserveEpub"
    BUILD_EPUB = "BuildEpub"
    WRITE_MARKDOWN = "WriteMarkdown"


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """The single authoritative sequence for one source and configuration."""

    source_format: DocumentFormat
    output_format: DocumentFormat
    steps: tuple[ExecutionStep, ...]

    def includes(self, step: ExecutionStep) -> bool:
        return step in self.steps

    @property
    def translates(self) -> bool:
        return any(
            step in self.steps
            for step in (ExecutionStep.TRANSLATE_AI, ExecutionStep.TRANSLATE_OFFLINE)
        )

    @property
    def preserves_epub(self) -> bool:
        return self.includes(ExecutionStep.PRESERVE_EPUB)

    @property
    def builds_epub(self) -> bool:
        return self.includes(ExecutionStep.BUILD_EPUB)

    @property
    def uses_direct_epub_executor(self) -> bool:
        return (
            self.source_format is DocumentFormat.EPUB
            and self.output_format is DocumentFormat.EPUB
            and not self.includes(ExecutionStep.CONVERT)
        )


def compile_execution_plan(
    source: DocumentSource,
    configuration: JobConfiguration,
) -> ExecutionPlan:
    """Compile the exact operation order from the durable product contract."""

    return _compile_execution_plan(
        source.format,
        configuration.output.format,
        translation_method=(
            configuration.translation.method if configuration.translation.enabled else None
        ),
        reviewed=configuration.plan is ProcessingPlan.LOCAL_AI_REVIEWED,
        preserve_epub_package=(
            configuration.output.preserve_styles
            and configuration.output.include_images
            and configuration.output.cover_strategy
            not in {CoverStrategy.CUSTOM, CoverStrategy.REMOVE}
        ),
    )


def execution_plan_from_runtime(
    source_format: DocumentFormat,
    output_format: DocumentFormat,
    *,
    translation_method: TranslationMethod | None,
    reviewed: bool,
    preserve_epub_package: bool = True,
    convert_source: bool | None = None,
) -> ExecutionPlan:
    """Temporary adapter for callers that still enter through ``ProcessRequest``."""

    return _compile_execution_plan(
        source_format,
        output_format,
        translation_method=translation_method,
        reviewed=reviewed,
        preserve_epub_package=preserve_epub_package,
        convert_source=convert_source,
    )


def _compile_execution_plan(
    source_format: DocumentFormat,
    output_format: DocumentFormat,
    *,
    translation_method: TranslationMethod | None,
    reviewed: bool,
    preserve_epub_package: bool,
    convert_source: bool | None = None,
) -> ExecutionPlan:
    direct_epub = (
        source_format is DocumentFormat.EPUB
        and output_format is DocumentFormat.EPUB
        and (translation_method is not None or not reviewed)
    )
    steps: list[ExecutionStep] = []
    needs_conversion = False if direct_epub else True if convert_source is None else convert_source
    if needs_conversion:
        steps.append(ExecutionStep.CONVERT)
    if translation_method is TranslationMethod.LOCAL_AI:
        steps.extend((ExecutionStep.TRANSLATE_AI, ExecutionStep.REPAIR_TRANSLATION))
    elif translation_method is TranslationMethod.OFFLINE:
        steps.extend((ExecutionStep.TRANSLATE_OFFLINE, ExecutionStep.REPAIR_TRANSLATION))
    if reviewed:
        steps.append(ExecutionStep.REVIEW_CONTENT)
        if output_format is DocumentFormat.EPUB:
            steps.append(ExecutionStep.REVIEW_STRUCTURE)
    if direct_epub:
        steps.append(
            ExecutionStep.PRESERVE_EPUB if preserve_epub_package else ExecutionStep.BUILD_EPUB
        )
    elif output_format is DocumentFormat.EPUB:
        steps.append(ExecutionStep.BUILD_EPUB)
    else:
        steps.append(ExecutionStep.WRITE_MARKDOWN)
    return ExecutionPlan(source_format, output_format, tuple(steps))
