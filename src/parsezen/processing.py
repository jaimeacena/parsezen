"""Synchronous, Qt-free orchestration for one local document."""

from __future__ import annotations

import inspect
import logging
import os
import re
import secrets
import shutil
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath
from tempfile import gettempdir, mkstemp
from time import monotonic
from typing import TypedDict

from parsezen.cancellation import CancellationToken, check_cancelled
from parsezen.conversion import (
    CONVERSION_REQUIRED_EXTENSIONS,
    MAX_DIRECT_TEXT_BYTES,
    SUPPORTED_EXTENSIONS,
    convert_document,
    materialize_converted_markdown,
    validate_docx_container,
)
from parsezen.document_model import ConvertedResource
from parsezen.docx_preservation import (
    rebuild_docx_with_paragraphs,
    split_docx_payload,
    transform_docx,
)
from parsezen.epub_builder import (
    EpubBookMetadata,
    build_epub,
    validate_epub_file,
)
from parsezen.epub_checkpoints import (
    EpubTranslationCheckpoints,
    open_epub_translation_checkpoints,
    prune_epub_translation_cache,
)
from parsezen.epub_conversion import (
    convert_epub,
    inspect_epub_package,
    replace_epub_metadata,
    translate_epub,
)
from parsezen.errors import (
    ParsezenError,
    ProcessingCancelledError,
    RequestValidationError,
    SettingsError,
    UnexpectedProcessingError,
)
from parsezen.final_integrity import (
    FinalIntegrityReport,
    IntegrityLedger,
    binary_integrity_capture,
    merge_integrity_reports,
    text_integrity_capture,
)
from parsezen.glossary import (
    MAX_GLOSSARY_ENTRIES,
    GlossaryEntry,
    glossary_fingerprint,
    protect_glossary,
    validate_glossary,
)
from parsezen.improvement import ImprovementMode, improve_markdown
from parsezen.local_models import is_reasoning_model_id
from parsezen.markdown_resources import without_markdown_images, without_markdown_resource
from parsezen.offline_translation import translate_markdown_offline
from parsezen.output import (
    replace_binary_output,
    replace_text_output,
    write_conversion_output,
    write_docx_output,
    write_epub_output,
    write_epub_translation_output,
    write_improvement_outputs,
    write_text_output,
)
from parsezen.pdf_conversion import (
    PdfPageRange,
    PdfProgressCallback,
    PdfProgressPhase,
    PdfQualityReport,
    extract_pdf_warning_pages,
    render_pdf_page_cover,
    resolve_pdf_page_range,
    strip_pdf_page_markers,
)
from parsezen.revision import RevisionDraft, RevisionKind, build_revision_draft
from parsezen.semantic_blocks import (
    DocumentTerm,
    SemanticDocument,
    SemanticRole,
    analyze_markdown,
    terminology_fingerprint,
)
from parsezen.settings import AppSettings, validate_settings
from parsezen.translation_quality import (
    TranslationQualityReport,
    build_translation_quality_report,
    detect_language_code,
    repair_untranslated_source_text,
    resolve_language_code,
    restore_changed_third_language_headings,
)
from parsezen.work_checkpoints import (
    WorkCheckpoints,
    checkpoint_key,
    open_work_checkpoints,
    prune_work_checkpoint_cache,
)
from parsezen.workflow import OutputFormat, WorkflowOptions, plan_workflow

LOGGER = logging.getLogger(__name__)
_PDF_OCR_CHECKPOINT_HEADER = "\x1eParsezen PDF OCR v1\x1f"
_MAX_EPUB_COVER_BYTES = 32 * 1024 * 1024
_MAX_INFERRED_TERMINOLOGY_OCCURRENCES = 64
_EPUB_COVER_MEDIA_TYPES = {
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}


def _encode_pdf_ocr_checkpoint(markdown: str) -> str:
    """Preserve the valid distinction between no result and an empty OCR page."""
    return f"{_PDF_OCR_CHECKPOINT_HEADER}{markdown}"


def _decode_pdf_ocr_checkpoint(payload: str | None) -> str | None:
    """Read the tagged OCR payload while remaining compatible with older caches."""
    if payload is None:
        return None
    if payload.startswith(_PDF_OCR_CHECKPOINT_HEADER):
        return payload[len(_PDF_OCR_CHECKPOINT_HEADER) :]
    return payload


class ProcessStage(StrEnum):
    """Observable stages backed by real work whenever a total is available."""

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
class StageTelemetry:
    """Aggregate time spent in one observable processing stage."""

    stage: ProcessStage
    duration_ms: int
    visits: int


@dataclass(frozen=True, slots=True)
class ProcessTelemetry:
    """Privacy-safe timings for one completed local transformation."""

    total_duration_ms: int
    stages: tuple[StageTelemetry, ...]


class _ProcessingTelemetryCollector:
    """Measure real stage transitions without retaining document content or paths."""

    def __init__(self, started_at: float) -> None:
        self._started_at = started_at
        self._current_stage: ProcessStage | None = None
        self._stage_started_at = started_at
        self._durations: dict[ProcessStage, float] = {}
        self._visits: dict[ProcessStage, int] = {}

    def enter(self, stage: ProcessStage, now: float) -> None:
        if self._current_stage is stage:
            return
        self._close_current(now)
        self._current_stage = stage
        self._stage_started_at = now
        self._visits[stage] = self._visits.get(stage, 0) + 1

    def snapshot(self, now: float) -> ProcessTelemetry:
        self._close_current(now)
        stages = tuple(
            StageTelemetry(
                stage=stage,
                duration_ms=max(0, round(self._durations.get(stage, 0.0) * 1000)),
                visits=self._visits[stage],
            )
            for stage in ProcessStage
            if stage in self._visits
        )
        return ProcessTelemetry(
            total_duration_ms=max(0, round((now - self._started_at) * 1000)),
            stages=stages,
        )

    def _close_current(self, now: float) -> None:
        if self._current_stage is None:
            return
        elapsed = max(0.0, now - self._stage_started_at)
        self._durations[self._current_stage] = (
            self._durations.get(self._current_stage, 0.0) + elapsed
        )
        self._current_stage = None


@dataclass(frozen=True, slots=True)
class ProcessRequest:
    """One local conversion and/or improvement request."""

    source_path: Path
    convert_to_markdown: bool
    output_directory: Path | None = None
    improvement_mode: ImprovementMode | None = None
    target_language: str | None = None
    offline_translation_language: str | None = None
    pdf_page_range: PdfPageRange | None = None
    force_pdf_ocr: bool = False
    output_format: OutputFormat = OutputFormat.MARKDOWN
    image_output_directory: Path | None = None
    epub_title: str | None = None
    epub_author: str | None = None
    epub_cover_path: Path | None = None
    glossary: tuple[GlossaryEntry, ...] = ()
    include_images: bool = True
    preserve_styles: bool = True
    epub_first_page_cover: bool = False
    epub_remove_cover: bool = False
    review_content: bool = False
    review_structure: bool = False


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Small result returned to the desktop UI."""

    final_path: Path
    raw_markdown_path: Path | None = None
    review_original_path: Path | None = None
    problematic_pdf_pages: tuple[int, ...] = ()
    pdf_quality_report: PdfQualityReport | None = None
    exhaustive_pdf_ocr_used: bool = False
    epub_translation_parts: int = 0
    epub_resumed_parts: int = 0
    epub_checkpoint_degraded: bool = False
    translation_quality_report: TranslationQualityReport | None = None
    preserved_translation_chunks: tuple[int, ...] = ()
    preserved_images: int = 0
    epub_chapters: int = 0
    revision_draft: RevisionDraft | None = None
    revision_resources: tuple[ConvertedResource, ...] = ()
    revision_epub_metadata: EpubBookMetadata | None = None
    revision_docx_original_paragraphs: tuple[str, ...] = ()
    revision_docx_proposed_paragraphs: tuple[str, ...] = ()
    review_markdown: str | None = None
    review_required: bool = False
    revision_approved: bool = False
    final_integrity_report: FinalIntegrityReport | None = None
    telemetry: ProcessTelemetry | None = None
    front_matter_blocks: int = 0
    toc_blocks: int = 0
    terminology_terms: int = 0


StageCallback = Callable[[ProcessStage], None]
ProgressCallback = Callable[[int, int], None]


class _ConversionArguments(TypedDict, total=False):
    on_ocr_start: Callable[[], None]
    on_pdf_quality_report: Callable[[PdfQualityReport], None]
    cancellation: CancellationToken
    pdf_page_range: PdfPageRange
    force_pdf_ocr: bool
    on_pdf_progress: PdfProgressCallback
    load_pdf_ocr_checkpoint: Callable[[int], str | None]
    save_pdf_ocr_checkpoint: Callable[[int, str], bool]
    load_pdf_page_checkpoint: Callable[[int], str | None]
    save_pdf_page_checkpoint: Callable[[int, str], bool]


class _ImprovementArguments(TypedDict, total=False):
    on_progress: ProgressCallback | None
    cancellation: CancellationToken
    load_checkpoint: Callable[[str], str | None]
    save_checkpoint: Callable[[str, str], bool]
    plain_text: bool
    on_translation_preserved: Callable[[int, int], None]


class _OfflineTranslationArguments(TypedDict, total=False):
    on_progress: ProgressCallback | None
    on_engine_ready: Callable[[], None]
    cancellation: CancellationToken


def process_document(
    request: ProcessRequest,
    on_stage: StageCallback | None = None,
    on_progress: ProgressCallback | None = None,
    *,
    settings: AppSettings | None = None,
    cancellation: CancellationToken | None = None,
    epub_checkpoint_root: Path | None = None,
    work_checkpoint_root: Path | None = None,
) -> ProcessResult:
    """Validate, read/convert, optionally improve and safely write one document."""
    started_at = monotonic()
    current_stage: ProcessStage | None = None
    telemetry = _ProcessingTelemetryCollector(started_at)

    def report_stage(stage: ProcessStage) -> None:
        nonlocal current_stage
        current_stage = stage
        telemetry.enter(stage, monotonic())
        # ``_process_document`` already routes every transition through
        # ``_notify``. Calling it again here duplicated each structured log
        # entry even though the UI only received one real transition.
        if on_stage is not None:
            on_stage(stage)

    extension = request.source_path.suffix.lower() or "(none)"
    mode = (
        request.improvement_mode.value
        if isinstance(request.improvement_mode, ImprovementMode)
        else "none"
        if request.improvement_mode is None
        else "invalid"
    )
    LOGGER.info(
        "processing_started extension=%s output_format=%s conversion=%s improvement_mode=%s "
        "offline_translation=%s review_content=%s review_structure=%s",
        extension,
        request.output_format.value,
        request.convert_to_markdown,
        mode,
        request.offline_translation_language is not None,
        request.review_content,
        request.review_structure,
    )
    try:
        translation_session: AbstractContextManager[object]
        if request.offline_translation_language is None:
            translation_session = nullcontext()
        else:
            from parsezen.offline_translation_executor import offline_translation_session

            translation_session = offline_translation_session()
        with translation_session:
            result = _process_document(
                request,
                report_stage,
                on_progress,
                settings=settings,
                cancellation=cancellation,
                epub_checkpoint_root=epub_checkpoint_root,
                work_checkpoint_root=work_checkpoint_root,
            )
    except ParsezenError as exc:
        LOGGER.warning(
            "processing_failed error_type=%s stage=%s duration_ms=%d",
            type(exc).__name__,
            current_stage.value if current_stage is not None else "not_started",
            _elapsed_milliseconds(started_at),
        )
        raise
    except Exception as exc:
        incident_id, module_name, function_name, line_number = _safe_failure_location(exc)
        LOGGER.error(
            "processing_failed incident=%s unexpected_error_type=%s module=%s function=%s "
            "line=%d stage=%s duration_ms=%d",
            incident_id,
            type(exc).__name__,
            module_name,
            function_name,
            line_number,
            current_stage.value if current_stage is not None else "not_started",
            _elapsed_milliseconds(started_at),
        )
        raise UnexpectedProcessingError(
            f"Se produjo un error inesperado. Referencia local: {incident_id}."
        ) from exc

    process_telemetry = telemetry.snapshot(monotonic())
    result = replace(result, telemetry=process_telemetry)
    LOGGER.info(
        "processing_completed extension=%s output_format=%s improvement_mode=%s raw_output=%s "
        "duration_ms=%d",
        extension,
        request.output_format.value,
        mode,
        result.raw_markdown_path is not None,
        process_telemetry.total_duration_ms,
    )
    return result


def apply_reviewed_revision(result: ProcessResult, reviewed_text: str) -> ProcessResult:
    """Publish locally reviewed text over the app-created conservative result."""
    draft = result.revision_draft
    if draft is None and result.review_markdown is None:
        raise RequestValidationError("Este resultado no contiene texto editable para revisar.")
    if not isinstance(reviewed_text, str) or not reviewed_text.strip() or "\0" in reviewed_text:
        raise RequestValidationError("El texto revisado está vacío o no es válido.")

    published_text = strip_pdf_page_markers(reviewed_text)
    suffix = result.final_path.suffix.lower()
    epub_chapters = result.epub_chapters
    preserved_images = result.preserved_images
    integrity_report: FinalIntegrityReport | None = None
    if suffix in {".txt", ".md", ".markdown"}:
        integrity = text_integrity_capture(
            published_text,
            markdown=suffix in {".md", ".markdown"},
        )
        replace_text_output(
            result.final_path,
            published_text,
            validate_staged=integrity,
        )
        integrity_report = integrity.report
    elif suffix == ".epub":
        metadata = result.revision_epub_metadata
        if metadata is None:
            raise RequestValidationError("Falta la información necesaria para reconstruir el EPUB.")
        built = build_epub(published_text, result.revision_resources, metadata)
        integrity = binary_integrity_capture(
            built.content,
            format_label="EPUB",
            validate_container=_validate_epub_path,
            ledger=(
                built.integrity_report.ledger
                if built.integrity_report is not None
                else IntegrityLedger()
            ),
        )
        replace_binary_output(
            result.final_path,
            built.content,
            validate_staged=integrity,
        )
        integrity_report = merge_integrity_reports(
            built.integrity_report,
            integrity.report,
        )
        epub_chapters = built.chapter_count
        preserved_images = built.resource_count
    elif suffix == ".docx":
        source_path = result.review_original_path
        if source_path is None:
            raise RequestValidationError(
                "Falta el documento Word original para aplicar la revisión."
            )
        if draft is not None and reviewed_text == draft.original_markdown:
            paragraphs = result.revision_docx_original_paragraphs
        elif draft is not None and reviewed_text == draft.proposed_markdown:
            paragraphs = result.revision_docx_proposed_paragraphs
        else:
            paragraphs = tuple(published_text.split("\n\n"))
        content = rebuild_docx_with_paragraphs(source_path, paragraphs)
        integrity = binary_integrity_capture(
            content,
            format_label="DOCX",
            validate_container=validate_docx_container,
        )
        replace_binary_output(
            result.final_path,
            content,
            validate_staged=integrity,
        )
        integrity_report = integrity.report
    else:
        raise RequestValidationError(
            "Este formato todavía no admite aplicar una revisión editable."
        )
    return replace(
        result,
        epub_chapters=epub_chapters,
        preserved_images=preserved_images,
        review_markdown=published_text,
        review_required=False,
        revision_approved=True,
        final_integrity_report=integrity_report,
    )


def _process_document(
    request: ProcessRequest,
    on_stage: StageCallback | None,
    on_progress: ProgressCallback | None,
    *,
    settings: AppSettings | None,
    cancellation: CancellationToken | None,
    epub_checkpoint_root: Path | None,
    work_checkpoint_root: Path | None,
) -> ProcessResult:
    _notify(on_stage, ProcessStage.VALIDATING)
    check_cancelled(cancellation)
    _validate_request(request, settings)
    check_cancelled(cancellation)

    if _is_epub_translation(request):
        return _process_epub_translation(
            request,
            on_stage,
            on_progress,
            settings=settings,
            cancellation=cancellation,
            epub_checkpoint_root=epub_checkpoint_root,
        )

    if _is_epub_personalization(request):
        return _process_epub_personalization(
            request,
            on_stage,
            cancellation=cancellation,
        )

    if _is_docx_transformation(request):
        return _process_docx_transformation(
            request,
            on_stage,
            on_progress,
            settings=settings,
            cancellation=cancellation,
            work_checkpoint_root=work_checkpoint_root,
        )

    work_checkpoints = _open_general_work_checkpoints(
        request,
        settings,
        root=work_checkpoint_root,
    )
    pdf_checkpoints = _open_pdf_conversion_checkpoints(
        request,
        root=work_checkpoint_root,
    )

    source_path = request.source_path
    input_stage = (
        ProcessStage.CONVERTING
        if source_path.suffix.lower() in CONVERSION_REQUIRED_EXTENSIONS
        else ProcessStage.READING
    )
    _notify(on_stage, input_stage)
    resolved_page_range = (
        resolve_pdf_page_range(source_path, request.pdf_page_range)
        if request.pdf_page_range is not None
        else None
    )
    check_cancelled(cancellation)
    output_stem = (
        f"{source_path.stem}.pages-{resolved_page_range.first_page}-{resolved_page_range.last_page}"
        if resolved_page_range is not None
        else None
    )
    pdf_quality_report: PdfQualityReport | None = None

    def capture_pdf_quality_report(report: PdfQualityReport) -> None:
        nonlocal pdf_quality_report
        pdf_quality_report = report

    conversion_arguments: _ConversionArguments = {}
    if source_path.suffix.lower() == ".pdf":
        current_pdf_stage = input_stage

        def announce_pdf_stage(stage: ProcessStage) -> None:
            nonlocal current_pdf_stage
            if stage is current_pdf_stage:
                return
            current_pdf_stage = stage
            _notify(on_stage, stage)

        def report_pdf_progress(
            phase: PdfProgressPhase,
            current: int,
            total: int,
        ) -> None:
            stage = {
                PdfProgressPhase.EXTRACTING: ProcessStage.CONVERTING,
                PdfProgressPhase.OCR: ProcessStage.OCR,
                PdfProgressPhase.IMAGES: ProcessStage.PRESERVING_IMAGES,
                PdfProgressPhase.STRUCTURING: ProcessStage.STRUCTURING,
            }[phase]
            announce_pdf_stage(stage)
            if on_progress is not None:
                on_progress(current, total)

        conversion_arguments["on_ocr_start"] = lambda: announce_pdf_stage(ProcessStage.OCR)
        conversion_arguments["on_pdf_progress"] = report_pdf_progress
        conversion_arguments["on_pdf_quality_report"] = capture_pdf_quality_report
        if pdf_checkpoints is not None:
            conversion_arguments["load_pdf_page_checkpoint"] = lambda page_number: (
                pdf_checkpoints.load(checkpoint_key("pdf-native-page", str(page_number)))
            )
            conversion_arguments["save_pdf_page_checkpoint"] = lambda page_number, text: (
                pdf_checkpoints.save(
                    checkpoint_key("pdf-native-page", str(page_number)),
                    text,
                )
            )
            conversion_arguments["load_pdf_ocr_checkpoint"] = lambda page_number: (
                _decode_pdf_ocr_checkpoint(
                    pdf_checkpoints.load(checkpoint_key("pdf-ocr-page", str(page_number)))
                )
            )
            conversion_arguments["save_pdf_ocr_checkpoint"] = lambda page_number, text: (
                pdf_checkpoints.save(
                    checkpoint_key("pdf-ocr-page", str(page_number)),
                    _encode_pdf_ocr_checkpoint(text),
                )
            )
        if resolved_page_range is not None:
            conversion_arguments["pdf_page_range"] = resolved_page_range
        if request.force_pdf_ocr:
            conversion_arguments["force_pdf_ocr"] = True
    if cancellation is not None:
        conversion_arguments["cancellation"] = cancellation
    generated_epub = request.output_format is OutputFormat.EPUB
    source_cover_path: PurePosixPath | None = None
    if source_path.suffix.lower() == ".epub" and generated_epub:
        source_cover_path = inspect_epub_package(source_path).cover_path
    preserve_source_cover = (
        source_cover_path is not None
        and request.epub_cover_path is None
        and not request.epub_remove_cover
    )
    converted_document = convert_document(
        source_path,
        preserve_resources=request.include_images or preserve_source_cover,
        strict_resource_references=generated_epub and request.include_images,
        **conversion_arguments,
    )
    markdown = (
        converted_document.markdown
        if request.include_images
        else without_markdown_images(converted_document.markdown)
    )
    if source_cover_path is not None and (
        request.epub_cover_path is not None or request.epub_remove_cover
    ):
        markdown = without_markdown_resource(markdown, source_cover_path)
    converted_resources: tuple[ConvertedResource, ...] = (
        converted_document.resources
        if request.include_images
        else tuple(
            resource
            for resource in converted_document.resources
            if resource.relative_path == source_cover_path
        )
    )
    if source_cover_path is not None and (
        request.epub_cover_path is not None or request.epub_remove_cover
    ):
        converted_resources = tuple(
            resource
            for resource in converted_resources
            if resource.relative_path != source_cover_path
        )
    check_cancelled(cancellation)
    problematic_pdf_pages = (
        tuple(issue.page_number for issue in pdf_quality_report.issues)
        if pdf_quality_report is not None
        else extract_pdf_warning_pages(markdown)
        if source_path.suffix.lower() == ".pdf"
        else ()
    )
    semantic_document = analyze_markdown(markdown)
    translation_glossary = _combined_translation_glossary(
        request.glossary,
        semantic_document.terms,
    )

    transformed_markdown = markdown
    translation_source: str | None = None
    preserved_translation_chunks: list[int] = []
    effective_ai_mode = _effective_ai_improvement_mode(request)
    ai_translation_is_redundant = (
        request.improvement_mode is ImprovementMode.TRANSLATE
        and _translation_is_redundant(
            transformed_markdown,
            request.target_language,
        )
    )
    if request.improvement_mode is not None and not ai_translation_is_redundant:
        if settings is None:
            raise AssertionError("Validated improvement requests always have settings.")
        if effective_ai_mode is None:
            raise AssertionError("An enabled AI transformation always has an effective mode.")
        _notify(
            on_stage,
            (
                ProcessStage.TRANSLATING
                if request.improvement_mode
                in {ImprovementMode.TRANSLATE, ImprovementMode.CLEAN_AND_TRANSLATE}
                else ProcessStage.IMPROVING
            ),
        )
        if request.improvement_mode in {
            ImprovementMode.TRANSLATE,
            ImprovementMode.CLEAN_AND_TRANSLATE,
        }:
            translation_source = transformed_markdown
        improvement_arguments: _ImprovementArguments = {"on_progress": on_progress}
        checkpoint_parameters = inspect.signature(improve_markdown).parameters
        if "on_translation_preserved" in checkpoint_parameters:
            improvement_arguments["on_translation_preserved"] = lambda current, _total: (
                preserved_translation_chunks.append(current)
            )
        if request.output_format is OutputFormat.TEXT:
            improvement_arguments["plain_text"] = True
        if cancellation is not None:
            improvement_arguments["cancellation"] = cancellation
        if work_checkpoints is not None and "load_checkpoint" in checkpoint_parameters:
            improvement_arguments["load_checkpoint"] = work_checkpoints.load
            improvement_arguments["save_checkpoint"] = work_checkpoints.save
        protected = (
            protect_glossary(transformed_markdown, translation_glossary)
            if request.improvement_mode
            in {ImprovementMode.TRANSLATE, ImprovementMode.CLEAN_AND_TRANSLATE}
            else None
        )
        transformed_markdown = improve_markdown(
            protected.text if protected is not None else transformed_markdown,
            effective_ai_mode,
            settings,
            request.target_language,
            **improvement_arguments,
        )
        if protected is not None:
            transformed_markdown = protected.restore(transformed_markdown)

        check_cancelled(cancellation)
        if translation_source is not None:
            transformed_markdown = _repair_translation_warnings(
                request,
                translation_source,
                transformed_markdown,
                settings=settings,
                cancellation=cancellation,
                translation_glossary=translation_glossary,
            )
    elif ai_translation_is_redundant:
        LOGGER.info("translation_skipped already_in_target_language=true engine=ai")

    offline_translation_is_redundant = (
        request.offline_translation_language is not None
        and _translation_is_redundant(
            transformed_markdown,
            request.offline_translation_language,
        )
    )
    if request.offline_translation_language is not None and not offline_translation_is_redundant:
        _notify(on_stage, ProcessStage.PREPARING_TRANSLATION)
        translation_source = transformed_markdown
        translation_stage_announced = False

        def announce_translation_stage() -> None:
            nonlocal translation_stage_announced
            if translation_stage_announced:
                return
            translation_stage_announced = True
            _notify(on_stage, ProcessStage.TRANSLATING)

        translation_arguments: _OfflineTranslationArguments = {
            "on_progress": on_progress,
            "on_engine_ready": announce_translation_stage,
        }
        if cancellation is not None:
            translation_arguments["cancellation"] = cancellation
        protected = protect_glossary(transformed_markdown, translation_glossary)
        transformed_markdown = translate_markdown_offline(
            protected.text,
            request.offline_translation_language,
            **translation_arguments,
        )
        transformed_markdown = protected.restore(transformed_markdown)
        announce_translation_stage()
        check_cancelled(cancellation)
        transformed_markdown = _repair_translation_warnings(
            request,
            translation_source,
            transformed_markdown,
            settings=settings,
            cancellation=cancellation,
            translation_glossary=translation_glossary,
        )
    elif offline_translation_is_redundant:
        LOGGER.info("translation_skipped already_in_target_language=true engine=offline")

    translation_quality_report = _translation_quality_report(
        request,
        translation_source,
        transformed_markdown,
    )

    # Translation is intentionally completed before review. This makes content
    # corrections target the language the user will actually read and leaves a
    # single, coherent proposal to inspect at the end of a chained workflow.
    revision_source = transformed_markdown
    revision_kinds: set[RevisionKind] = set()
    if request.review_content:
        if settings is None:
            raise AssertionError("Validated review requests always have settings.")
        _notify(on_stage, ProcessStage.REVIEWING_CONTENT)
        if _content_review_was_fused(request, effective_ai_mode, translation_source):
            transformed_markdown = _improve_selected_content(
                transformed_markdown,
                settings,
                request,
                on_progress,
                cancellation,
                work_checkpoints,
                semantic_document=analyze_markdown(transformed_markdown),
                pdf_quality_report=pdf_quality_report,
            )
        else:
            transformed_markdown = _improve_with_checkpoints(
                transformed_markdown,
                ImprovementMode.REVIEW_CONTENT,
                settings,
                request,
                on_progress,
                cancellation,
                work_checkpoints,
            )
        if transformed_markdown != revision_source:
            revision_kinds.add(RevisionKind.CONTENT)
    if request.review_structure:
        if settings is None:
            raise AssertionError("Validated review requests always have settings.")
        _notify(on_stage, ProcessStage.ORGANIZING_STRUCTURE)
        transformed_markdown = _improve_with_checkpoints(
            transformed_markdown,
            ImprovementMode.REVIEW_STRUCTURE,
            settings,
            request,
            on_progress,
            cancellation,
            work_checkpoints,
        )
        revision_kinds.add(RevisionKind.STRUCTURE)
    revision_candidate = (
        build_revision_draft(
            revision_source,
            transformed_markdown,
            kinds=frozenset(revision_kinds),
        )
        if revision_kinds
        else None
    )
    revision_draft = (
        revision_candidate
        if revision_candidate is not None and revision_candidate.changes
        else None
    )

    published_markdown = revision_source if revision_draft is not None else transformed_markdown
    review_required = (
        _review_is_required(
            revision_draft,
            pdf_quality_report,
            translation_quality_report,
            bool(preserved_translation_chunks),
        )
        or generated_epub
    )
    public_markdown = (
        strip_pdf_page_markers(published_markdown)
        if source_path.suffix.lower() == ".pdf" and not review_required
        else published_markdown
    )

    if request.output_format is OutputFormat.TEXT:
        check_cancelled(cancellation)
        _notify(on_stage, ProcessStage.WRITING)
        integrity = text_integrity_capture(public_markdown, markdown=False)
        final_path = write_text_output(
            source_path,
            public_markdown,
            request.output_directory,
            validate_staged=integrity,
        )
        _notify(on_stage, ProcessStage.COMPLETED)
        _finish_work_checkpoints(
            work_checkpoints,
            settings=settings,
            root=work_checkpoint_root,
            cleanup_allowed=revision_draft is None,
        )
        _finish_work_checkpoints(
            pdf_checkpoints,
            settings=settings,
            root=work_checkpoint_root,
            cleanup_allowed=revision_draft is None,
        )
        return ProcessResult(
            final_path=final_path,
            review_original_path=source_path,
            translation_quality_report=translation_quality_report,
            preserved_translation_chunks=tuple(preserved_translation_chunks),
            revision_draft=revision_draft,
            review_markdown=transformed_markdown,
            review_required=review_required,
            final_integrity_report=integrity.report,
            front_matter_blocks=semantic_document.front_matter_blocks,
            toc_blocks=semantic_document.toc_blocks,
            terminology_terms=len(semantic_document.terms),
        )

    if generated_epub:
        check_cancelled(cancellation)
        _notify(on_stage, ProcessStage.BUILDING_EPUB)
        target_language = request.offline_translation_language or request.target_language
        language_code = resolve_language_code(target_language)
        if language_code is None:
            language_code = detect_language_code(transformed_markdown, minimum_letters=80) or "und"
        epub_resources = converted_resources
        cover_resource_path = None if request.epub_remove_cover else source_cover_path
        if request.epub_cover_path is not None:
            cover_resource = _read_epub_cover(request.epub_cover_path)
            epub_resources = (
                *(
                    resource
                    for resource in epub_resources
                    if resource.relative_path != cover_resource.relative_path
                ),
                cover_resource,
            )
            cover_resource_path = cover_resource.relative_path
        elif request.epub_first_page_cover:
            page_number = resolved_page_range.first_page if resolved_page_range is not None else 1
            cover_resource = ConvertedResource(
                PurePosixPath("cover/first-page.jpg"),
                render_pdf_page_cover(source_path, page_number),
                "image/jpeg",
            )
            epub_resources = (*epub_resources, cover_resource)
            cover_resource_path = cover_resource.relative_path
        epub_metadata = EpubBookMetadata(
            request.epub_title or source_path.stem,
            language_code,
            request.epub_author,
            cover_resource_path,
        )
        built_epub = build_epub(
            published_markdown,
            epub_resources,
            epub_metadata,
            cancellation=cancellation,
        )
        check_cancelled(cancellation)
        _notify(on_stage, ProcessStage.WRITING)
        integrity = binary_integrity_capture(
            built_epub.content,
            format_label="EPUB",
            validate_container=_validate_epub_path,
            ledger=(
                built_epub.integrity_report.ledger
                if built_epub.integrity_report is not None
                else IntegrityLedger()
            ),
        )
        final_path = write_epub_output(
            source_path,
            built_epub.content,
            request.output_directory,
            output_stem=output_stem,
            language_code=(
                language_code
                if request.offline_translation_language is not None
                or request.improvement_mode
                in {ImprovementMode.TRANSLATE, ImprovementMode.CLEAN_AND_TRANSLATE}
                else None
            ),
            validate_staged=integrity,
        )
        _notify(on_stage, ProcessStage.COMPLETED)
        _finish_work_checkpoints(
            work_checkpoints,
            settings=settings,
            root=work_checkpoint_root,
            cleanup_allowed=revision_draft is None,
        )
        _finish_work_checkpoints(
            pdf_checkpoints,
            settings=settings,
            root=work_checkpoint_root,
            cleanup_allowed=revision_draft is None,
        )
        return ProcessResult(
            final_path=final_path,
            review_original_path=source_path,
            problematic_pdf_pages=problematic_pdf_pages,
            pdf_quality_report=pdf_quality_report,
            exhaustive_pdf_ocr_used=request.force_pdf_ocr,
            translation_quality_report=translation_quality_report,
            preserved_translation_chunks=tuple(preserved_translation_chunks),
            preserved_images=built_epub.resource_count,
            epub_chapters=built_epub.chapter_count,
            revision_draft=revision_draft,
            revision_resources=epub_resources,
            revision_epub_metadata=epub_metadata,
            review_markdown=transformed_markdown,
            review_required=review_required,
            final_integrity_report=merge_integrity_reports(
                built_epub.integrity_report,
                integrity.report,
            ),
            front_matter_blocks=semantic_document.front_matter_blocks,
            toc_blocks=semantic_document.toc_blocks,
            terminology_terms=len(semantic_document.terms),
        )

    if (
        request.improvement_mode is not None
        or request.offline_translation_language is not None
        or request.review_content
        or request.review_structure
    ):
        _notify(on_stage, ProcessStage.WRITING)
        integrity = text_integrity_capture(public_markdown, markdown=True)
        final_path, raw_path = write_improvement_outputs(
            source_path,
            markdown,
            public_markdown,
            keep_raw=source_path.suffix.lower() in CONVERSION_REQUIRED_EXTENSIONS,
            output_directory=request.output_directory,
            output_stem=output_stem,
            resources=converted_resources,
            image_output_directory=request.image_output_directory,
            validate_staged=integrity,
        )
        _notify(on_stage, ProcessStage.COMPLETED)
        _finish_work_checkpoints(
            work_checkpoints,
            settings=settings,
            root=work_checkpoint_root,
            cleanup_allowed=revision_draft is None,
        )
        _finish_work_checkpoints(
            pdf_checkpoints,
            settings=settings,
            root=work_checkpoint_root,
            cleanup_allowed=revision_draft is None,
        )
        materialized_revision = _materialize_markdown_revision(
            revision_draft,
            final_path,
            request.image_output_directory,
            bool(converted_resources),
        )
        return ProcessResult(
            final_path=final_path,
            raw_markdown_path=raw_path,
            review_original_path=raw_path if raw_path is not None else source_path,
            problematic_pdf_pages=problematic_pdf_pages,
            pdf_quality_report=pdf_quality_report,
            exhaustive_pdf_ocr_used=request.force_pdf_ocr,
            translation_quality_report=translation_quality_report,
            preserved_translation_chunks=tuple(preserved_translation_chunks),
            preserved_images=len(converted_resources),
            revision_draft=materialized_revision,
            review_markdown=(
                materialized_revision.proposed_markdown
                if materialized_revision is not None
                else final_path.read_text(encoding="utf-8")
            ),
            review_required=review_required,
            final_integrity_report=integrity.report,
            front_matter_blocks=semantic_document.front_matter_blocks,
            toc_blocks=semantic_document.toc_blocks,
            terminology_terms=len(semantic_document.terms),
        )

    check_cancelled(cancellation)
    _notify(on_stage, ProcessStage.WRITING)
    integrity = text_integrity_capture(public_markdown, markdown=True)
    final_path = write_conversion_output(
        source_path,
        public_markdown,
        output_directory=request.output_directory,
        output_stem=output_stem,
        resources=converted_resources,
        image_output_directory=request.image_output_directory,
        validate_staged=integrity,
    )
    _notify(on_stage, ProcessStage.COMPLETED)
    _finish_work_checkpoints(
        work_checkpoints,
        settings=settings,
        root=work_checkpoint_root,
        cleanup_allowed=revision_draft is None,
    )
    _finish_work_checkpoints(
        pdf_checkpoints,
        settings=settings,
        root=work_checkpoint_root,
        cleanup_allowed=revision_draft is None,
    )
    return ProcessResult(
        final_path=final_path,
        problematic_pdf_pages=problematic_pdf_pages,
        pdf_quality_report=pdf_quality_report,
        exhaustive_pdf_ocr_used=request.force_pdf_ocr,
        preserved_images=len(converted_resources),
        review_markdown=final_path.read_text(encoding="utf-8"),
        review_required=_review_is_required(None, pdf_quality_report, None),
        final_integrity_report=integrity.report,
        front_matter_blocks=semantic_document.front_matter_blocks,
        toc_blocks=semantic_document.toc_blocks,
        terminology_terms=len(semantic_document.terms),
    )


def _process_epub_translation(
    request: ProcessRequest,
    on_stage: StageCallback | None,
    on_progress: ProgressCallback | None,
    *,
    settings: AppSettings | None,
    cancellation: CancellationToken | None,
    epub_checkpoint_root: Path | None,
) -> ProcessResult:
    target_language = request.offline_translation_language or request.target_language
    language_code = resolve_language_code(target_language)
    if language_code is None:
        raise RequestValidationError("El idioma de destino del EPUB no está soportado.")

    source_language_code = resolve_language_code(inspect_epub_package(request.source_path).language)
    if source_language_code == language_code:
        LOGGER.info(
            "translation_skipped already_in_target_language=true engine=%s format=epub",
            "offline" if request.offline_translation_language is not None else "ai",
        )
        return _process_epub_personalization(
            request,
            on_stage,
            cancellation=cancellation,
        )

    source_semantic = analyze_markdown(
        convert_epub(request.source_path, cancellation=cancellation).markdown
    )
    translation_glossary = _combined_translation_glossary(
        request.glossary,
        source_semantic.terms,
    )
    effective_ai_mode = _effective_ai_improvement_mode(request)
    if request.offline_translation_language is not None:
        _notify(on_stage, ProcessStage.PREPARING_TRANSLATION)
    else:
        _notify(on_stage, ProcessStage.TRANSLATING)
    checkpoints = open_epub_translation_checkpoints(
        request.source_path,
        _epub_translation_resume_key(
            request,
            settings,
            language_code,
            source_semantic.terms,
        ),
        root=epub_checkpoint_root,
    )
    translation_stage_announced = request.offline_translation_language is None

    def announce_translation_stage() -> None:
        nonlocal translation_stage_announced
        if translation_stage_announced:
            return
        translation_stage_announced = True
        _notify(on_stage, ProcessStage.TRANSLATING)

    def translate_payload(
        payload: str,
        progress: ProgressCallback | None,
        token: CancellationToken | None,
    ) -> str:
        translated = payload
        if effective_ai_mode is not None:
            if settings is None:
                raise AssertionError("Validated improvement requests always have settings.")
            protected = protect_glossary(translated, translation_glossary)
            translated = improve_markdown(
                protected.text,
                effective_ai_mode,
                settings,
                request.target_language,
                on_progress=progress,
                cancellation=token,
            )
            translated = protected.restore(translated)
        if request.offline_translation_language is not None:
            protected = protect_glossary(translated, translation_glossary)
            translated = translate_markdown_offline(
                protected.text,
                request.offline_translation_language,
                on_progress=progress,
                on_engine_ready=announce_translation_stage,
                cancellation=token,
            )
            translated = protected.restore(translated)
        return translated

    def repair_payload(
        source_payload: str,
        translated_payload: str,
        source_language_code: str | None,
        token: CancellationToken | None,
    ) -> str:
        return _repair_translation_warnings(
            request,
            source_payload,
            translated_payload,
            settings=settings,
            cancellation=token,
            source_language_code=source_language_code,
            translation_glossary=translation_glossary,
        )

    translated_epub = translate_epub(
        request.source_path,
        language_code,
        translate_payload,
        on_progress=on_progress,
        cancellation=cancellation,
        load_checkpoint=checkpoints.load,
        save_checkpoint=checkpoints.save,
        repair_text=repair_payload,
    )
    translated_content = replace_epub_metadata(
        translated_epub.content,
        title=request.epub_title,
        author=request.epub_author,
        cancellation=cancellation,
    )
    announce_translation_stage()
    check_cancelled(cancellation)
    _notify(on_stage, ProcessStage.WRITING)
    translated_integrity = binary_integrity_capture(
        translated_content,
        format_label="EPUB",
        validate_container=_validate_preserved_epub_path,
    )
    final_path = write_epub_translation_output(
        request.source_path,
        translated_content,
        translated_epub.language_code,
        request.output_directory,
        validate_staged=translated_integrity,
    )
    normalized_document = None
    normalized_metadata = None
    normalized_chapters = 0
    normalized_resource_count = 0
    normalized_integrity: FinalIntegrityReport | None = None
    normalize_output = (
        not request.preserve_styles
        or not request.include_images
        or request.epub_cover_path is not None
        or request.epub_remove_cover
    )
    prepare_editor = True
    if prepare_editor:
        # The exact translated package is retained when both resources and
        # compatible styles are requested. Normalization is otherwise the
        # deliberate contract: it can reliably omit images and produces the
        # editable representation required by a human quality review.
        converted_document = convert_epub(final_path, cancellation=cancellation)
        package_metadata = inspect_epub_package(final_path)
        normalized_markdown = (
            converted_document.markdown
            if request.include_images
            else without_markdown_images(converted_document.markdown)
        )
        normalized_resources = (
            converted_document.resources
            if request.include_images
            else tuple(
                resource
                for resource in converted_document.resources
                if resource.relative_path == package_metadata.cover_path
            )
        )
        cover_resource_path = package_metadata.cover_path
        if package_metadata.cover_path is not None and (
            request.epub_cover_path is not None or request.epub_remove_cover
        ):
            normalized_markdown = without_markdown_resource(
                normalized_markdown,
                package_metadata.cover_path,
            )
            normalized_resources = tuple(
                resource
                for resource in normalized_resources
                if resource.relative_path != package_metadata.cover_path
            )
        if request.epub_cover_path is not None:
            cover_resource = _read_epub_cover(request.epub_cover_path)
            normalized_resources = (
                *(
                    resource
                    for resource in normalized_resources
                    if resource.relative_path != cover_resource.relative_path
                ),
                cover_resource,
            )
            cover_resource_path = cover_resource.relative_path
        elif request.epub_remove_cover:
            cover_resource_path = None
        normalized_document = replace(
            converted_document,
            markdown=normalized_markdown,
            resources=normalized_resources,
        )
        normalized_resource_count = len(normalized_resources)
        normalized_metadata = EpubBookMetadata(
            request.epub_title or package_metadata.title or request.source_path.stem,
            package_metadata.language or translated_epub.language_code,
            request.epub_author
            if request.epub_author is not None
            else ", ".join(package_metadata.authors) or None,
            cover_resource_path,
        )
        if normalize_output:
            built = build_epub(
                normalized_document.markdown,
                normalized_document.resources,
                normalized_metadata,
                cancellation=cancellation,
            )
            normalized_capture = binary_integrity_capture(
                built.content,
                format_label="EPUB",
                validate_container=_validate_epub_path,
                ledger=(
                    built.integrity_report.ledger
                    if built.integrity_report is not None
                    else IntegrityLedger()
                ),
            )
            replace_binary_output(
                final_path,
                built.content,
                validate_staged=normalized_capture,
            )
            normalized_chapters = built.chapter_count
            normalized_resource_count = built.resource_count
            normalized_integrity = merge_integrity_reports(
                built.integrity_report,
                normalized_capture.report,
            )
    _finish_epub_checkpoints(
        checkpoints,
        settings=settings,
        root=epub_checkpoint_root,
    )
    _notify(on_stage, ProcessStage.COMPLETED)
    review_required = True
    return ProcessResult(
        final_path=final_path,
        review_original_path=request.source_path,
        epub_translation_parts=translated_epub.translation_parts,
        epub_resumed_parts=translated_epub.resumed_parts,
        epub_checkpoint_degraded=translated_epub.checkpoint_degraded,
        translation_quality_report=translated_epub.quality_report,
        preserved_images=normalized_resource_count,
        epub_chapters=normalized_chapters,
        revision_resources=(
            normalized_document.resources if normalized_document is not None else ()
        ),
        revision_epub_metadata=normalized_metadata,
        review_markdown=(normalized_document.markdown if normalized_document is not None else None),
        review_required=review_required,
        final_integrity_report=(
            normalized_integrity
            if normalized_integrity is not None
            else translated_integrity.report
        ),
        front_matter_blocks=source_semantic.front_matter_blocks,
        toc_blocks=source_semantic.toc_blocks,
        terminology_terms=len(source_semantic.terms),
    )


def _process_epub_personalization(
    request: ProcessRequest,
    on_stage: StageCallback | None,
    *,
    cancellation: CancellationToken | None,
) -> ProcessResult:
    """Create an editable final-review result without requiring a prior transform."""

    _notify(on_stage, ProcessStage.READING)
    converted = convert_epub(request.source_path, cancellation=cancellation)
    package = inspect_epub_package(request.source_path)
    markdown = (
        converted.markdown
        if request.include_images
        else without_markdown_images(converted.markdown)
    )
    resources = (
        converted.resources
        if request.include_images
        else tuple(
            resource
            for resource in converted.resources
            if resource.relative_path == package.cover_path
        )
    )
    metadata = EpubBookMetadata(
        package.title or request.source_path.stem,
        package.language or "und",
        ", ".join(package.authors) or None,
        package.cover_path,
    )
    semantic_document = analyze_markdown(markdown)
    check_cancelled(cancellation)
    _notify(on_stage, ProcessStage.WRITING)
    source_content = request.source_path.read_bytes()
    integrity = binary_integrity_capture(
        source_content,
        format_label="EPUB",
        validate_container=_validate_preserved_epub_path,
        ledger=IntegrityLedger(
            blocks=len(semantic_document.blocks),
            headings=sum(block.role is SemanticRole.HEADING for block in semantic_document.blocks),
            images=sum(resource.media_type.startswith("image/") for resource in resources),
            resources=len(resources),
        ),
    )
    final_path = write_epub_output(
        request.source_path,
        source_content,
        request.output_directory,
        validate_staged=integrity,
    )
    _notify(on_stage, ProcessStage.COMPLETED)
    return ProcessResult(
        final_path=final_path,
        review_original_path=request.source_path,
        preserved_images=sum(resource.media_type.startswith("image/") for resource in resources),
        revision_resources=resources,
        revision_epub_metadata=metadata,
        review_markdown=markdown,
        review_required=True,
        final_integrity_report=integrity.report,
        front_matter_blocks=semantic_document.front_matter_blocks,
        toc_blocks=semantic_document.toc_blocks,
        terminology_terms=len(semantic_document.terms),
    )


def _improve_with_checkpoints(
    markdown: str,
    mode: ImprovementMode,
    settings: AppSettings,
    request: ProcessRequest,
    on_progress: ProgressCallback | None,
    cancellation: CancellationToken | None,
    checkpoints: WorkCheckpoints | None,
    *,
    plain_text: bool | None = None,
) -> str:
    """Apply one ordered review pass with the same resumable chunk contract."""
    arguments: _ImprovementArguments = {
        "on_progress": on_progress,
        "plain_text": (
            request.output_format is OutputFormat.TEXT if plain_text is None else plain_text
        ),
    }
    if cancellation is not None:
        arguments["cancellation"] = cancellation
    if checkpoints is not None:
        arguments["load_checkpoint"] = checkpoints.load
        arguments["save_checkpoint"] = checkpoints.save
    return improve_markdown(
        markdown,
        mode,
        settings,
        **arguments,
    )


def _effective_ai_improvement_mode(request: ProcessRequest) -> ImprovementMode | None:
    """Fuse AI translation and requested correction into one conservative pass."""
    if request.review_content and request.improvement_mode is ImprovementMode.TRANSLATE:
        return ImprovementMode.CLEAN_AND_TRANSLATE
    return request.improvement_mode


def _content_review_was_fused(
    request: ProcessRequest,
    effective_mode: ImprovementMode | None,
    translation_source: str | None,
) -> bool:
    return bool(
        request.review_content
        and translation_source is not None
        and request.offline_translation_language is None
        and effective_mode is ImprovementMode.CLEAN_AND_TRANSLATE
    )


def _improve_selected_content(
    markdown: str,
    settings: AppSettings,
    request: ProcessRequest,
    on_progress: ProgressCallback | None,
    cancellation: CancellationToken | None,
    checkpoints: WorkCheckpoints | None,
    *,
    semantic_document: SemanticDocument,
    pdf_quality_report: PdfQualityReport | None,
) -> str:
    """Run a second correction only where extraction quality supplied evidence."""
    problem_pages = (
        {issue.page_number for issue in pdf_quality_report.issues}
        if pdf_quality_report is not None
        else set()
    )
    selected = tuple(
        block
        for block in semantic_document.blocks
        if block.role
        not in {
            SemanticRole.PROVENANCE,
            SemanticRole.CODE,
            SemanticRole.IMAGE,
        }
        and (block.page_number in problem_pages or _contains_conversion_damage(block.markdown))
    )
    if not selected:
        if on_progress is not None:
            on_progress(1, 1)
        return markdown

    replacements: dict[str, str] = {}
    total = len(selected)
    for current, block in enumerate(selected, start=1):
        check_cancelled(cancellation)
        if on_progress is not None:
            on_progress(current, total)
        improved = _improve_with_checkpoints(
            block.markdown,
            ImprovementMode.REVIEW_CONTENT,
            settings,
            request,
            None,
            cancellation,
            checkpoints,
        )
        if improved != block.markdown:
            replacements[block.identifier] = improved
    return "".join(
        replacements.get(block.identifier, block.markdown) for block in semantic_document.blocks
    )


def _contains_conversion_damage(markdown: str) -> bool:
    visible = re.sub(r"<!--[\s\S]*?-->", "", markdown)
    return bool(
        "\ufffd" in visible
        or re.search(r"(?i)\b(?:aviso|warning)\s+OCR\b", visible)
        or re.search(r"\b(?:[^\W\d_]\s+){5,}[^\W\d_]\b", visible)
        or re.search(r"(?i)\b([^\W\d_]{3,})(?:\s+\1){2,}\b", visible)
    )


def _process_docx_transformation(
    request: ProcessRequest,
    on_stage: StageCallback | None,
    on_progress: ProgressCallback | None,
    *,
    settings: AppSettings | None,
    cancellation: CancellationToken | None,
    work_checkpoint_root: Path | None,
) -> ProcessResult:
    """Transform Word text while retaining the original OOXML package and resources."""
    work_checkpoints = _open_general_work_checkpoints(
        request,
        settings,
        root=work_checkpoint_root,
    )
    _notify(on_stage, ProcessStage.READING)
    revision_baseline_payload: str | None = None
    preserved_translation_chunks: list[int] = []
    translation_performed = False
    semantic_document: SemanticDocument | None = None
    effective_ai_mode = _effective_ai_improvement_mode(request)

    def transform_payload(payload: str) -> str:
        nonlocal revision_baseline_payload, semantic_document, translation_performed
        transformed = payload
        semantic_document = analyze_markdown(payload)
        translation_glossary = _combined_translation_glossary(
            request.glossary,
            semantic_document.terms,
        )
        translation_source: str | None = None
        ai_translation_is_redundant = (
            request.improvement_mode is ImprovementMode.TRANSLATE
            and _translation_is_redundant(
                transformed,
                request.target_language,
            )
        )
        if request.improvement_mode is not None and not ai_translation_is_redundant:
            if settings is None:
                raise AssertionError("Validated improvement requests always have settings.")
            if effective_ai_mode is None:
                raise AssertionError("An enabled AI transformation always has an effective mode.")
            _notify(
                on_stage,
                (
                    ProcessStage.TRANSLATING
                    if request.improvement_mode
                    in {ImprovementMode.TRANSLATE, ImprovementMode.CLEAN_AND_TRANSLATE}
                    else ProcessStage.IMPROVING
                ),
            )
            if request.improvement_mode in {
                ImprovementMode.TRANSLATE,
                ImprovementMode.CLEAN_AND_TRANSLATE,
            }:
                translation_source = transformed
                translation_performed = True
            protected = (
                protect_glossary(transformed, translation_glossary)
                if translation_source is not None
                else None
            )
            arguments: _ImprovementArguments = {"on_progress": on_progress}
            arguments["plain_text"] = True
            improvement_parameters = inspect.signature(improve_markdown).parameters
            if "on_translation_preserved" in improvement_parameters:
                arguments["on_translation_preserved"] = lambda current, _total: (
                    preserved_translation_chunks.append(current)
                )
            if cancellation is not None:
                arguments["cancellation"] = cancellation
            if work_checkpoints is not None and "load_checkpoint" in improvement_parameters:
                arguments["load_checkpoint"] = work_checkpoints.load
                arguments["save_checkpoint"] = work_checkpoints.save
            transformed = improve_markdown(
                protected.text if protected is not None else transformed,
                effective_ai_mode,
                settings,
                request.target_language,
                **arguments,
            )
            if protected is not None:
                transformed = protected.restore(transformed)
        elif ai_translation_is_redundant:
            LOGGER.info("translation_skipped already_in_target_language=true engine=ai")
        offline_translation_is_redundant = (
            request.offline_translation_language is not None
            and _translation_is_redundant(
                transformed,
                request.offline_translation_language,
            )
        )
        if (
            request.offline_translation_language is not None
            and not offline_translation_is_redundant
        ):
            _notify(on_stage, ProcessStage.PREPARING_TRANSLATION)
            translation_source = transformed
            translation_performed = True
            translation_announced = False

            def announce_translation() -> None:
                nonlocal translation_announced
                if not translation_announced:
                    translation_announced = True
                    _notify(on_stage, ProcessStage.TRANSLATING)

            protected = protect_glossary(transformed, translation_glossary)
            transformed = translate_markdown_offline(
                protected.text,
                request.offline_translation_language,
                on_progress=on_progress,
                on_engine_ready=announce_translation,
                cancellation=cancellation,
            )
            announce_translation()
            transformed = protected.restore(transformed)
        elif offline_translation_is_redundant:
            LOGGER.info("translation_skipped already_in_target_language=true engine=offline")
        if translation_source is not None:
            transformed = _repair_translation_warnings(
                request,
                translation_source,
                transformed,
                settings=settings,
                cancellation=cancellation,
                translation_glossary=translation_glossary,
            )
        revision_baseline_payload = transformed
        if request.review_content and not _content_review_was_fused(
            request,
            effective_ai_mode,
            translation_source,
        ):
            if settings is None:
                raise AssertionError("Validated review requests always have settings.")
            _notify(on_stage, ProcessStage.REVIEWING_CONTENT)
            transformed = _improve_with_checkpoints(
                transformed,
                ImprovementMode.REVIEW_CONTENT,
                settings,
                request,
                on_progress,
                cancellation,
                work_checkpoints,
                plain_text=True,
            )
        return transformed

    transformed_docx = transform_docx(
        request.source_path,
        transform_payload,
        cancellation=cancellation,
    )
    baseline_paragraphs = (
        split_docx_payload(revision_baseline_payload, transformed_docx.paragraph_count)
        if revision_baseline_payload is not None
        else transformed_docx.source_paragraphs
    )
    baseline_text = "\n\n".join(baseline_paragraphs)
    report = _translation_quality_report(
        request,
        transformed_docx.source_text if translation_performed else None,
        baseline_text if request.review_content else transformed_docx.transformed_text,
    )
    revision_candidate = (
        build_revision_draft(
            baseline_text,
            transformed_docx.transformed_text,
            kinds=frozenset({RevisionKind.CONTENT}),
        )
        if request.review_content
        else None
    )
    revision_draft = (
        revision_candidate
        if revision_candidate is not None and revision_candidate.changes
        else None
    )
    check_cancelled(cancellation)
    _notify(on_stage, ProcessStage.WRITING)
    output_content = (
        rebuild_docx_with_paragraphs(request.source_path, baseline_paragraphs)
        if revision_draft is not None
        else transformed_docx.content
    )
    integrity = binary_integrity_capture(
        output_content,
        format_label="DOCX",
        validate_container=validate_docx_container,
        ledger=IntegrityLedger(
            blocks=len(baseline_paragraphs)
            if revision_draft is not None
            else transformed_docx.paragraph_count,
            paragraphs=len(baseline_paragraphs)
            if revision_draft is not None
            else transformed_docx.paragraph_count,
            resources=transformed_docx.preserved_resources,
        ),
    )
    final_path = write_docx_output(
        request.source_path,
        output_content,
        request.output_directory,
        validate_staged=integrity,
    )
    _notify(on_stage, ProcessStage.COMPLETED)
    _finish_work_checkpoints(
        work_checkpoints,
        settings=settings,
        root=work_checkpoint_root,
        cleanup_allowed=revision_draft is None,
    )
    return ProcessResult(
        final_path=final_path,
        review_original_path=request.source_path,
        translation_quality_report=report,
        preserved_translation_chunks=tuple(preserved_translation_chunks),
        preserved_images=transformed_docx.preserved_resources,
        revision_draft=revision_draft,
        revision_docx_original_paragraphs=(
            baseline_paragraphs if revision_draft is not None else ()
        ),
        revision_docx_proposed_paragraphs=(
            transformed_docx.transformed_paragraphs if revision_draft is not None else ()
        ),
        review_markdown=transformed_docx.transformed_text,
        review_required=_review_is_required(
            revision_draft,
            None,
            report,
            bool(preserved_translation_chunks),
        ),
        final_integrity_report=integrity.report,
        front_matter_blocks=(
            semantic_document.front_matter_blocks if semantic_document is not None else 0
        ),
        toc_blocks=semantic_document.toc_blocks if semantic_document is not None else 0,
        terminology_terms=len(semantic_document.terms) if semantic_document is not None else 0,
    )


def _translation_quality_report(
    request: ProcessRequest,
    source: str | None,
    translated: str,
) -> TranslationQualityReport | None:
    if source is None:
        return None
    target_language = request.offline_translation_language or request.target_language
    target_language_code = resolve_language_code(target_language)
    if target_language_code is None:
        raise AssertionError("A completed translation always has a supported target language.")
    return build_translation_quality_report(
        source,
        translated,
        target_language=target_language_code,
    )


def _translation_is_redundant(markdown: str, target_language: str | None) -> bool:
    """Return true only when both source and target languages are confidently known."""

    target_language_code = resolve_language_code(target_language)
    if target_language_code is None:
        return False
    source_language_code = detect_language_code(markdown)
    return source_language_code is not None and source_language_code == target_language_code


def _review_is_required(
    revision_draft: RevisionDraft | None,
    pdf_report: PdfQualityReport | None,
    translation_report: TranslationQualityReport | None,
    unsafe_translation_preserved: bool = False,
) -> bool:
    """Distinguish optional advice from issues needing an explicit decision."""
    return bool(
        revision_draft is not None
        or (pdf_report is not None and any(issue.blocking for issue in pdf_report.issues))
        or unsafe_translation_preserved
    )


def _repair_translation_warnings(
    request: ProcessRequest,
    source: str,
    translated: str,
    *,
    settings: AppSettings | None,
    cancellation: CancellationToken | None,
    source_language_code: str | None = None,
    translation_glossary: tuple[GlossaryEntry, ...] | None = None,
) -> str:
    """Retry aligned blocks with source-language residue or a critical fidelity warning."""

    target_language = request.offline_translation_language or request.target_language
    target_language_code = resolve_language_code(target_language)
    if target_language_code is None:
        raise AssertionError("A validated translation always has a supported target language.")
    source_language_code = source_language_code or detect_language_code(
        source,
        minimum_letters=80,
    )
    effective_glossary = request.glossary if translation_glossary is None else translation_glossary

    def translate_segment(source_segment: str, current_segment: str) -> str:
        check_cancelled(cancellation)
        try:
            protected = protect_glossary(source_segment, effective_glossary)
            if request.offline_translation_language is not None:
                repaired = translate_markdown_offline(
                    protected.text,
                    request.offline_translation_language,
                    source_language_code=source_language_code,
                    cancellation=cancellation,
                )
                return protected.restore(repaired)
            if settings is None:
                raise AssertionError("Validated AI translations always have settings.")
            repaired = improve_markdown(
                protected.text,
                ImprovementMode.TRANSLATE,
                settings,
                request.target_language,
                cancellation=cancellation,
                plain_text=request.output_format in {OutputFormat.TEXT, OutputFormat.DOCX},
                source_language_code=source_language_code,
            )
            return protected.restore(repaired)
        except ProcessingCancelledError:
            raise
        except ParsezenError as exc:
            LOGGER.warning(
                "translation_source_text_repair_failed error_type=%s",
                type(exc).__name__,
            )
            return current_segment

    repair = repair_untranslated_source_text(
        source,
        translated,
        source_language=source_language_code,
        target_language=target_language_code,
        translate_segment=translate_segment,
        preserve_paragraphs=(
            _effective_ai_improvement_mode(request) is not ImprovementMode.CLEAN_AND_TRANSLATE
        ),
    )
    if repair.attempted_segments:
        LOGGER.info(
            "translation_source_text_repair_completed attempted=%d accepted=%d",
            repair.attempted_segments,
            repair.repaired_segments,
        )
    if source_language_code is None or source_language_code == target_language_code:
        return repair.translated
    return restore_changed_third_language_headings(
        source,
        repair.translated,
        source_language=source_language_code,
        target_language=target_language_code,
    )


def _epub_translation_resume_key(
    request: ProcessRequest,
    settings: AppSettings | None,
    language_code: str,
    terminology: tuple[DocumentTerm, ...] = (),
) -> str:
    """Identify settings that can materially change translated EPUB text."""

    mode = request.improvement_mode.value if request.improvement_mode is not None else "none"
    model = settings.model if settings is not None else None
    context_window = settings.context_window if settings is not None else None
    return repr(
        (
            "epub-translation-v3",
            language_code,
            mode,
            request.offline_translation_language is not None,
            model,
            context_window,
            glossary_fingerprint(request.glossary),
            terminology_fingerprint(terminology),
        )
    )


def _combined_translation_glossary(
    glossary: tuple[GlossaryEntry, ...],
    terminology: tuple[DocumentTerm, ...],
) -> tuple[GlossaryEntry, ...]:
    """Keep repeated proper terms stable while preserving explicit user choices."""
    normalized = list(validate_glossary(glossary))
    seen = {entry.source.casefold() for entry in normalized}
    inferred_occurrences = 0
    for term in terminology:
        if len(normalized) >= MAX_GLOSSARY_ENTRIES:
            break
        key = term.text.casefold()
        if key in seen:
            continue
        if (
            term.occurrences <= 0
            or inferred_occurrences + term.occurrences > _MAX_INFERRED_TERMINOLOGY_OCCURRENCES
        ):
            continue
        normalized.append(GlossaryEntry(term.text, term.text))
        seen.add(key)
        inferred_occurrences += term.occurrences
    return tuple(normalized)


def _open_general_work_checkpoints(
    request: ProcessRequest,
    settings: AppSettings | None,
    *,
    root: Path | None,
) -> WorkCheckpoints | None:
    """Open checkpoints only when a workflow contains expensive resumable work."""
    if (
        request.source_path.suffix.lower() != ".pdf"
        and request.improvement_mode is None
        and not request.review_content
        and not request.review_structure
    ):
        return None
    model = settings.model if settings is not None else None
    context_window = settings.context_window if settings is not None else None
    page_range = (
        (request.pdf_page_range.first_page, request.pdf_page_range.last_page)
        if request.pdf_page_range is not None
        else None
    )
    resume_key = repr(
        (
            "general-work-v3",
            request.convert_to_markdown,
            request.improvement_mode.value if request.improvement_mode is not None else None,
            request.review_content,
            request.review_structure,
            request.target_language,
            request.offline_translation_language,
            page_range,
            request.force_pdf_ocr,
            request.output_format.value,
            model,
            context_window,
        )
    )
    return open_work_checkpoints(request.source_path, resume_key, root=root)


def _open_pdf_conversion_checkpoints(
    request: ProcessRequest,
    *,
    root: Path | None,
) -> WorkCheckpoints | None:
    """Share costly page/OCR work across Markdown, EPUB and later text transformations."""
    if request.source_path.suffix.lower() != ".pdf" or not request.convert_to_markdown:
        return None
    # Native extraction and OCR payloads are keyed again by their absolute page
    # number. Keeping the directory independent from the selected interval lets
    # the representative early check feed the later full run without ever
    # sharing data between different source bytes or OCR strategies.
    resume_key = repr(("pdf-conversion-v2", request.force_pdf_ocr))
    return open_work_checkpoints(request.source_path, resume_key, root=root)


def _finish_work_checkpoints(
    checkpoints: WorkCheckpoints | None,
    *,
    settings: AppSettings | None,
    root: Path | None,
    cleanup_allowed: bool,
) -> None:
    """Apply the configured encrypted-cache policy after a successful output."""
    if checkpoints is None:
        return
    retention_days = (
        settings.checkpoint_retention_days
        if settings is not None
        else AppSettings().checkpoint_retention_days
    )
    if retention_days == 0:
        if cleanup_allowed:
            checkpoints.clear()
        return
    prune_work_checkpoint_cache(root=root, max_age_days=retention_days)


def _finish_epub_checkpoints(
    checkpoints: EpubTranslationCheckpoints,
    *,
    settings: AppSettings | None,
    root: Path | None,
) -> None:
    """Retain only encrypted resumable EPUB work for the requested period."""
    retention_days = (
        settings.checkpoint_retention_days
        if settings is not None
        else AppSettings().checkpoint_retention_days
    )
    if retention_days == 0:
        checkpoints.clear()
        return
    prune_epub_translation_cache(root=root, max_age_days=retention_days)


def clear_document_work_checkpoints(
    request: ProcessRequest,
    settings: AppSettings | None,
    *,
    root: Path | None = None,
) -> None:
    """Discard resumable work for one exact request after an explicit cancellation."""
    checkpoints = _open_general_work_checkpoints(request, settings, root=root)
    if checkpoints is not None:
        checkpoints.clear()
    pdf_checkpoints = _open_pdf_conversion_checkpoints(request, root=root)
    if pdf_checkpoints is not None:
        pdf_checkpoints.clear()


def clear_general_work_checkpoints(
    request: ProcessRequest,
    settings: AppSettings | None,
    *,
    root: Path | None = None,
) -> None:
    """Discard transformed sample text while retaining reusable PDF page work."""

    checkpoints = _open_general_work_checkpoints(request, settings, root=root)
    if checkpoints is not None:
        checkpoints.clear()


def validate_process_request(request: ProcessRequest, settings: AppSettings | None) -> None:
    """Validate one complete request before a worker or batch is started."""
    _validate_request(request, settings)
    if request.pdf_page_range is not None:
        # Resolve the selection during batch preflight so an impossible range in a
        # later PDF cannot fail only after earlier documents have been processed.
        resolve_pdf_page_range(request.source_path, request.pdf_page_range)
    elif request.epub_first_page_cover:
        resolve_pdf_page_range(request.source_path, PdfPageRange(1, 1))


def _validate_request(request: ProcessRequest, settings: AppSettings | None) -> None:
    _validate_source(request.source_path)
    _validate_pdf_options(request)
    _validate_translation_options(request)
    _validate_requested_action(request)
    _validate_output_format(request)
    _validate_ai_settings(request, settings)

    output_directory = request.output_directory or request.source_path.parent
    _validate_output_directory(output_directory, request.source_path)
    if (
        request.output_format is OutputFormat.MARKDOWN
        and request.image_output_directory is not None
    ):
        _validate_output_directory(request.image_output_directory, request.source_path)
        try:
            os.path.relpath(request.image_output_directory, start=output_directory)
        except ValueError as exc:
            raise RequestValidationError(
                "La carpeta de imágenes debe estar en la misma unidad que la salida Markdown."
            ) from exc
    _validate_temporary_working_space(request)


def _validate_source(source_path: Path) -> None:
    if not source_path.exists():
        raise RequestValidationError(f"No existe el documento {source_path.name}.")
    if not source_path.is_file():
        raise RequestValidationError("La entrada debe ser un documento local.")
    if source_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise RequestValidationError(
            f"El formato {source_path.suffix or '(sin extensión)'} no está soportado."
        )
    if source_path.suffix.lower() in {".txt", ".md", ".markdown"}:
        try:
            source_size = source_path.stat().st_size
        except OSError as exc:
            raise RequestValidationError(f"No se pudo leer {source_path.name}.") from exc
        if source_size > MAX_DIRECT_TEXT_BYTES:
            raise RequestValidationError(
                f"{source_path.name} supera el límite de 64 MiB para texto o Markdown."
            )


def _validate_pdf_options(request: ProcessRequest) -> None:
    source_path = request.source_path
    page_range = request.pdf_page_range
    if page_range is not None:
        if source_path.suffix.lower() != ".pdf":
            raise RequestValidationError("La selección de páginas solo se puede usar con PDF.")
        if not isinstance(page_range, PdfPageRange):
            raise RequestValidationError("La selección de páginas no es válida.")
        if (
            isinstance(page_range.first_page, bool)
            or isinstance(page_range.last_page, bool)
            or not isinstance(page_range.first_page, int)
            or not isinstance(page_range.last_page, int)
            or page_range.first_page < 1
            or page_range.last_page < page_range.first_page
        ):
            raise RequestValidationError("El rango de páginas seleccionado no es válido.")
    if not isinstance(request.force_pdf_ocr, bool):
        raise RequestValidationError("La opción de OCR exhaustivo no es válida.")
    if request.force_pdf_ocr and (
        source_path.suffix.lower() != ".pdf" or not request.convert_to_markdown
    ):
        raise RequestValidationError(
            "El análisis OCR exhaustivo solo se puede usar al convertir un PDF."
        )


def _validate_translation_options(request: ProcessRequest) -> None:
    mode = request.improvement_mode
    if not isinstance(request.review_content, bool) or not isinstance(
        request.review_structure, bool
    ):
        raise RequestValidationError("Las opciones de revisión no son válidas.")
    if mode is not None and not isinstance(mode, ImprovementMode):
        raise RequestValidationError("El modo de mejora seleccionado no es válido.")
    offline_language = request.offline_translation_language
    if offline_language is not None and (
        not isinstance(offline_language, str) or not offline_language.strip()
    ):
        raise RequestValidationError("Indica el idioma de destino para traducir offline.")
    if (
        mode in {ImprovementMode.TRANSLATE, ImprovementMode.CLEAN_AND_TRANSLATE}
        and offline_language
    ):
        raise RequestValidationError(
            "Elige traducción con IA local o traducción offline, pero no ambas."
        )
    glossary = validate_glossary(request.glossary)
    if glossary and not _request_translates(request):
        raise RequestValidationError("El glosario solo se utiliza al traducir.")


def _validate_requested_action(request: ProcessRequest) -> None:
    source_path = request.source_path
    mode = request.improvement_mode
    offline_language = request.offline_translation_language
    if (
        not request.convert_to_markdown
        and mode is None
        and offline_language is None
        and not request.review_content
        and not request.review_structure
        and not _is_epub_personalization(request)
    ):
        raise RequestValidationError(
            "Activa la conversión o la mejora con IA local, o bien la traducción offline."
        )
    if (
        source_path.suffix.lower() in CONVERSION_REQUIRED_EXTENSIONS
        and not request.convert_to_markdown
        and not _is_epub_translation(request)
        and not _is_epub_personalization(request)
        and not _is_docx_transformation(request)
    ):
        format_name = source_path.suffix.removeprefix(".").upper()
        raise RequestValidationError(
            f"Un documento {format_name} necesita convertirse antes de usar la IA."
        )


def _validate_output_format(request: ProcessRequest) -> None:
    if not isinstance(request.output_format, OutputFormat):
        raise RequestValidationError("El formato de salida seleccionado no es válido.")
    mode = request.improvement_mode
    plan = plan_workflow(
        (request.source_path.suffix,),
        request.output_format,
        options=WorkflowOptions(
            improvement_enabled=mode is not None
            or request.offline_translation_language is not None
            or request.review_content
            or request.review_structure,
            clean=mode in {ImprovementMode.CLEAN, ImprovementMode.CLEAN_AND_TRANSLATE},
            translate=(
                request.offline_translation_language is not None
                or mode in {ImprovementMode.TRANSLATE, ImprovementMode.CLEAN_AND_TRANSLATE}
            ),
            review_content=request.review_content,
            review_structure=request.review_structure,
        ),
    )
    direct_epub_translation = _is_epub_translation(request)
    direct_epub_personalization = _is_epub_personalization(request)
    generated_epub = (
        request.output_format is OutputFormat.EPUB
        and (
            plan.epub_buildable
            or (plan.epub_rebuildable and (request.review_content or request.review_structure))
        )
        and request.convert_to_markdown
    )
    if not plan.output_available:
        raise RequestValidationError(
            "El formato de salida no está disponible para el documento seleccionado."
        )
    if plan.semantic_issue is not None and request.output_format in {
        OutputFormat.TEXT,
        OutputFormat.DOCX,
    }:
        raise RequestValidationError(plan.semantic_issue)
    if request.output_format is OutputFormat.TEXT and request.convert_to_markdown:
        raise RequestValidationError(
            "La salida TXT debe conservar el texto sin convertirlo a Markdown."
        )
    if request.output_format is OutputFormat.DOCX and not _is_docx_transformation(request):
        raise RequestValidationError(
            "La salida Word debe conservar directamente el documento DOCX."
        )
    if request.output_format is OutputFormat.EPUB and not (
        generated_epub or direct_epub_translation or direct_epub_personalization
    ):
        raise RequestValidationError("Para conservar un EPUB como libro, activa Traducir.")
    if direct_epub_translation and plan.semantic_issue is not None:
        raise RequestValidationError(plan.semantic_issue)
    if request.image_output_directory is not None and not isinstance(
        request.image_output_directory, Path
    ):
        raise RequestValidationError("La carpeta de imágenes seleccionada no es válida.")
    if not isinstance(request.include_images, bool):
        raise RequestValidationError("La opción de incluir imágenes no es válida.")
    if not isinstance(request.preserve_styles, bool):
        raise RequestValidationError("La opción de conservar estilos no es válida.")
    if not request.include_images and request.output_format not in {
        OutputFormat.MARKDOWN,
        OutputFormat.EPUB,
    }:
        raise RequestValidationError(
            "La exclusión de imágenes solo se utiliza al crear Markdown o EPUB."
        )
    if (
        request.image_output_directory is not None
        and request.output_format is not OutputFormat.MARKDOWN
    ):
        raise RequestValidationError(
            "La carpeta de imágenes separada solo se utiliza con una salida Markdown."
        )
    if request.image_output_directory is not None and not request.include_images:
        raise RequestValidationError("Activa Incluir imágenes antes de elegir dónde guardarlas.")
    _validate_epub_metadata(request)


def _validate_epub_metadata(request: ProcessRequest) -> None:
    values = (request.epub_title, request.epub_author)
    if any(value is not None and not isinstance(value, str) for value in values):
        raise RequestValidationError("Los metadatos del EPUB no son válidos.")
    if any(value is not None and len(value.strip()) > 500 for value in values):
        raise RequestValidationError("El título y el autor del EPUB deben ser más breves.")
    has_metadata = any(value is not None and value.strip() for value in values)
    if not isinstance(request.epub_first_page_cover, bool) or not isinstance(
        request.epub_remove_cover,
        bool,
    ):
        raise RequestValidationError("La opción de portada del EPUB no es válida.")
    has_cover = (
        request.epub_cover_path is not None
        or request.epub_first_page_cover
        or request.epub_remove_cover
    )
    if (has_metadata or has_cover) and request.output_format is not OutputFormat.EPUB:
        raise RequestValidationError(
            "El título, autor y portada solo se utilizan cuando el resultado es EPUB."
        )
    if (
        sum(
            (
                request.epub_cover_path is not None,
                request.epub_first_page_cover,
                request.epub_remove_cover,
            )
        )
        > 1
    ):
        raise RequestValidationError("Elige una sola portada para el EPUB.")
    if (
        request.epub_cover_path is not None or request.epub_first_page_cover
    ) and not request.include_images:
        raise RequestValidationError(
            "Activa Incluir imágenes antes de elegir una portada para el EPUB."
        )
    if request.epub_remove_cover and request.source_path.suffix.lower() != ".epub":
        raise RequestValidationError(
            "Solo se puede quitar una portada existente cuando el origen es EPUB."
        )
    if request.epub_first_page_cover and request.source_path.suffix.lower() != ".pdf":
        raise RequestValidationError(
            "La primera página solo puede usarse como portada al crear un EPUB desde PDF."
        )
    cover_path = request.epub_cover_path
    if cover_path is None:
        return
    if not isinstance(cover_path, Path) or not cover_path.is_file():
        raise RequestValidationError("La portada seleccionada ya no está disponible.")
    if cover_path.suffix.lower() not in _EPUB_COVER_MEDIA_TYPES:
        raise RequestValidationError("La portada debe ser una imagen JPG, PNG, GIF, SVG o WebP.")
    try:
        cover_size = cover_path.stat().st_size
    except OSError as exc:
        raise RequestValidationError("No se pudo leer la portada seleccionada.") from exc
    if cover_size <= 0 or cover_size > _MAX_EPUB_COVER_BYTES:
        raise RequestValidationError("La portada debe ocupar entre 1 byte y 32 MiB.")


def _read_epub_cover(path: Path) -> ConvertedResource:
    suffix = path.suffix.lower()
    media_type = _EPUB_COVER_MEDIA_TYPES[suffix]
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise RequestValidationError("No se pudo leer la portada seleccionada.") from exc
    return ConvertedResource(
        PurePosixPath(f"cover/cover{suffix}"),
        content,
        media_type,
    )


def _validate_ai_settings(
    request: ProcessRequest,
    settings: AppSettings | None,
) -> None:
    mode = request.improvement_mode
    if mode is not None or request.review_content or request.review_structure:
        if settings is None:
            raise SettingsError("Elige un modelo de IA instalado antes de usar la IA.")
        normalized_settings = validate_settings(settings)
        if normalized_settings.model is None:
            raise SettingsError("Elige un modelo de IA instalado antes de usar la IA.")
        if is_reasoning_model_id(normalized_settings.model):
            raise SettingsError(
                "Ese modelo prioriza el razonamiento y no es apto para transformar documentos. "
                "Elige una variante Instruct."
            )
        if mode in {ImprovementMode.TRANSLATE, ImprovementMode.CLEAN_AND_TRANSLATE}:
            if request.target_language is None or not request.target_language.strip():
                raise RequestValidationError("Indica el idioma de destino para traducir.")


def _validate_output_directory(output_directory: Path, source_path: Path) -> None:
    if not output_directory.exists():
        raise RequestValidationError("La carpeta de salida no existe.")
    if not output_directory.is_dir():
        raise RequestValidationError("La salida debe ser una carpeta.")

    probe_path: Path | None = None
    try:
        descriptor, temporary_name = mkstemp(
            dir=output_directory,
            prefix=".parsezen-write-test-",
            suffix=".tmp",
        )
        probe_path = Path(temporary_name)
        os.write(descriptor, b"Parsezen")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
    except OSError as exc:
        raise RequestValidationError(
            "No se puede escribir en la carpeta de salida seleccionada."
        ) from exc
    finally:
        if "descriptor" in locals() and descriptor >= 0:
            os.close(descriptor)
        if probe_path is not None:
            try:
                probe_path.unlink(missing_ok=True)
            except OSError:
                pass

    try:
        source_size = source_path.stat().st_size
        free_bytes = shutil.disk_usage(output_directory).free
    except OSError as exc:
        raise RequestValidationError(
            "No se pudo comprobar el espacio disponible en la carpeta de salida."
        ) from exc
    required_bytes = max(8 * 1024 * 1024, min(source_size * 2, 1024 * 1024 * 1024))
    if free_bytes < required_bytes:
        raise RequestValidationError(
            "No hay espacio libre suficiente en la carpeta de salida para procesar el documento."
        )


def _validate_temporary_working_space(request: ProcessRequest) -> None:
    if request.source_path.suffix.lower() != ".pdf" or not request.convert_to_markdown:
        return
    temporary_directory = Path(gettempdir())
    try:
        source_size = request.source_path.stat().st_size
        free_bytes = shutil.disk_usage(temporary_directory).free
    except OSError as exc:
        raise RequestValidationError(
            "No se pudo comprobar el espacio temporal disponible para procesar el PDF."
        ) from exc
    required_bytes = max(
        512 * 1024 * 1024,
        min(source_size * 6, 4 * 1024 * 1024 * 1024),
    )
    if free_bytes < required_bytes:
        raise RequestValidationError(
            "No hay espacio temporal suficiente para procesar el PDF con seguridad."
        )


def _is_epub_translation(request: ProcessRequest) -> bool:
    return (
        request.source_path.suffix.lower() == ".epub"
        and not request.convert_to_markdown
        and not request.review_content
        and not request.review_structure
        and request.output_format is OutputFormat.EPUB
        and (
            request.offline_translation_language is not None
            or request.improvement_mode
            in {ImprovementMode.TRANSLATE, ImprovementMode.CLEAN_AND_TRANSLATE}
        )
    )


def _is_epub_personalization(request: ProcessRequest) -> bool:
    return (
        request.source_path.suffix.lower() == ".epub"
        and not request.convert_to_markdown
        and request.output_format is OutputFormat.EPUB
        and request.improvement_mode is None
        and request.offline_translation_language is None
        and not request.review_content
        and not request.review_structure
    )


def _is_docx_transformation(request: ProcessRequest) -> bool:
    return (
        request.source_path.suffix.lower() == ".docx"
        and not request.convert_to_markdown
        and request.output_format is OutputFormat.DOCX
        and (
            request.improvement_mode is not None
            or request.offline_translation_language is not None
            or request.review_content
        )
    )


def _request_translates(request: ProcessRequest) -> bool:
    return request.offline_translation_language is not None or request.improvement_mode in {
        ImprovementMode.TRANSLATE,
        ImprovementMode.CLEAN_AND_TRANSLATE,
    }


def _materialize_markdown_revision(
    draft: RevisionDraft | None,
    final_path: Path,
    image_output_directory: Path | None,
    has_resources: bool,
) -> RevisionDraft | None:
    """Resolve private resource markers before presenting a Markdown review."""
    if draft is None:
        return None
    resources_reference: str | None = None
    if has_resources:
        suffix = ".mended.md"
        base_name = (
            final_path.name[: -len(suffix)] if final_path.name.endswith(suffix) else final_path.stem
        )
        resources_directory = (image_output_directory or final_path.parent) / (
            f"{base_name}.assets"
        )
        try:
            resources_reference = os.path.relpath(
                resources_directory,
                start=final_path.parent,
            )
        except ValueError as exc:
            raise RequestValidationError(
                "La carpeta de imágenes debe estar en la misma unidad que el Markdown."
            ) from exc
    original = materialize_converted_markdown(
        draft.original_markdown,
        resources_reference,
    )
    proposed = materialize_converted_markdown(
        draft.proposed_markdown,
        resources_reference,
    )
    return build_revision_draft(original, proposed, kinds=draft.kinds)


def _validate_epub_path(path: Path) -> None:
    validate_epub_file(path)


def _validate_preserved_epub_path(path: Path) -> None:
    inspect_epub_package(path)


def _notify(on_stage: StageCallback | None, stage: ProcessStage) -> None:
    LOGGER.info("processing_stage stage=%s", stage.value)
    if on_stage is not None:
        on_stage(stage)


def _elapsed_milliseconds(started_at: float) -> int:
    return round((monotonic() - started_at) * 1000)


def _safe_failure_location(exc: Exception) -> tuple[str, str, str, int]:
    """Return allowlisted code location data, never paths, messages or locals."""

    module_name = "external"
    function_name = "unknown"
    line_number = 0
    traceback_cursor = exc.__traceback__
    while traceback_cursor is not None:
        frame = traceback_cursor.tb_frame
        candidate_module = frame.f_globals.get("__name__")
        if isinstance(candidate_module, str) and (
            candidate_module == "parsezen" or candidate_module.startswith("parsezen.")
        ):
            module_name = candidate_module
            function_name = frame.f_code.co_name
            line_number = traceback_cursor.tb_lineno
        traceback_cursor = traceback_cursor.tb_next
    return secrets.token_hex(4), module_name, function_name, line_number
