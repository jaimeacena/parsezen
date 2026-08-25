"""Synchronous, Qt-free orchestration for one local document."""

from __future__ import annotations

import inspect
import logging
import os
import re
import secrets
import shutil
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, replace
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
)
from parsezen.document_model import ConvertedDocument, ConvertedResource
from parsezen.domain.attempt_activity import (
    AttemptPhase,
    is_safe_token,
)
from parsezen.domain.jobs import MarkdownOrganization, ReviewRecommendation
from parsezen.domain.process_lifecycle import ProcessStage, phase_for_process_stage
from parsezen.domain.source_identity import is_sha256_digest, sha256_file
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
    ImprovementError,
    OutputWriteError,
    ParsezenError,
    ProcessingCancelledError,
    RequestValidationError,
    SettingsError,
    UnexpectedProcessingError,
)
from parsezen.failure_recovery import classify_failure
from parsezen.final_integrity import (
    FinalIntegrityReport,
    IntegrityLedger,
    binary_integrity_capture,
    merge_integrity_reports,
    text_integrity_capture,
)
from parsezen.glossary import (
    GlossaryEntry,
    protect_glossary,
    validate_glossary,
)
from parsezen.improvement import (
    ImprovementMode,
    improve_markdown,
)
from parsezen.local_models import is_reasoning_model_id
from parsezen.markdown_resources import without_markdown_images, without_markdown_resource
from parsezen.offline_translation import translate_markdown_offline
from parsezen.output import (
    replace_binary_output,
    replace_markdown_output,
    write_epub_output,
    write_epub_translation_output,
)
from parsezen.pdf_conversion import (
    PdfPageRange,
    PdfProgressCallback,
    PdfQualityReport,
    PdfVisualArbiterFactory,
    render_pdf_page_cover,
    resolve_pdf_page_range,
    strip_pdf_page_markers,
)
from parsezen.pipeline.contracts import (
    ProcessRequest,
    ProcessResult,
    ProcessTelemetry,
    ProgressCallback,
    StageCallback,
    StageTelemetry,
)
from parsezen.pipeline.prepare import (
    combined_translation_glossary,
    prepare_document_input,
)
from parsezen.pipeline.publish import (
    EPUB_COVER_MEDIA_TYPES,
    finish_work_checkpoints,
    publish_transformed_document,
)
from parsezen.pipeline.transform import (
    effective_ai_improvement_mode as _effective_ai_improvement_mode,
)
from parsezen.pipeline.transform import (
    epub_translation_resume_key,
    transform_prepared_document,
)
from parsezen.pipeline.transform import (
    improve_with_checkpoints as _improve_with_checkpoints,
)
from parsezen.pipeline.transform import (
    linguistic_review_coverage as _linguistic_review_coverage,
)
from parsezen.pipeline.transform import (
    repair_translation_warnings as _repair_translation_warnings,
)
from parsezen.pipeline.transform import (
    resolve_review_positions as _resolve_targeted_review_positions,
)
from parsezen.pipeline.transform import (
    review_translation_with_checkpoints as _review_translation_with_checkpoints,
)
from parsezen.pipeline.transform import (
    reviewable_semantic_blocks as _reviewable_semantic_blocks,
)
from parsezen.pipeline.transform import (
    translation_quality_report as _translation_quality_report,
)
from parsezen.processing_metrics import BatchTelemetry, capture_batch_telemetry
from parsezen.revision import (
    RevisionDraft,
    RevisionKind,
    build_revision_draft,
    validate_revision_selection,
)
from parsezen.semantic_blocks import (
    SemanticRole,
    analyze_markdown,
)
from parsezen.settings import (
    AppSettings,
    has_specialized_ai_profiles,
    settings_for_review,
    settings_for_translation,
    validate_settings,
)
from parsezen.translation_quality import (
    LinguisticReviewMode,
    TranslationQualityReport,
    detect_language_code,
    resolve_language_code,
)
from parsezen.visual_ocr import build_local_visual_text_arbiter
from parsezen.work_checkpoints import (
    WorkCheckpoints,
    open_work_checkpoints,
)
from parsezen.workflow import OutputFormat, WorkflowOptions, plan_workflow

LOGGER = logging.getLogger(__name__)
_CURRENT_ATTEMPT_ID: ContextVar[str | None] = ContextVar(
    "parsezen_current_attempt_id",
    default=None,
)
_MAX_EPUB_COVER_BYTES = 32 * 1024 * 1024


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

    def snapshot(self, now: float, batches: BatchTelemetry) -> ProcessTelemetry:
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
            batches=batches,
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
class _PreparedEpubReview:
    document: ConvertedDocument
    metadata: EpubBookMetadata
    chapter_count: int
    resource_count: int
    integrity_report: FinalIntegrityReport | None
    revision_draft: RevisionDraft | None
    linguistic_review_mode: LinguisticReviewMode
    translation_quality_report: TranslationQualityReport
    review_translation_quality_report: TranslationQualityReport
    normalized_output: bool


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
    load_checkpoint: Callable[[str], str | None]
    save_checkpoint: Callable[[str, str], bool]


def process_document(
    request: ProcessRequest,
    on_stage: StageCallback | None = None,
    on_progress: ProgressCallback | None = None,
    *,
    settings: AppSettings | None = None,
    cancellation: CancellationToken | None = None,
    epub_checkpoint_root: Path | None = None,
    work_checkpoint_root: Path | None = None,
    attempt_id: str | None = None,
) -> ProcessResult:
    """Validate, read/convert, optionally improve and safely write one document."""
    started_at = monotonic()
    effective_attempt_id = _effective_attempt_id(attempt_id)
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
        "processing_started attempt_id=%s phase=%s extension=%s output_format=%s conversion=%s "
        "improvement_mode=%s offline_translation=%s review_content=%s review_structure=%s",
        effective_attempt_id,
        AttemptPhase.PREPARATION.value,
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
        with capture_batch_telemetry() as batch_telemetry:
            with translation_session:
                with _attempt_stage_context(effective_attempt_id):
                    result = _process_document(
                        request,
                        report_stage,
                        on_progress,
                        settings=settings,
                        cancellation=cancellation,
                        epub_checkpoint_root=epub_checkpoint_root,
                        work_checkpoint_root=work_checkpoint_root,
                    )
    except ProcessingCancelledError as exc:
        phase = phase_for_process_stage(current_stage)
        LOGGER.info(
            "processing_cancelled attempt_id=%s phase=%s error_code=cancellation "
            "error_type=%s stage=%s duration_ms=%d",
            effective_attempt_id,
            phase.value,
            type(exc).__name__,
            current_stage.value if current_stage is not None else "not_started",
            _elapsed_milliseconds(started_at),
        )
        raise
    except ParsezenError as exc:
        phase = phase_for_process_stage(current_stage)
        LOGGER.warning(
            "processing_failed attempt_id=%s phase=%s error_code=%s error_type=%s stage=%s "
            "duration_ms=%d",
            effective_attempt_id,
            phase.value,
            classify_failure(exc).value,
            type(exc).__name__,
            current_stage.value if current_stage is not None else "not_started",
            _elapsed_milliseconds(started_at),
        )
        raise
    except Exception as exc:
        incident_id, module_name, function_name, line_number = _safe_failure_location(exc)
        LOGGER.error(
            "processing_failed attempt_id=%s phase=%s error_code=unexpected incident=%s "
            "diagnostic_reference=%s unexpected_error_type=%s module=%s function=%s line=%d "
            "stage=%s duration_ms=%d",
            effective_attempt_id,
            phase_for_process_stage(current_stage).value,
            incident_id,
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

    process_telemetry = telemetry.snapshot(monotonic(), batch_telemetry.snapshot())
    result = replace(result, telemetry=process_telemetry)
    LOGGER.info(
        "processing_completed attempt_id=%s phase=%s extension=%s output_format=%s "
        "improvement_mode=%s raw_output=%s duration_ms=%d",
        effective_attempt_id,
        AttemptPhase.COMPLETION.value,
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
    if draft is not None:
        try:
            validate_revision_selection(draft, reviewed_text)
        except ImprovementError as exc:
            raise RequestValidationError(str(exc)) from exc

    suffix = result.final_path.suffix.lower()
    published_text = strip_pdf_page_markers(reviewed_text)
    if suffix in {".md", ".markdown"} and result.markdown_include_page_references:
        published_text = re.sub(
            r"<!--\s*PZDOC PDF PAGE (\d+)\s*-->",
            lambda match: (
                f'<a id="pagina-{match.group(1)}"></a>\n\n> Página original {match.group(1)}'
            ),
            reviewed_text,
            flags=re.IGNORECASE,
        )
    epub_chapters = result.epub_chapters
    preserved_images = result.preserved_images
    integrity_report: FinalIntegrityReport | None = None
    if suffix in {".md", ".markdown"}:
        integrity = text_integrity_capture(
            published_text,
            markdown=True,
        )
        replace_markdown_output(
            result.final_path,
            published_text,
            organization=result.markdown_organization,
            source_name=result.markdown_source_name or result.final_path.name,
            include_metadata=result.markdown_include_metadata,
            include_page_references=result.markdown_include_page_references,
            validate_staged=integrity,
        )
        integrity_report = integrity.report
    elif suffix == ".epub":
        metadata = result.revision_epub_metadata
        if metadata is None:
            raise RequestValidationError("Falta la información necesaria para reconstruir el EPUB.")
        preserve_existing_package = bool(
            result.preserve_epub_package_on_unchanged_review
            and draft is None
            and result.review_markdown is not None
            and published_text == strip_pdf_page_markers(result.review_markdown)
        )
        if preserve_existing_package:
            try:
                existing_content = result.final_path.read_bytes()
            except OSError as exc:
                raise OutputWriteError("No se pudo volver a leer el EPUB revisado.") from exc
            integrity = binary_integrity_capture(
                existing_content,
                format_label="EPUB",
                validate_container=_validate_preserved_epub_path,
                ledger=(
                    result.final_integrity_report.ledger
                    if result.final_integrity_report is not None
                    else IntegrityLedger()
                ),
            )
            integrity(result.final_path)
            integrity_report = merge_integrity_reports(
                result.final_integrity_report,
                integrity.report,
            )
        else:
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
    else:
        raise RequestValidationError(
            "Este formato todavía no admite aplicar una revisión editable."
        )
    return replace(
        result,
        epub_chapters=epub_chapters,
        preserved_images=preserved_images,
        translation_quality_report=result.translation_quality_for_review,
        review_translation_quality_report=result.translation_quality_for_review,
        review_markdown=published_text,
        review_required=False,
        revision_approved=True,
        final_integrity_report=integrity_report,
    )


def review_completed_result(
    request: ProcessRequest,
    *,
    base_result: ProcessResult,
    recommendation: ReviewRecommendation,
    settings: AppSettings,
    on_stage: StageCallback | None = None,
    on_progress: ProgressCallback | None = None,
    cancellation: CancellationToken | None = None,
    work_checkpoint_root: Path | None = None,
) -> ProcessResult:
    """Build a conservative proposal from only the blocks backed by quality signals."""

    current = base_result.review_markdown
    if current is None or not current.strip():
        raise RequestValidationError("El resultado ya no contiene texto local revisable.")
    target_document = analyze_markdown(current)
    target_blocks = _reviewable_semantic_blocks(target_document)
    resolved_positions = _resolve_targeted_review_positions(target_blocks, recommendation)
    if resolved_positions is None:
        raise RequestValidationError(
            "El resultado ha cambiado desde la recomendación. "
            "Vuelve a procesarlo antes de revisarlo."
        )
    scoped_request = replace(
        request,
        improvement_mode=None,
        target_language=None,
        review_content=True,
        review_structure=False,
    )
    _validate_request(scoped_request, settings)
    source_digest = _checkpoint_source_digest(scoped_request)
    checkpoints = _open_general_work_checkpoints(
        scoped_request,
        settings,
        root=work_checkpoint_root,
        source_digest=source_digest,
    )
    selected = frozenset(resolved_positions)

    source_markdown = (
        _review_source_markdown(request, base_result, cancellation)
        if _request_translates(request)
        else None
    )
    source_blocks = (
        _reviewable_semantic_blocks(analyze_markdown(source_markdown))
        if source_markdown is not None
        else ()
    )
    bilingual = bool(source_markdown is not None and len(source_blocks) == len(target_blocks))
    replacements: dict[str, str] = {}
    _notify(on_stage, ProcessStage.REVIEWING_CONTENT)
    total = len(selected)
    for progress, position in enumerate(sorted(selected), start=1):
        check_cancelled(cancellation)
        if on_progress is not None:
            on_progress(progress, total)
        target_block = target_blocks[position]
        if bilingual:
            source_block = source_blocks[position]
            proposed = _review_translation_with_checkpoints(
                source_block.markdown,
                target_block.markdown,
                settings,
                request,
                None,
                cancellation,
                checkpoints,
            )
        else:
            proposed = _improve_with_checkpoints(
                target_block.markdown,
                ImprovementMode.REVIEW_CONTENT,
                settings,
                scoped_request,
                None,
                cancellation,
                checkpoints,
            )
        if proposed != target_block.markdown:
            replacements[target_block.identifier] = proposed

    proposed_markdown = "".join(
        replacements.get(block.identifier, block.markdown) for block in target_document.blocks
    )
    candidate = build_revision_draft(
        current,
        proposed_markdown,
        kinds=frozenset({RevisionKind.CONTENT}),
        translation_source_markdown=source_markdown if bilingual else None,
        translation_target_language=(
            request.offline_translation_language or request.target_language if bilingual else None
        ),
        protected_translation_terms=tuple(entry.target for entry in request.glossary),
    )
    draft = candidate if candidate.changes else None
    review_translation_quality_report = base_result.translation_quality_for_review
    linguistic_review_coverage = base_result.linguistic_review_coverage
    if bilingual and source_markdown is not None:
        review_translation_quality_report = _translation_quality_report(
            request,
            source_markdown,
            proposed_markdown,
        )
        previous_reviewed = (
            linguistic_review_coverage.semantically_reviewed_blocks
            if linguistic_review_coverage is not None
            else 0
        )
        previous_independent = (
            linguistic_review_coverage.independently_verified_blocks
            if linguistic_review_coverage is not None
            else 0
        )
        linguistic_review_coverage = _linguistic_review_coverage(
            review_translation_quality_report,
            mode=LinguisticReviewMode.TARGETED_BILINGUAL,
            reviewed_blocks=previous_reviewed + len(selected),
            independently_verified_blocks=previous_independent + len(selected),
        )
    finish_work_checkpoints(
        checkpoints,
        settings=settings,
        root=work_checkpoint_root,
        cleanup_allowed=draft is None,
    )
    return replace(
        base_result,
        review_translation_quality_report=review_translation_quality_report,
        linguistic_review_coverage=linguistic_review_coverage,
        revision_draft=draft,
        review_markdown=proposed_markdown if draft is not None else current,
        review_required=draft is not None,
        revision_approved=False,
        telemetry=None,
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
        epub_source_digest = _checkpoint_source_digest(request)
        return _process_epub_translation(
            request,
            on_stage,
            on_progress,
            settings=settings,
            cancellation=cancellation,
            epub_checkpoint_root=epub_checkpoint_root,
            work_checkpoint_root=work_checkpoint_root,
            source_digest=epub_source_digest,
        )

    if _is_epub_personalization(request):
        return _process_epub_personalization(
            request,
            on_stage,
            cancellation=cancellation,
        )

    source_digest = (
        _checkpoint_source_digest(request)
        if _uses_general_work_checkpoints(request) or _uses_pdf_conversion_checkpoints(request)
        else None
    )
    work_checkpoints = _open_general_work_checkpoints(
        request,
        settings,
        root=work_checkpoint_root,
        source_digest=source_digest,
    )
    pdf_checkpoints = _open_pdf_conversion_checkpoints(
        request,
        root=work_checkpoint_root,
        source_digest=source_digest,
    )

    prepared = prepare_document_input(
        request,
        lambda stage: _notify(on_stage, stage),
        on_progress,
        cancellation,
        pdf_checkpoints,
        converter=convert_document,
        page_range_resolver=resolve_pdf_page_range,
        pdf_visual_arbiter_factory=_pdf_visual_arbiter_factory(request, settings),
    )
    generated_epub = request.output_format is OutputFormat.EPUB

    transformed = transform_prepared_document(
        prepared,
        request,
        lambda stage: _notify(on_stage, stage),
        on_progress,
        settings,
        cancellation,
        work_checkpoints,
        generated_epub=generated_epub,
    )

    return publish_transformed_document(
        prepared,
        transformed,
        request,
        lambda stage: _notify(on_stage, stage),
        settings,
        cancellation,
        work_checkpoints,
        pdf_checkpoints,
        work_checkpoint_root,
        render_cover=render_pdf_page_cover,
    )


def _prepare_translated_epub_review(
    request: ProcessRequest,
    staged_path: Path,
    translated_language_code: str,
    converted_source: ConvertedDocument,
    initial_translation_quality_report: TranslationQualityReport,
    translation_glossary: tuple[GlossaryEntry, ...],
    target_language: str | None,
    effective_ai_mode: ImprovementMode | None,
    on_stage: StageCallback | None,
    on_progress: ProgressCallback | None,
    settings: AppSettings | None,
    cancellation: CancellationToken | None,
    work_checkpoints: WorkCheckpoints | None,
) -> _PreparedEpubReview:
    """Prepare the normalized, editable representation of one translated EPUB."""

    normalize_output = (
        not request.preserve_styles
        or not request.include_images
        or request.epub_cover_path is not None
        or request.epub_remove_cover
    )
    # Every translated EPUB reaches the same confirmation/editor contract. The
    # exact package stays published unless an option requires normalization.
    converted_document = convert_epub(staged_path, cancellation=cancellation)
    package_metadata = inspect_epub_package(staged_path)
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
    normalized_metadata = EpubBookMetadata(
        title=request.epub_title or package_metadata.title or request.source_path.stem,
        language=package_metadata.language or translated_language_code,
        author=(
            request.epub_author
            if request.epub_author is not None
            else ", ".join(package_metadata.authors) or None
        ),
        cover_resource=cover_resource_path,
        identifiers=package_metadata.identifiers,
        publisher=package_metadata.publisher,
        publication_date=package_metadata.publication_date,
    )
    revision_source = normalized_document.markdown
    reviewed_markdown = revision_source
    review_base_quality_report = (
        _translation_quality_report(
            request,
            converted_source.markdown,
            revision_source,
        )
        or initial_translation_quality_report
    )
    revision_kinds: set[RevisionKind] = set()
    translation_review_source: str | None = None
    content_review_fused = bool(
        request.review_content
        and request.offline_translation_language is None
        and effective_ai_mode is ImprovementMode.CLEAN_AND_TRANSLATE
    )
    linguistic_review_mode = (
        LinguisticReviewMode.CORRECTED_DURING_TRANSLATION
        if content_review_fused
        else LinguisticReviewMode.NOT_REVIEWED
    )
    if request.review_content and not content_review_fused:
        if settings is None:
            raise AssertionError("Validated review requests always have settings.")
        _notify(on_stage, ProcessStage.REVIEWING_CONTENT)
        linguistic_review_mode = LinguisticReviewMode.INDEPENDENT_BILINGUAL
        translation_review_source = converted_source.markdown
        reviewed_markdown = _review_translation_with_checkpoints(
            translation_review_source,
            reviewed_markdown,
            settings,
            request,
            on_progress,
            cancellation,
            work_checkpoints,
            quality_report=review_base_quality_report,
        )
        if reviewed_markdown != revision_source:
            revision_kinds.add(RevisionKind.CONTENT)
    if request.review_structure:
        if settings is None:
            raise AssertionError("Validated review requests always have settings.")
        _notify(on_stage, ProcessStage.ORGANIZING_STRUCTURE)
        reviewed_markdown = _improve_with_checkpoints(
            reviewed_markdown,
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
            reviewed_markdown,
            kinds=frozenset(revision_kinds),
            translation_source_markdown=translation_review_source,
            translation_target_language=target_language,
            protected_translation_terms=tuple(entry.target for entry in translation_glossary),
        )
        if revision_kinds
        else None
    )
    revision_draft = (
        revision_candidate
        if revision_candidate is not None and revision_candidate.changes
        else None
    )
    review_translation_quality_report = (
        _translation_quality_report(
            request,
            converted_source.markdown,
            reviewed_markdown,
        )
        or initial_translation_quality_report
    )
    normalized_document = replace(
        normalized_document,
        markdown=(revision_source if revision_draft is not None else reviewed_markdown),
    )
    chapter_count = 0
    resource_count = len(normalized_resources)
    integrity_report: FinalIntegrityReport | None = None
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
            staged_path,
            built.content,
            validate_staged=normalized_capture,
        )
        chapter_count = built.chapter_count
        resource_count = built.resource_count
        integrity_report = merge_integrity_reports(
            built.integrity_report,
            normalized_capture.report,
        )
    return _PreparedEpubReview(
        document=normalized_document,
        metadata=normalized_metadata,
        chapter_count=chapter_count,
        resource_count=resource_count,
        integrity_report=integrity_report,
        revision_draft=revision_draft,
        linguistic_review_mode=linguistic_review_mode,
        translation_quality_report=initial_translation_quality_report,
        review_translation_quality_report=review_translation_quality_report,
        normalized_output=normalize_output,
    )


def _process_epub_translation(
    request: ProcessRequest,
    on_stage: StageCallback | None,
    on_progress: ProgressCallback | None,
    *,
    settings: AppSettings | None,
    cancellation: CancellationToken | None,
    epub_checkpoint_root: Path | None,
    work_checkpoint_root: Path | None,
    source_digest: str,
) -> ProcessResult:
    target_language = request.offline_translation_language or request.target_language
    language_code = resolve_language_code(target_language)
    if language_code is None:
        raise RequestValidationError("El idioma de destino del EPUB no está soportado.")

    package_language_code = resolve_language_code(
        inspect_epub_package(request.source_path).language
    )
    converted_source = convert_epub(request.source_path, cancellation=cancellation)
    # Short navigation labels are too little evidence to overrule publication metadata.
    detected_language_code = detect_language_code(
        converted_source.markdown,
        minimum_letters=80,
    )
    source_language_code = detected_language_code or package_language_code
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

    source_semantic = analyze_markdown(converted_source.markdown)
    work_checkpoints = _open_general_work_checkpoints(
        request,
        settings,
        root=work_checkpoint_root,
        source_digest=source_digest,
    )
    translation_glossary = combined_translation_glossary(
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
        epub_translation_resume_key(
            request,
            settings,
            language_code,
            source_semantic.terms,
        ),
        root=epub_checkpoint_root,
        source_digest=source_digest,
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
            improvement_arguments: _ImprovementArguments = {"on_progress": progress}
            if token is not None:
                improvement_arguments["cancellation"] = token
            if work_checkpoints is not None:
                improvement_arguments["load_checkpoint"] = work_checkpoints.load
                improvement_arguments["save_checkpoint"] = work_checkpoints.save
            translated = improve_markdown(
                protected.text,
                effective_ai_mode,
                settings_for_translation(settings),
                request.target_language,
                source_language_code=source_language_code,
                **improvement_arguments,
            )
            translated = protected.restore(translated)
        if request.offline_translation_language is not None:
            protected = protect_glossary(translated, translation_glossary)
            offline_arguments: _OfflineTranslationArguments = {
                "on_progress": progress,
                "on_engine_ready": announce_translation_stage,
            }
            if token is not None:
                offline_arguments["cancellation"] = token
            translation_parameters = inspect.signature(translate_markdown_offline).parameters
            if work_checkpoints is not None and "load_checkpoint" in translation_parameters:
                offline_arguments["load_checkpoint"] = work_checkpoints.load
                offline_arguments["save_checkpoint"] = work_checkpoints.save
            translated = translate_markdown_offline(
                protected.text,
                request.offline_translation_language,
                **offline_arguments,
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
            settings=settings_for_translation(settings) if settings is not None else None,
            cancellation=token,
            source_language_code=source_language_code,
            translation_glossary=translation_glossary,
            work_checkpoints=work_checkpoints,
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
    translated_integrity = binary_integrity_capture(
        translated_content,
        format_label="EPUB",
        validate_container=_validate_preserved_epub_path,
    )
    staging_directory = request.output_directory or request.source_path.parent
    with _staged_epub_payload(translated_content, staging_directory) as staged_path:
        translated_integrity(staged_path)
        prepared_review = _prepare_translated_epub_review(
            request,
            staged_path,
            translated_epub.language_code,
            converted_source,
            translated_epub.quality_report,
            translation_glossary,
            target_language,
            effective_ai_mode,
            on_stage,
            on_progress,
            settings_for_review(settings) if settings is not None else None,
            cancellation,
            work_checkpoints,
        )
        check_cancelled(cancellation)
        try:
            publication_content = staged_path.read_bytes()
        except OSError as exc:
            raise OutputWriteError(
                "No se pudo preparar el EPUB validado para su publicación."
            ) from exc
    publication_integrity = binary_integrity_capture(
        publication_content,
        format_label="EPUB",
        validate_container=(
            _validate_epub_path
            if prepared_review.normalized_output
            else _validate_preserved_epub_path
        ),
        ledger=(
            prepared_review.integrity_report.ledger
            if prepared_review.integrity_report is not None
            else (
                translated_integrity.report.ledger
                if translated_integrity.report is not None
                else IntegrityLedger()
            )
        ),
    )
    _notify(on_stage, ProcessStage.WRITING)
    final_path = write_epub_translation_output(
        request.source_path,
        publication_content,
        translated_epub.language_code,
        request.output_directory,
        validate_staged=publication_integrity,
    )
    _finish_epub_checkpoints(
        checkpoints,
        settings=settings,
        root=epub_checkpoint_root,
    )
    finish_work_checkpoints(
        work_checkpoints,
        settings=settings,
        root=work_checkpoint_root,
        cleanup_allowed=prepared_review.revision_draft is None,
    )
    _notify(on_stage, ProcessStage.COMPLETED)
    return ProcessResult(
        final_path=final_path,
        review_original_path=request.source_path,
        epub_translation_parts=translated_epub.translation_parts,
        epub_resumed_parts=translated_epub.resumed_parts,
        epub_checkpoint_degraded=translated_epub.checkpoint_degraded,
        translation_quality_report=prepared_review.translation_quality_report,
        review_translation_quality_report=(prepared_review.review_translation_quality_report),
        linguistic_review_coverage=_linguistic_review_coverage(
            prepared_review.review_translation_quality_report,
            mode=prepared_review.linguistic_review_mode,
        ),
        preserved_images=prepared_review.resource_count,
        epub_chapters=prepared_review.chapter_count,
        revision_resources=prepared_review.document.resources,
        revision_epub_metadata=prepared_review.metadata,
        revision_draft=prepared_review.revision_draft,
        review_markdown=(
            prepared_review.revision_draft.proposed_markdown
            if prepared_review.revision_draft is not None
            else prepared_review.document.markdown
        ),
        review_required=True,
        preserve_epub_package_on_unchanged_review=True,
        final_integrity_report=merge_integrity_reports(
            translated_integrity.report,
            prepared_review.integrity_report,
            publication_integrity.report,
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
        title=package.title or request.source_path.stem,
        language=package.language or "und",
        author=", ".join(package.authors) or None,
        cover_resource=package.cover_path,
        identifiers=package.identifiers,
        publisher=package.publisher,
        publication_date=package.publication_date,
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
        preserve_epub_package_on_unchanged_review=True,
        final_integrity_report=integrity.report,
        front_matter_blocks=semantic_document.front_matter_blocks,
        toc_blocks=semantic_document.toc_blocks,
        terminology_terms=len(semantic_document.terms),
    )


def _review_source_markdown(
    request: ProcessRequest,
    result: ProcessResult,
    cancellation: CancellationToken | None,
) -> str | None:
    """Recover the aligned local source only for an explicitly requested bilingual review."""

    source = result.review_original_path or request.source_path
    suffix = source.suffix.casefold()
    if suffix in {".md", ".markdown", ".txt"}:
        try:
            return source.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
    arguments: _ConversionArguments = {}
    if cancellation is not None:
        arguments["cancellation"] = cancellation
    if suffix == ".pdf" and request.pdf_page_range is not None:
        arguments["pdf_page_range"] = request.pdf_page_range
    if suffix == ".pdf" and request.force_pdf_ocr:
        arguments["force_pdf_ocr"] = True
    try:
        return convert_document(
            source,
            preserve_resources=False,
            **arguments,
        ).markdown
    except (OSError, ParsezenError, ValueError):
        return None


def _uses_general_work_checkpoints(request: ProcessRequest) -> bool:
    return bool(
        request.improvement_mode is not None
        or request.offline_translation_language is not None
        or request.review_content
        or request.review_structure
    )


def _uses_pdf_conversion_checkpoints(request: ProcessRequest) -> bool:
    return request.source_path.suffix.lower() == ".pdf" and request.convert_to_markdown


def _pdf_visual_arbiter_factory(
    request: ProcessRequest,
    settings: AppSettings | None,
) -> PdfVisualArbiterFactory | None:
    """Create the optional vision arbiter lazily only for an AI-backed PDF workflow."""

    if (
        request.source_path.suffix.lower() != ".pdf"
        or settings is None
        or not (
            request.improvement_mode is not None
            or request.review_content
            or request.review_structure
        )
    ):
        return None
    normalized = settings_for_review(validate_settings(settings))
    if normalized.model is None:
        return None
    return lambda: build_local_visual_text_arbiter(
        normalized.model,
        normalized.timeout_seconds,
    )


def _checkpoint_source_digest(request: ProcessRequest) -> str:
    return request.source_content_sha256 or sha256_file(request.source_path)


def _open_general_work_checkpoints(
    request: ProcessRequest,
    settings: AppSettings | None,
    *,
    root: Path | None,
    source_digest: str | None = None,
) -> WorkCheckpoints | None:
    """Open checkpoints only when a workflow contains expensive resumable work."""
    if not _uses_general_work_checkpoints(request):
        return None
    page_range = (
        (request.pdf_page_range.first_page, request.pdf_page_range.last_page)
        if request.pdf_page_range is not None
        else None
    )
    common = (
        request.convert_to_markdown,
        request.improvement_mode.value if request.improvement_mode is not None else None,
        request.review_content,
        request.review_structure,
        request.target_language,
        request.offline_translation_language,
        page_range,
        request.force_pdf_ocr,
        request.output_format.value,
    )
    if not has_specialized_ai_profiles(settings):
        # Keep the pre-specialized resume identity exactly stable so existing
        # general-work checkpoints remain reusable after the profile split.
        model = settings.model if settings is not None else None
        context_window = settings.context_window if settings is not None else None
        resume_key = repr(("general-work-v3", *common, model, context_window))
    else:
        assert settings is not None
        translation_settings = settings_for_translation(settings)
        review_settings = settings_for_review(settings)
        resume_key = repr(
            (
                "general-work-v4-specialized",
                *common,
                translation_settings.model,
                translation_settings.context_window,
                review_settings.model,
                review_settings.context_window,
            )
        )
    return open_work_checkpoints(
        request.source_path,
        resume_key,
        root=root,
        source_digest=source_digest,
    )


def _open_pdf_conversion_checkpoints(
    request: ProcessRequest,
    *,
    root: Path | None,
    source_digest: str | None = None,
) -> WorkCheckpoints | None:
    """Share costly page/OCR work across Markdown, EPUB and later text transformations."""
    if not _uses_pdf_conversion_checkpoints(request):
        return None
    # Native extraction and OCR payloads are keyed again by their absolute page
    # number. Keeping the directory independent from the selected interval lets
    # the representative early check feed the later full run without ever
    # sharing data between different source bytes or OCR strategies.
    # Bump this whenever deterministic native-page reconciliation changes. Reusing
    # an older page payload would otherwise retain already-fixed TOC glyph errors.
    resume_key = repr(("pdf-conversion-v7", request.force_pdf_ocr))
    return open_work_checkpoints(
        request.source_path,
        resume_key,
        root=root,
        source_digest=source_digest,
    )


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
    source_digest = (
        _checkpoint_source_digest(request)
        if _uses_general_work_checkpoints(request) or _uses_pdf_conversion_checkpoints(request)
        else None
    )
    checkpoints = _open_general_work_checkpoints(
        request,
        settings,
        root=root,
        source_digest=source_digest,
    )
    if checkpoints is not None:
        checkpoints.clear()
    pdf_checkpoints = _open_pdf_conversion_checkpoints(
        request,
        root=root,
        source_digest=source_digest,
    )
    if pdf_checkpoints is not None:
        pdf_checkpoints.clear()


def clear_general_work_checkpoints(
    request: ProcessRequest,
    settings: AppSettings | None,
    *,
    root: Path | None = None,
) -> None:
    """Discard transformed sample text while retaining reusable PDF page work."""

    source_digest = (
        _checkpoint_source_digest(request) if _uses_general_work_checkpoints(request) else None
    )
    checkpoints = _open_general_work_checkpoints(
        request,
        settings,
        root=root,
        source_digest=source_digest,
    )
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
    _validate_source_identity(request)
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


def _validate_source_identity(request: ProcessRequest) -> None:
    """Reject a source that no longer matches the immutable queued document."""

    expected_size = request.source_size_bytes
    expected_modified = request.source_modified_ns
    expected_digest = request.source_content_sha256
    if expected_digest is not None and not is_sha256_digest(expected_digest):
        raise RequestValidationError("La identidad del documento original no es válida.")
    if request.source_identity_verified and (
        expected_size is None or expected_modified is None or expected_digest is None
    ):
        raise RequestValidationError("La identidad verificada del documento está incompleta.")
    if expected_size is None and expected_modified is None and expected_digest is None:
        return
    if expected_size is None or expected_modified is None:
        raise RequestValidationError("La identidad del documento original está incompleta.")
    try:
        statistics = request.source_path.stat()
    except OSError as exc:
        raise RequestValidationError("No se pudo comprobar el documento original.") from exc
    if statistics.st_size != expected_size or statistics.st_mtime_ns != expected_modified:
        raise RequestValidationError(
            "El original cambió desde que se añadió a la cola. "
            "Quítalo y vuelve a añadirlo antes de procesarlo."
        )
    if expected_digest is None or request.source_identity_verified:
        return
    try:
        current_digest = sha256_file(request.source_path)
    except OSError as exc:
        raise RequestValidationError("No se pudo comprobar el documento original.") from exc
    if current_digest != expected_digest:
        raise RequestValidationError(
            "El original cambió desde que se añadió a la cola. "
            "Quítalo y vuelve a añadirlo antes de procesarlo."
        )


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
    if mode in {ImprovementMode.TRANSLATE, ImprovementMode.CLEAN_AND_TRANSLATE}:
        target_language = request.target_language
        if not isinstance(target_language, str) or not target_language.strip():
            raise RequestValidationError("Indica el idioma de destino para traducir.")
        if resolve_language_code(target_language) is None:
            raise RequestValidationError("El idioma de destino no está soportado.")
    offline_language = request.offline_translation_language
    if offline_language is not None and (
        not isinstance(offline_language, str) or not offline_language.strip()
    ):
        raise RequestValidationError("Indica el idioma de destino para traducir offline.")
    if offline_language is not None and resolve_language_code(offline_language) is None:
        raise RequestValidationError("El idioma de destino no está soportado.")
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
    if not isinstance(request.markdown_organization, MarkdownOrganization):
        raise RequestValidationError("La organización del Markdown no es válida.")
    if not isinstance(request.markdown_include_metadata, bool) or not isinstance(
        request.markdown_include_page_references, bool
    ):
        raise RequestValidationError("Las opciones de referencia del Markdown no son válidas.")
    if request.output_format is not OutputFormat.MARKDOWN and (
        request.markdown_organization is not MarkdownOrganization.SINGLE_FILE
        or request.markdown_include_metadata
        or request.markdown_include_page_references
    ):
        raise RequestValidationError(
            "La organización, los metadatos y las páginas de origen solo se aplican a Markdown."
        )
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
    if cover_path.suffix.lower() not in EPUB_COVER_MEDIA_TYPES:
        raise RequestValidationError("La portada debe ser una imagen JPG, PNG, GIF, SVG o WebP.")
    try:
        cover_size = cover_path.stat().st_size
    except OSError as exc:
        raise RequestValidationError("No se pudo leer la portada seleccionada.") from exc
    if cover_size <= 0 or cover_size > _MAX_EPUB_COVER_BYTES:
        raise RequestValidationError("La portada debe ocupar entre 1 byte y 32 MiB.")


def _read_epub_cover(path: Path) -> ConvertedResource:
    suffix = path.suffix.lower()
    media_type = EPUB_COVER_MEDIA_TYPES[suffix]
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
        phases: list[AppSettings] = []
        if mode in {ImprovementMode.TRANSLATE, ImprovementMode.CLEAN_AND_TRANSLATE}:
            phases.append(settings_for_translation(normalized_settings))
        elif mode is not None:
            phases.append(settings_for_review(normalized_settings))
        if request.review_content or request.review_structure:
            phases.append(settings_for_review(normalized_settings))
        for phase_settings in phases:
            if phase_settings.model is None:
                raise SettingsError("Elige un modelo de IA instalado antes de usar la IA.")
            if is_reasoning_model_id(phase_settings.model):
                raise SettingsError(
                    "Ese modelo prioriza el razonamiento y no es apto para transformar documentos. "
                    "Elige una variante Instruct."
                )


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
        and request.output_format is OutputFormat.EPUB
        and _request_translates(request)
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


def _request_translates(request: ProcessRequest) -> bool:
    return request.offline_translation_language is not None or request.improvement_mode in {
        ImprovementMode.TRANSLATE,
        ImprovementMode.CLEAN_AND_TRANSLATE,
    }


def _validate_epub_path(path: Path) -> None:
    validate_epub_file(path)


def _validate_preserved_epub_path(path: Path) -> None:
    inspect_epub_package(path)


@contextmanager
def _staged_epub_payload(content: bytes, directory: Path) -> Iterator[Path]:
    """Expose unpublished EPUB bytes to local readers and remove them on every exit path."""

    staged_path: Path | None = None
    try:
        descriptor, temporary_name = mkstemp(
            dir=directory,
            prefix=".parsezen-epub-prepare-",
            suffix=".epub",
        )
        staged_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as output_file:
            output_file.write(content)
            output_file.flush()
            os.fsync(output_file.fileno())
    except OSError as exc:
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)
        raise OutputWriteError("No se pudo preparar temporalmente el EPUB traducido.") from exc
    try:
        yield staged_path
    finally:
        try:
            staged_path.unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("epub_staging_cleanup_failed")


@contextmanager
def _attempt_stage_context(attempt_id: str) -> Iterator[None]:
    token = _CURRENT_ATTEMPT_ID.set(attempt_id)
    try:
        yield
    finally:
        _CURRENT_ATTEMPT_ID.reset(token)


def _effective_attempt_id(value: str | None) -> str:
    if value is not None and is_safe_token(value):
        return value
    return secrets.token_hex(16)


def _notify(on_stage: StageCallback | None, stage: ProcessStage) -> None:
    LOGGER.info(
        "processing_stage attempt_id=%s phase=%s stage=%s",
        _CURRENT_ATTEMPT_ID.get() or "unassigned",
        phase_for_process_stage(stage).value,
        stage.value,
    )
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
