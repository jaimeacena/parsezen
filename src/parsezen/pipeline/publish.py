"""Publish one transformed document through the existing atomic output boundaries."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path, PurePosixPath

from parsezen.cancellation import CancellationToken, check_cancelled
from parsezen.conversion import CONVERSION_REQUIRED_EXTENSIONS, materialize_converted_markdown
from parsezen.document_model import ConvertedResource
from parsezen.domain.process_lifecycle import ProcessStage
from parsezen.epub_builder import EpubBookMetadata, build_epub, validate_epub_file
from parsezen.errors import RequestValidationError
from parsezen.final_integrity import (
    IntegrityLedger,
    binary_integrity_capture,
    merge_integrity_reports,
    text_integrity_capture,
)
from parsezen.improvement import ImprovementMode
from parsezen.output import (
    write_conversion_output,
    write_epub_output,
    write_improvement_outputs,
)
from parsezen.pdf_conversion import render_pdf_page_cover
from parsezen.pipeline.contracts import (
    PreparedDocument,
    ProcessRequest,
    ProcessResult,
    StageCallback,
    TransformedDocument,
)
from parsezen.pipeline.transform import review_is_required
from parsezen.revision import RevisionDecision, RevisionDraft, build_revision_draft
from parsezen.settings import AppSettings
from parsezen.translation_quality import (
    detect_language_code,
    resolve_language_code,
)
from parsezen.work_checkpoints import WorkCheckpoints, prune_work_checkpoint_cache
from parsezen.workflow import OutputFormat

EPUB_COVER_MEDIA_TYPES = {
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}

CoverRenderer = Callable[[Path, int], bytes]


def finish_work_checkpoints(
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


def publish_transformed_document(
    prepared: PreparedDocument,
    transformed: TransformedDocument,
    request: ProcessRequest,
    on_stage: StageCallback | None,
    settings: AppSettings | None,
    cancellation: CancellationToken | None,
    work_checkpoints: WorkCheckpoints | None,
    pdf_checkpoints: WorkCheckpoints | None,
    work_checkpoint_root: Path | None,
    *,
    render_cover: CoverRenderer = render_pdf_page_cover,
) -> ProcessResult:
    """Build and atomically publish the selected output format."""

    source_path = prepared.source_path
    resolved_page_range = prepared.resolved_page_range
    output_stem = prepared.output_stem
    pdf_quality_report = prepared.pdf_quality_report
    source_cover_path = prepared.source_cover_path
    converted_resources = prepared.converted_resources
    markdown = prepared.markdown
    problematic_pdf_pages = prepared.problematic_pdf_pages
    semantic_document = prepared.semantic_document
    transformed_markdown = transformed.transformed_markdown
    translation_quality_report = transformed.translation_quality_report
    linguistic_review_coverage = transformed.linguistic_review_coverage
    preserved_translation_chunks = transformed.preserved_translation_chunks
    revision_draft = transformed.revision_draft
    published_markdown = transformed.published_markdown
    review_required = transformed.review_required
    public_markdown = transformed.public_markdown
    generated_epub = request.output_format is OutputFormat.EPUB

    if generated_epub:
        check_cancelled(cancellation)
        _announce(on_stage, ProcessStage.BUILDING_EPUB)
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
                render_cover(source_path, page_number),
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
        _announce(on_stage, ProcessStage.WRITING)
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
        _announce(on_stage, ProcessStage.COMPLETED)
        _finish_both_checkpoints(
            work_checkpoints,
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
            linguistic_review_coverage=linguistic_review_coverage,
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
        _announce(on_stage, ProcessStage.WRITING)
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
            markdown_organization=request.markdown_organization,
            markdown_include_metadata=request.markdown_include_metadata,
            markdown_include_page_references=request.markdown_include_page_references,
        )
        _announce(on_stage, ProcessStage.COMPLETED)
        _finish_both_checkpoints(
            work_checkpoints,
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
        effective_review_required = review_is_required(
            materialized_revision,
            pdf_quality_report,
            translation_quality_report,
            bool(preserved_translation_chunks),
        )
        return ProcessResult(
            final_path=final_path,
            raw_markdown_path=raw_path,
            review_original_path=raw_path if raw_path is not None else source_path,
            problematic_pdf_pages=problematic_pdf_pages,
            pdf_quality_report=pdf_quality_report,
            exhaustive_pdf_ocr_used=request.force_pdf_ocr,
            translation_quality_report=translation_quality_report,
            linguistic_review_coverage=linguistic_review_coverage,
            preserved_translation_chunks=tuple(preserved_translation_chunks),
            preserved_images=len(converted_resources),
            revision_draft=materialized_revision,
            review_markdown=(
                materialized_revision.proposed_markdown
                if materialized_revision is not None
                else public_markdown
            ),
            review_required=effective_review_required,
            final_integrity_report=integrity.report,
            front_matter_blocks=semantic_document.front_matter_blocks,
            toc_blocks=semantic_document.toc_blocks,
            terminology_terms=len(semantic_document.terms),
            markdown_organization=request.markdown_organization,
            markdown_include_metadata=request.markdown_include_metadata,
            markdown_include_page_references=request.markdown_include_page_references,
            markdown_source_name=source_path.name,
        )

    check_cancelled(cancellation)
    _announce(on_stage, ProcessStage.WRITING)
    integrity = text_integrity_capture(public_markdown, markdown=True)
    final_path = write_conversion_output(
        source_path,
        public_markdown,
        output_directory=request.output_directory,
        output_stem=output_stem,
        resources=converted_resources,
        image_output_directory=request.image_output_directory,
        validate_staged=integrity,
        markdown_organization=request.markdown_organization,
        markdown_include_metadata=request.markdown_include_metadata,
        markdown_include_page_references=request.markdown_include_page_references,
    )
    _announce(on_stage, ProcessStage.COMPLETED)
    _finish_both_checkpoints(
        work_checkpoints,
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
        review_markdown=published_markdown,
        review_required=review_is_required(None, pdf_quality_report, None),
        final_integrity_report=integrity.report,
        front_matter_blocks=semantic_document.front_matter_blocks,
        toc_blocks=semantic_document.toc_blocks,
        terminology_terms=len(semantic_document.terms),
        markdown_organization=request.markdown_organization,
        markdown_include_metadata=request.markdown_include_metadata,
        markdown_include_page_references=request.markdown_include_page_references,
        markdown_source_name=source_path.name,
    )


def _finish_both_checkpoints(
    work_checkpoints: WorkCheckpoints | None,
    pdf_checkpoints: WorkCheckpoints | None,
    *,
    settings: AppSettings | None,
    root: Path | None,
    cleanup_allowed: bool,
) -> None:
    finish_work_checkpoints(
        work_checkpoints,
        settings=settings,
        root=root,
        cleanup_allowed=cleanup_allowed,
    )
    finish_work_checkpoints(
        pdf_checkpoints,
        settings=settings,
        root=root,
        cleanup_allowed=cleanup_allowed,
    )


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
    safe_proposed = draft.render(
        {
            change.identifier: RevisionDecision.ACCEPTED
            for change in draft.changes
            if change.proposal_selectable
        }
    )
    if safe_proposed == draft.original_markdown:
        return None
    original = materialize_converted_markdown(
        draft.original_markdown,
        resources_reference,
    )
    proposed = materialize_converted_markdown(
        safe_proposed,
        resources_reference,
    )
    materialized = build_revision_draft(original, proposed, kinds=draft.kinds)
    if len(materialized.changes) == len(draft.changes):
        materialized = replace(
            materialized,
            changes=tuple(
                replace(
                    materialized_change,
                    risk=source_change.risk,
                    risk_reason=source_change.risk_reason,
                    proposal_selectable=source_change.proposal_selectable,
                )
                for materialized_change, source_change in zip(
                    materialized.changes,
                    draft.changes,
                    strict=True,
                )
            ),
        )
    return materialized if materialized.changes else None


def _validate_epub_path(path: Path) -> None:
    validate_epub_file(path)


def _announce(on_stage: StageCallback | None, stage: ProcessStage) -> None:
    if on_stage is not None:
        on_stage(stage)


__all__ = [
    "EPUB_COVER_MEDIA_TYPES",
    "finish_work_checkpoints",
    "publish_transformed_document",
    "review_is_required",
]
