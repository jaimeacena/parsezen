"""Map stable document jobs to and from the physical processing runtime."""

from __future__ import annotations

from parsezen.application.configuration_rules import configuration_issues
from parsezen.application.review_plan import review_steps_for_result
from parsezen.domain.jobs import (
    AIProfileConfiguration,
    CoverStrategy,
    DocumentFormat,
    DocumentJob,
    JobConfiguration,
    OutputConfiguration,
    PageRangeConfiguration,
    RefinementConfiguration,
    StructureConfiguration,
    TranslationConfiguration,
    TranslationMethod,
    effective_ai_profile,
)
from parsezen.domain.stages import StageKind
from parsezen.glossary import GlossaryEntry
from parsezen.improvement import ImprovementMode
from parsezen.pdf_conversion import PdfPageRange
from parsezen.processing import ProcessRequest, ProcessResult, ProcessStage
from parsezen.settings import AppSettings
from parsezen.workflow import OutputFormat, WorkflowOptions, plan_workflow

_PROCESS_STAGE_MAP = {
    ProcessStage.VALIDATING: StageKind.PREPARE,
    ProcessStage.READING: StageKind.PREPARE,
    ProcessStage.CONVERTING: StageKind.PREPARE,
    ProcessStage.OCR: StageKind.PREPARE,
    ProcessStage.PRESERVING_IMAGES: StageKind.PREPARE,
    ProcessStage.STRUCTURING: StageKind.PREPARE,
    ProcessStage.PREPARING_TRANSLATION: StageKind.TRANSLATE,
    ProcessStage.TRANSLATING: StageKind.TRANSLATE,
    ProcessStage.IMPROVING: StageKind.REFINE,
    ProcessStage.REVIEWING_CONTENT: StageKind.REFINE,
    ProcessStage.ORGANIZING_STRUCTURE: StageKind.STRUCTURE,
    ProcessStage.BUILDING_EPUB: StageKind.PUBLISH,
    ProcessStage.WRITING: StageKind.PUBLISH,
    ProcessStage.COMPLETED: StageKind.PUBLISH,
}


def stage_kind_from_process_stage(stage: ProcessStage | None) -> StageKind:
    """Translate one physical processor stage into the stable domain phase."""

    return _PROCESS_STAGE_MAP.get(stage or ProcessStage.VALIDATING, StageKind.PREPARE)


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
) -> JobConfiguration:
    translation_enabled = (
        request.target_language is not None
        or request.offline_translation_language is not None
        or request.improvement_mode
        in {ImprovementMode.TRANSLATE, ImprovementMode.CLEAN_AND_TRANSLATE}
    )
    refinement_enabled = request.review_content or request.improvement_mode in {
        ImprovementMode.CLEAN,
        ImprovementMode.CLEAN_AND_TRANSLATE,
    }
    return JobConfiguration(
        output=OutputConfiguration(
            configured=True,
            format=_document_format(request.output_format),
            directory=request.output_directory,
            directory_is_custom=request.output_directory is not None,
            include_images=request.include_images,
            image_directory=request.image_output_directory,
            preserve_styles=request.preserve_styles,
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
        ),
        translation=TranslationConfiguration(
            enabled=translation_enabled,
            method=(
                TranslationMethod.OFFLINE
                if request.offline_translation_language is not None
                else TranslationMethod.LOCAL_AI
            ),
            target_language=request.offline_translation_language or request.target_language,
            glossary=tuple((entry.source, entry.target) for entry in request.glossary),
            manual_review=True,
        ),
        refinement=RefinementConfiguration(
            enabled=refinement_enabled,
            manual_review=request.review_content,
        ),
        structure=StructureConfiguration(
            enabled=request.review_structure,
            manual_review=request.review_structure,
        ),
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
    issues = configuration_issues(
        job.source,
        configuration,
        allow_noop_same_format=True,
    )
    if issues:
        raise ValueError(issues[0].message)
    output_format = _process_output_format(configuration.output.format)
    options = WorkflowOptions(
        improvement_enabled=(
            configuration.translation.enabled
            or configuration.refinement.enabled
            or configuration.structure.enabled
        ),
        translate=configuration.translation.enabled,
        review_content=configuration.refinement.enabled,
        review_structure=configuration.structure.enabled,
    )
    plan = plan_workflow((job.source.path.suffix,), output_format, options=options)
    improvement_mode = None
    if (
        configuration.translation.enabled
        and configuration.translation.method is TranslationMethod.LOCAL_AI
    ):
        improvement_mode = ImprovementMode.TRANSLATE

    glossary = tuple(
        GlossaryEntry(source, target) for source, target in configuration.translation.glossary
    )
    request = ProcessRequest(
        source_path=job.source.path,
        convert_to_markdown=plan.convert_to_markdown_for(job.source.path.suffix),
        output_directory=configuration.output.directory,
        improvement_mode=improvement_mode,
        target_language=(
            configuration.translation.target_language
            if configuration.translation.enabled
            and configuration.translation.method is TranslationMethod.LOCAL_AI
            else None
        ),
        offline_translation_language=(
            configuration.translation.target_language
            if configuration.translation.enabled
            and configuration.translation.method is TranslationMethod.OFFLINE
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
        epub_first_page_cover=(configuration.output.cover_strategy is CoverStrategy.FIRST_PAGE),
        epub_remove_cover=(configuration.output.cover_strategy is CoverStrategy.REMOVE),
        review_content=configuration.refinement.enabled,
        review_structure=configuration.structure.enabled,
    )
    ai_profile = effective_ai_profile(configuration)
    settings = AppSettings(
        model=ai_profile.model,
        context_window=ai_profile.context_window,
        output_directory=configuration.output.directory,
        image_output_directory=configuration.output.image_directory,
        timeout_seconds=timeout_seconds,
        checkpoint_retention_days=checkpoint_retention_days,
    )
    return request, settings


def _document_format(output_format: OutputFormat) -> DocumentFormat:
    return {
        OutputFormat.TEXT: DocumentFormat.TEXT,
        OutputFormat.MARKDOWN: DocumentFormat.MARKDOWN,
        OutputFormat.DOCX: DocumentFormat.DOCX,
        OutputFormat.EPUB: DocumentFormat.EPUB,
    }[output_format]


def _process_output_format(document_format: DocumentFormat) -> OutputFormat:
    return {
        DocumentFormat.TEXT: OutputFormat.TEXT,
        DocumentFormat.MARKDOWN: OutputFormat.MARKDOWN,
        DocumentFormat.DOCX: OutputFormat.DOCX,
        DocumentFormat.EPUB: OutputFormat.EPUB,
        DocumentFormat.PDF: OutputFormat.MARKDOWN,
    }[document_format]
