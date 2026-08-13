"""Derive optional, targeted local-AI review recommendations from quality evidence."""

from __future__ import annotations

import re
from collections import Counter

from parsezen.domain.jobs import DocumentFormat, DocumentJob, ReviewRecommendation, ReviewSignal
from parsezen.epub_builder import EpubBookMetadata
from parsezen.epub_conversion import convert_epub, inspect_epub_package
from parsezen.errors import ParsezenError
from parsezen.pipeline.contracts import ProcessResult
from parsezen.pipeline.transform import (
    resolve_targeted_review_positions,
    review_block_fingerprint,
    review_scope_fingerprint,
)
from parsezen.semantic_blocks import SemanticBlock, SemanticRole, analyze_markdown
from parsezen.translation_quality import (
    TranslationIssueKind,
    TranslationQualityIssue,
    natural_language_text,
)

MAX_TARGETED_REVIEW_BLOCKS = 64
_EXCLUDED_ROLES = frozenset(
    {
        SemanticRole.PROVENANCE,
        SemanticRole.CODE,
        SemanticRole.IMAGE,
    }
)
_TRANSLATION_REVIEW_KINDS = frozenset(
    {
        TranslationIssueKind.SOURCE_TEXT,
        TranslationIssueKind.LENGTH,
        TranslationIssueKind.ALIGNMENT,
        TranslationIssueKind.FIDELITY,
    }
)


def recommend_targeted_review(result: ProcessResult) -> ReviewRecommendation | None:
    """Return a bounded recommendation only when evidence identifies concrete blocks."""

    markdown = result.review_markdown
    if not markdown:
        return None
    document = analyze_markdown(markdown)
    reviewable = tuple(block for block in document.blocks if _reviewable(block))
    if not reviewable:
        return None
    review_indexes = {block.position: index for index, block in enumerate(reviewable)}
    selected: set[int] = set()
    counts: Counter[ReviewSignal] = Counter()

    damaged = tuple(block for block in reviewable if _contains_conversion_damage(block.markdown))
    if damaged:
        selected.update(review_indexes[block.position] for block in damaged)
        counts[ReviewSignal.CONVERSION_DAMAGE] += len(damaged)

    pdf_report = result.pdf_quality_report
    if pdf_report is not None:
        issue_pages = {
            issue.page_number
            for issue in pdf_report.issues
            if issue.blocking or len(pdf_report.issues) >= 2
        }
        issue_pages.update(pdf_report.low_confidence_pages)
        page_blocks = tuple(block for block in reviewable if block.page_number in issue_pages)
        if page_blocks:
            selected.update(review_indexes[block.position] for block in page_blocks)
            counts[ReviewSignal.CONVERSION_DAMAGE] += len(issue_pages)

    translation_report = result.translation_quality_report
    if translation_report is not None:
        relevant_issues = tuple(
            issue for issue in translation_report.issues if issue.kind in _TRANSLATION_REVIEW_KINDS
        )
        inconsistent_count = sum(
            count
            for kind, count in translation_report.issues_by_kind.items()
            if kind
            in {
                TranslationIssueKind.LENGTH,
                TranslationIssueKind.ALIGNMENT,
                TranslationIssueKind.FIDELITY,
            }
        )
        for issue in relevant_issues:
            if (
                issue.kind is not TranslationIssueKind.SOURCE_TEXT
                and issue.kind is not TranslationIssueKind.FIDELITY
                and inconsistent_count < 2
            ):
                continue
            position = _review_index_for_issue(issue, reviewable)
            if position is None:
                continue
            selected.add(position)
            counts[
                ReviewSignal.SOURCE_TEXT_RESIDUE
                if issue.kind is TranslationIssueKind.SOURCE_TEXT
                else ReviewSignal.TRANSLATION_INCONSISTENCY
            ] += 1

    ordered = tuple(sorted(selected))
    if not ordered or len(ordered) > MAX_TARGETED_REVIEW_BLOCKS:
        return None
    signal_counts = tuple((signal, counts[signal]) for signal in ReviewSignal if counts[signal])
    return (
        ReviewRecommendation(
            signal_counts,
            ordered,
            review_scope_fingerprint(markdown),
            tuple(review_block_fingerprint(reviewable[position].markdown) for position in ordered),
        )
        if signal_counts
        else None
    )


def rebind_review_recommendation(
    markdown: str,
    recommendation: ReviewRecommendation,
) -> ReviewRecommendation | None:
    """Rebind safe targets after a local EPUB packaging or confirmed-review change."""

    positions = resolve_targeted_review_positions(markdown, recommendation)
    if positions is None:
        return None
    reviewable = tuple(block for block in analyze_markdown(markdown).blocks if _reviewable(block))
    return ReviewRecommendation(
        recommendation.signal_counts,
        positions,
        review_scope_fingerprint(markdown),
        tuple(review_block_fingerprint(reviewable[position].markdown) for position in positions),
    )


def recommendation_summary(recommendation: ReviewRecommendation) -> str:
    """Explain evidence and bounded scope without exposing any document text."""

    labels = {
        ReviewSignal.CONVERSION_DAMAGE: "conversión u OCR",
        ReviewSignal.SOURCE_TEXT_RESIDUE: "texto del idioma original",
        ReviewSignal.TRANSLATION_INCONSISTENCY: "consistencia de traducción",
    }
    evidence = ", ".join(
        f"{count} de {labels[signal]}" for signal, count in recommendation.signal_counts
    )
    blocks = len(recommendation.block_positions)
    return (
        f"Señales: {evidence}. La revisión con IA se limitará a {blocks} "
        f"{'bloque' if blocks == 1 else 'bloques'}; el resto no se enviará al modelo local."
    )


def reconstruct_completed_result(job: DocumentJob) -> ProcessResult:
    """Recover the minimum local result needed to act on a durable recommendation."""

    path = job.result_path
    if path is None or not path.is_file():
        raise ValueError("El resultado recomendado ya no está disponible.")
    if job.configuration.output.format is DocumentFormat.MARKDOWN:
        try:
            markdown = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValueError("No se pudo volver a abrir el resultado Markdown.") from exc
        return ProcessResult(
            final_path=path,
            review_original_path=job.source.path,
            review_markdown=markdown,
            revision_approved=True,
            markdown_organization=job.configuration.output.markdown_organization,
            markdown_include_metadata=job.configuration.output.markdown_include_metadata,
            markdown_include_page_references=(
                job.configuration.output.markdown_include_page_references
            ),
            markdown_source_name=job.source.path.name,
        )
    try:
        converted = convert_epub(path)
        package = inspect_epub_package(path)
    except (OSError, ParsezenError, ValueError) as exc:
        raise ValueError("No se pudo volver a abrir el resultado EPUB.") from exc
    metadata = EpubBookMetadata(
        title=package.title or path.stem,
        language=package.language or "und",
        author=", ".join(package.authors) or None,
        cover_resource=package.cover_path,
        identifiers=package.identifiers,
        publisher=package.publisher,
        publication_date=package.publication_date,
    )
    return ProcessResult(
        final_path=path,
        review_original_path=job.source.path,
        preserved_images=sum(
            resource.media_type.startswith("image/") for resource in converted.resources
        ),
        revision_resources=converted.resources,
        revision_epub_metadata=metadata,
        review_markdown=converted.markdown,
        revision_approved=True,
    )


def _reviewable(block: SemanticBlock) -> bool:
    return block.role not in _EXCLUDED_ROLES and bool(natural_language_text(block.markdown).strip())


def _review_index_for_issue(
    issue: TranslationQualityIssue,
    reviewable: tuple[SemanticBlock, ...],
) -> int | None:
    if issue.segment_number < 1:
        return None
    candidates = (
        tuple(block for block in reviewable if block.role is SemanticRole.HEADING)
        if issue.kind is TranslationIssueKind.FIDELITY
        else reviewable
    )
    index = issue.segment_number - 1
    if not 0 <= index < len(candidates):
        return None
    target_position = candidates[index].position
    return next(
        (
            review_index
            for review_index, block in enumerate(reviewable)
            if block.position == target_position
        ),
        None,
    )


def _contains_conversion_damage(markdown: str) -> bool:
    visible = re.sub(r"<!--[\s\S]*?-->", "", markdown)
    return bool(
        "\ufffd" in visible
        or re.search(r"(?i)\b(?:aviso|warning)\s+OCR\b", visible)
        or re.search(r"\b(?:[^\W\d_]\s+){5,}[^\W\d_]\b", visible)
        or re.search(r"(?i)\b([^\W\d_]{3,})(?:\s+\1){2,}\b", visible)
    )
