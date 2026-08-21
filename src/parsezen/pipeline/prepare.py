"""Prepare one source document without transforming or publishing it."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import TypedDict

from parsezen.cancellation import CancellationToken, check_cancelled
from parsezen.conversion import CONVERSION_REQUIRED_EXTENSIONS, convert_document
from parsezen.document_model import ConvertedDocument, ConvertedResource
from parsezen.domain.process_lifecycle import ProcessStage
from parsezen.epub_conversion import inspect_epub_package
from parsezen.glossary import MAX_GLOSSARY_ENTRIES, GlossaryEntry, validate_glossary
from parsezen.markdown_resources import without_markdown_images, without_markdown_resource
from parsezen.pdf_conversion import (
    PdfPageRange,
    PdfProgressCallback,
    PdfProgressPhase,
    PdfQualityReport,
    PdfVisualArbiterFactory,
    extract_pdf_warning_pages,
    resolve_pdf_page_range,
)
from parsezen.pipeline.contracts import (
    PreparedDocument,
    ProcessRequest,
    ProgressCallback,
    StageCallback,
)
from parsezen.semantic_blocks import DocumentTerm, analyze_markdown, reconcile_document_evidence
from parsezen.work_checkpoints import WorkCheckpoints, checkpoint_key
from parsezen.workflow import OutputFormat

_PDF_OCR_CHECKPOINT_PREFIX = "\x1eParsezen PDF OCR "
_PDF_OCR_CHECKPOINT_HEADER = f"{_PDF_OCR_CHECKPOINT_PREFIX}v4\x1f"
_MAX_INFERRED_TERMINOLOGY_OCCURRENCES = 64

DocumentConverter = Callable[..., ConvertedDocument]
PageRangeResolver = Callable[[Path, PdfPageRange], PdfPageRange]


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
    pdf_visual_arbiter_factory: PdfVisualArbiterFactory


def encode_pdf_ocr_checkpoint(markdown: str) -> str:
    """Preserve the valid distinction between no result and an empty OCR page."""

    return f"{_PDF_OCR_CHECKPOINT_HEADER}{markdown}"


def decode_pdf_ocr_checkpoint(payload: str | None) -> str | None:
    """Read current OCR data, retry tagged stale versions, and accept legacy text."""

    if payload is None:
        return None
    if payload.startswith(_PDF_OCR_CHECKPOINT_HEADER):
        return payload[len(_PDF_OCR_CHECKPOINT_HEADER) :]
    if payload.startswith(_PDF_OCR_CHECKPOINT_PREFIX):
        return None
    return payload


def combined_translation_glossary(
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


def prepare_document_input(
    request: ProcessRequest,
    on_stage: StageCallback | None,
    on_progress: ProgressCallback | None,
    cancellation: CancellationToken | None,
    pdf_checkpoints: WorkCheckpoints | None,
    *,
    converter: DocumentConverter = convert_document,
    page_range_resolver: PageRangeResolver = resolve_pdf_page_range,
    pdf_visual_arbiter_factory: PdfVisualArbiterFactory | None = None,
) -> PreparedDocument:
    """Convert and analyze the source without changing its content or publishing output."""

    source_path = request.source_path
    input_stage = (
        ProcessStage.CONVERTING
        if source_path.suffix.lower() in CONVERSION_REQUIRED_EXTENSIONS
        else ProcessStage.READING
    )
    _announce(on_stage, input_stage)
    resolved_page_range = (
        page_range_resolver(source_path, request.pdf_page_range)
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
            _announce(on_stage, stage)

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
                pdf_checkpoints.load(checkpoint_key("pdf-native-page-v2", str(page_number)))
            )
            conversion_arguments["save_pdf_page_checkpoint"] = lambda page_number, text: (
                pdf_checkpoints.save(
                    checkpoint_key("pdf-native-page-v2", str(page_number)),
                    text,
                )
            )
            conversion_arguments["load_pdf_ocr_checkpoint"] = lambda page_number: (
                decode_pdf_ocr_checkpoint(
                    pdf_checkpoints.load(checkpoint_key("pdf-ocr-page", str(page_number)))
                )
            )
            conversion_arguments["save_pdf_ocr_checkpoint"] = lambda page_number, text: (
                pdf_checkpoints.save(
                    checkpoint_key("pdf-ocr-page", str(page_number)),
                    encode_pdf_ocr_checkpoint(text),
                )
            )
        if resolved_page_range is not None:
            conversion_arguments["pdf_page_range"] = resolved_page_range
        if request.force_pdf_ocr:
            conversion_arguments["force_pdf_ocr"] = True
        if pdf_visual_arbiter_factory is not None:
            conversion_arguments["pdf_visual_arbiter_factory"] = pdf_visual_arbiter_factory
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
    converted_document = converter(
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
    markdown, _evidence_changes = reconcile_document_evidence(markdown)
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
    translation_glossary = combined_translation_glossary(
        request.glossary,
        semantic_document.terms,
    )

    return PreparedDocument(
        source_path,
        resolved_page_range,
        output_stem,
        pdf_quality_report,
        source_cover_path,
        converted_resources,
        markdown,
        problematic_pdf_pages,
        semantic_document,
        translation_glossary,
    )


def _announce(on_stage: StageCallback | None, stage: ProcessStage) -> None:
    if on_stage is not None:
        on_stage(stage)


__all__ = ["combined_translation_glossary", "prepare_document_input"]
