"""Map stable product plans to and from the physical processing runtime."""

from __future__ import annotations

from parsezen.application.configuration_rules import configuration_issues
from parsezen.application.review_plan import review_steps_for_result
from parsezen.domain.jobs import (
    AIProfileConfiguration,
    CoverStrategy,
    DocumentFormat,
    DocumentJob,
    JobConfiguration,
    LocalAIPolicySnapshot,
    OutputConfiguration,
    PageRangeConfiguration,
    ProcessingPlan,
    TranslationConfiguration,
    TranslationMethod,
)
from parsezen.domain.stages import StageKind
from parsezen.glossary import GlossaryEntry
from parsezen.improvement import ImprovementMode
from parsezen.pdf_conversion import PdfPageRange
from parsezen.pipeline.contracts import ProcessRequest, ProcessResult
from parsezen.settings import AppSettings
from parsezen.workflow import OutputFormat, WorkflowOptions, plan_workflow


def review_stage_for_result(
    result: ProcessResult,
    configuration: JobConfiguration,
) -> StageKind:
    """Locate a combined worker review in the exact blocking domain phase."""

    steps = review_steps_for_result(result, configuration)
    return steps[0].stage if steps else StageKind.PREPARE


def configuration_from_request(
    request: ProcessRequest,
    settings: AppSettings,
    *,
    local_ai_policy: LocalAIPolicySnapshot | None = None,
) -> JobConfiguration:
    """Normalize a low-level request into the product's two-plan configuration."""

    translation_enabled = (
        request.target_language is not None
        or request.offline_translation_language is not None
        or request.improvement_mode
        in {ImprovementMode.TRANSLATE, ImprovementMode.CLEAN_AND_TRANSLATE}
    )
    reviewed = (
        request.review_content
        or request.review_structure
        or request.improvement_mode
        in {
            ImprovementMode.CLEAN,
            ImprovementMode.CLEAN_AND_TRANSLATE,
        }
    )
    return JobConfiguration(
        output=OutputConfiguration(
            configured=True,
            format=_document_format(request.output_format),
            directory=request.output_directory,
            include_images=request.include_images,
            image_directory=request.image_output_directory,
            preserve_styles=request.preserve_styles,
            markdown_organization=request.markdown_organization,
            markdown_include_metadata=request.markdown_include_metadata,
            markdown_include_page_references=request.markdown_include_page_references,
            title=request.epub_title,
            author=request.epub_author,
            cover_strategy=(
                CoverStrategy.REMOVE
                if request.epub_remove_cover
                else CoverStrategy.CUSTOM
                if request.epub_cover_path is not None
                else CoverStrategy.FIRST_PAGE
                if request.epub_first_page_cover
                else CoverStrategy.NONE
            ),
            cover_path=request.epub_cover_path,
        ),
        ai=AIProfileConfiguration(
            model=settings.model,
            context_window=settings.context_window,
            components=(
                local_ai_policy if local_ai_policy is not None else LocalAIPolicySnapshot()
            ),
            translation_model=(
                local_ai_policy.translation.model
                if local_ai_policy is not None and local_ai_policy.translation is not None
                else settings.translation_model
            ),
            translation_context_window=(
                local_ai_policy.translation.context_window
                if local_ai_policy is not None
                and local_ai_policy.translation is not None
                and local_ai_policy.translation.context_window is not None
                else settings.translation_context_window
            ),
            review_model=(
                local_ai_policy.review.model
                if local_ai_policy is not None and local_ai_policy.review is not None
                else settings.review_model
            ),
            review_context_window=(
                local_ai_policy.review.context_window
                if local_ai_policy is not None
                and local_ai_policy.review is not None
                and local_ai_policy.review.context_window is not None
                else settings.review_context_window
            ),
        ),
        translation=TranslationConfiguration(
            enabled=translation_enabled,
            method=(
                TranslationMethod.LOCAL_AI
                if request.target_language is not None
                or request.improvement_mode
                in {ImprovementMode.TRANSLATE, ImprovementMode.CLEAN_AND_TRANSLATE}
                else TranslationMethod.OFFLINE
            ),
            target_language=request.offline_translation_language or request.target_language,
            glossary=tuple((entry.source, entry.target) for entry in request.glossary),
        ),
        plan=(ProcessingPlan.LOCAL_AI_REVIEWED if reviewed else ProcessingPlan.STANDARD),
        page_range=(
            PageRangeConfiguration(
                request.pdf_page_range.first_page,
                request.pdf_page_range.last_page,
            )
            if request.pdf_page_range is not None
            else None
        ),
        force_pdf_ocr=request.force_pdf_ocr,
    )


def request_and_settings_from_job(
    job: DocumentJob,
    *,
    timeout_seconds: float = 120.0,
    checkpoint_retention_days: int = 30,
) -> tuple[ProcessRequest, AppSettings]:
    configuration = job.configuration
    issues = configuration_issues(job.source, configuration, allow_noop_same_format=True)
    if issues:
        raise ValueError(issues[0].message)
    output_format = _process_output_format(configuration.output.format)
    reviewed = configuration.plan is ProcessingPlan.LOCAL_AI_REVIEWED
    ai_translation = (
        configuration.translation.enabled
        and configuration.translation.method is TranslationMethod.LOCAL_AI
    )
    review_structure = reviewed and configuration.output.format is DocumentFormat.EPUB
    options = WorkflowOptions(
        improvement_enabled=configuration.translation.enabled or reviewed,
        translate=configuration.translation.enabled,
        review_content=reviewed,
        review_structure=review_structure,
    )
    plan = plan_workflow((job.source.path.suffix,), output_format, options=options)
    glossary = tuple(
        GlossaryEntry(source, target) for source, target in configuration.translation.glossary
    )
    request = ProcessRequest(
        source_path=job.source.path,
        convert_to_markdown=plan.convert_to_markdown_for(job.source.path.suffix),
        output_directory=configuration.output.directory,
        improvement_mode=(ImprovementMode.TRANSLATE if ai_translation else None),
        target_language=(configuration.translation.target_language if ai_translation else None),
        offline_translation_language=(
            configuration.translation.target_language
            if configuration.translation.enabled and not ai_translation
            else None
        ),
        pdf_page_range=(
            PdfPageRange(
                configuration.page_range.first_page,
                configuration.page_range.last_page,
            )
            if configuration.page_range is not None
            else None
        ),
        force_pdf_ocr=configuration.force_pdf_ocr,
        output_format=output_format,
        image_output_directory=configuration.output.image_directory,
        epub_title=configuration.output.title,
        epub_author=configuration.output.author,
        epub_cover_path=configuration.output.cover_path,
        glossary=glossary,
        include_images=configuration.output.include_images,
        preserve_styles=configuration.output.preserve_styles,
        markdown_organization=configuration.output.markdown_organization,
        markdown_include_metadata=configuration.output.markdown_include_metadata,
        markdown_include_page_references=configuration.output.markdown_include_page_references,
        epub_first_page_cover=(configuration.output.cover_strategy is CoverStrategy.FIRST_PAGE),
        epub_remove_cover=(configuration.output.cover_strategy is CoverStrategy.REMOVE),
        review_content=reviewed,
        review_structure=review_structure,
        source_size_bytes=job.source.size_bytes,
        source_modified_ns=job.source.modified_ns,
        source_content_sha256=job.source.content_sha256,
    )
    settings = AppSettings(
        model=configuration.ai.model,
        context_window=configuration.ai.context_window,
        translation_model=(
            configuration.ai.components.translation.model
            if configuration.ai.components.translation is not None
            else configuration.ai.translation_model
        ),
        translation_context_window=(
            configuration.ai.components.translation.context_window
            if configuration.ai.components.translation is not None
            and configuration.ai.components.translation.context_window is not None
            else configuration.ai.translation_context_window
        ),
        review_model=(
            configuration.ai.components.review.model
            if configuration.ai.components.review is not None
            else configuration.ai.review_model
        ),
        review_context_window=(
            configuration.ai.components.review.context_window
            if configuration.ai.components.review is not None
            and configuration.ai.components.review.context_window is not None
            else configuration.ai.review_context_window
        ),
        output_directory=configuration.output.directory,
        image_output_directory=configuration.output.image_directory,
        timeout_seconds=timeout_seconds,
        checkpoint_retention_days=checkpoint_retention_days,
    )
    return request, settings


def _document_format(output_format: OutputFormat) -> DocumentFormat:
    return {
        OutputFormat.MARKDOWN: DocumentFormat.MARKDOWN,
        OutputFormat.EPUB: DocumentFormat.EPUB,
    }[output_format]


def _process_output_format(document_format: DocumentFormat) -> OutputFormat:
    if document_format is DocumentFormat.EPUB:
        return OutputFormat.EPUB
    return OutputFormat.MARKDOWN
