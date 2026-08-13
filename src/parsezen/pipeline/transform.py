"""Apply translation and review transformations without publishing outputs."""

from __future__ import annotations

import hashlib
import inspect
import logging
import re
from collections.abc import Callable
from typing import TypedDict

from parsezen.cancellation import CancellationToken, check_cancelled
from parsezen.domain.jobs import ReviewRecommendation
from parsezen.domain.process_lifecycle import ProcessStage
from parsezen.errors import ParsezenError, ProcessingCancelledError
from parsezen.glossary import GlossaryEntry, protect_glossary
from parsezen.improvement import (
    ImprovementMode,
    improve_markdown,
    review_translation_markdown,
)
from parsezen.offline_translation import translate_markdown_offline
from parsezen.pdf_conversion import PdfQualityReport, strip_pdf_page_markers
from parsezen.pipeline.contracts import (
    PreparedDocument,
    ProcessRequest,
    ProgressCallback,
    StageCallback,
    TransformedDocument,
)
from parsezen.revision import RevisionDraft, RevisionKind, build_revision_draft
from parsezen.semantic_blocks import (
    SemanticBlock,
    SemanticDocument,
    SemanticRole,
    analyze_markdown,
)
from parsezen.settings import AppSettings
from parsezen.translation_quality import (
    LinguisticReviewCoverage,
    LinguisticReviewMode,
    TranslationQualityReport,
    build_translation_quality_report,
    detect_language_code,
    natural_language_text,
    numeric_tokens_are_conserved,
    repair_untranslated_source_text,
    resolve_language_code,
    restore_changed_third_language_headings,
)
from parsezen.work_checkpoints import WorkCheckpoints
from parsezen.workflow import OutputFormat

LOGGER = logging.getLogger(__name__)


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


def review_is_required(
    revision_draft: RevisionDraft | None,
    pdf_report: PdfQualityReport | None,
    translation_report: TranslationQualityReport | None,
    unsafe_translation_preserved: bool = False,
) -> bool:
    """Distinguish optional advice from issues needing an explicit decision."""

    del translation_report
    return bool(
        revision_draft is not None
        or (pdf_report is not None and any(issue.blocking for issue in pdf_report.issues))
        or unsafe_translation_preserved
    )


def transform_prepared_document(
    prepared: PreparedDocument,
    request: ProcessRequest,
    on_stage: StageCallback | None,
    on_progress: ProgressCallback | None,
    settings: AppSettings | None,
    cancellation: CancellationToken | None,
    work_checkpoints: WorkCheckpoints | None,
    *,
    generated_epub: bool,
) -> TransformedDocument:
    """Translate and build a review proposal without writing final output."""

    source_path = prepared.source_path
    markdown = prepared.markdown
    pdf_quality_report = prepared.pdf_quality_report
    translation_glossary = prepared.translation_glossary
    transformed_markdown = markdown
    translation_source: str | None = None
    preserved_translation_chunks: list[int] = []
    effective_ai_mode = effective_ai_improvement_mode(request)
    ai_translation_is_redundant = (
        request.improvement_mode is ImprovementMode.TRANSLATE
        and translation_is_redundant(
            transformed_markdown,
            request.target_language,
        )
    )
    if request.improvement_mode is not None and not ai_translation_is_redundant:
        if settings is None:
            raise AssertionError("Validated improvement requests always have settings.")
        if effective_ai_mode is None:
            raise AssertionError("An enabled AI transformation always has an effective mode.")
        _announce(
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
            transformed_markdown = repair_translation_warnings(
                request,
                translation_source,
                transformed_markdown,
                settings=settings,
                cancellation=cancellation,
                translation_glossary=translation_glossary,
                work_checkpoints=work_checkpoints,
            )
    elif ai_translation_is_redundant:
        LOGGER.info("translation_skipped already_in_target_language=true engine=ai")

    offline_translation_is_redundant = (
        request.offline_translation_language is not None
        and translation_is_redundant(
            transformed_markdown,
            request.offline_translation_language,
        )
    )
    if request.offline_translation_language is not None and not offline_translation_is_redundant:
        _announce(on_stage, ProcessStage.PREPARING_TRANSLATION)
        translation_source = transformed_markdown
        translation_stage_announced = False

        def announce_translation_stage() -> None:
            nonlocal translation_stage_announced
            if translation_stage_announced:
                return
            translation_stage_announced = True
            _announce(on_stage, ProcessStage.TRANSLATING)

        translation_arguments: _OfflineTranslationArguments = {
            "on_progress": on_progress,
            "on_engine_ready": announce_translation_stage,
        }
        if cancellation is not None:
            translation_arguments["cancellation"] = cancellation
        translation_parameters = inspect.signature(translate_markdown_offline).parameters
        if work_checkpoints is not None and "load_checkpoint" in translation_parameters:
            translation_arguments["load_checkpoint"] = work_checkpoints.load
            translation_arguments["save_checkpoint"] = work_checkpoints.save
        protected = protect_glossary(transformed_markdown, translation_glossary)
        transformed_markdown = translate_markdown_offline(
            protected.text,
            request.offline_translation_language,
            **translation_arguments,
        )
        transformed_markdown = protected.restore(transformed_markdown)
        announce_translation_stage()
        check_cancelled(cancellation)
        transformed_markdown = repair_translation_warnings(
            request,
            translation_source,
            transformed_markdown,
            settings=settings,
            cancellation=cancellation,
            translation_glossary=translation_glossary,
            work_checkpoints=work_checkpoints,
        )
    elif offline_translation_is_redundant:
        LOGGER.info("translation_skipped already_in_target_language=true engine=offline")

    translation_quality = translation_quality_report(
        request,
        translation_source,
        transformed_markdown,
    )
    revision_source = transformed_markdown
    revision_kinds: set[RevisionKind] = set()
    translation_review_source: str | None = None
    linguistic_review_mode = LinguisticReviewMode.NOT_REVIEWED
    if request.review_content:
        if settings is None:
            raise AssertionError("Validated review requests always have settings.")
        _announce(on_stage, ProcessStage.REVIEWING_CONTENT)
        if _content_review_was_fused(request, effective_ai_mode, translation_source):
            linguistic_review_mode = LinguisticReviewMode.CORRECTED_DURING_TRANSLATION
            transformed_markdown = improve_selected_content(
                transformed_markdown,
                settings,
                request,
                on_progress,
                cancellation,
                work_checkpoints,
                semantic_document=analyze_markdown(transformed_markdown),
                pdf_quality_report=pdf_quality_report,
            )
        elif translation_source is not None:
            linguistic_review_mode = LinguisticReviewMode.INDEPENDENT_BILINGUAL
            translation_review_source = translation_source
            transformed_markdown = review_translation_with_checkpoints(
                translation_source,
                transformed_markdown,
                settings,
                request,
                on_progress,
                cancellation,
                work_checkpoints,
            )
        else:
            transformed_markdown = improve_with_checkpoints(
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
        _announce(on_stage, ProcessStage.ORGANIZING_STRUCTURE)
        transformed_markdown = improve_with_checkpoints(
            transformed_markdown,
            ImprovementMode.REVIEW_STRUCTURE,
            settings,
            request,
            on_progress,
            cancellation,
            work_checkpoints,
        )
        revision_kinds.add(RevisionKind.STRUCTURE)
    if translation_source is not None and (
        translation_review_source is not None or request.review_structure
    ):
        translation_quality = translation_quality_report(
            request,
            translation_source,
            transformed_markdown,
        )
    revision_candidate = (
        build_revision_draft(
            revision_source,
            transformed_markdown,
            kinds=frozenset(revision_kinds),
            translation_source_markdown=translation_review_source,
            translation_target_language=(
                request.offline_translation_language or request.target_language
            ),
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
    linguistic_coverage = linguistic_review_coverage(
        translation_quality,
        mode=linguistic_review_mode,
    )

    published_markdown = revision_source if revision_draft is not None else transformed_markdown
    review_required = (
        review_is_required(
            revision_draft,
            pdf_quality_report,
            translation_quality,
            bool(preserved_translation_chunks),
        )
        or generated_epub
    )
    public_markdown = (
        strip_pdf_page_markers(published_markdown)
        if source_path.suffix.lower() == ".pdf"
        and not review_required
        and not (
            request.output_format is OutputFormat.MARKDOWN
            and request.markdown_include_page_references
        )
        else published_markdown
    )

    return TransformedDocument(
        transformed_markdown,
        translation_quality,
        linguistic_coverage,
        tuple(preserved_translation_chunks),
        revision_draft,
        published_markdown,
        review_required,
        public_markdown,
    )


def improve_with_checkpoints(
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

    del request
    arguments: _ImprovementArguments = {
        "on_progress": on_progress,
        "plain_text": False if plain_text is None else plain_text,
    }
    if cancellation is not None:
        arguments["cancellation"] = cancellation
    if checkpoints is not None:
        arguments["load_checkpoint"] = checkpoints.load
        arguments["save_checkpoint"] = checkpoints.save
    return improve_markdown(markdown, mode, settings, **arguments)


def review_translation_with_checkpoints(
    source_markdown: str,
    translated_markdown: str,
    settings: AppSettings,
    request: ProcessRequest,
    on_progress: ProgressCallback | None,
    cancellation: CancellationToken | None,
    checkpoints: WorkCheckpoints | None,
) -> str:
    """Review a translation against its aligned source using only local Ollama."""

    target_language = request.offline_translation_language or request.target_language
    if target_language is None:
        return translated_markdown
    return review_translation_markdown(
        source_markdown,
        translated_markdown,
        settings,
        target_language,
        on_progress=on_progress,
        cancellation=cancellation,
        load_checkpoint=checkpoints.load if checkpoints is not None else None,
        save_checkpoint=checkpoints.save if checkpoints is not None else None,
        priority_block_count=max(analyze_markdown(source_markdown).front_matter_blocks, 8),
    )


def effective_ai_improvement_mode(request: ProcessRequest) -> ImprovementMode | None:
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


def improve_selected_content(
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
        if block.role not in {SemanticRole.PROVENANCE, SemanticRole.CODE, SemanticRole.IMAGE}
        and (block.page_number in problem_pages or contains_conversion_damage(block.markdown))
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
        improved = improve_with_checkpoints(
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


def reviewable_semantic_blocks(document: SemanticDocument) -> tuple[SemanticBlock, ...]:
    return tuple(
        block
        for block in document.blocks
        if block.role not in {SemanticRole.PROVENANCE, SemanticRole.CODE, SemanticRole.IMAGE}
        and natural_language_text(block.markdown).strip()
    )


def review_scope_fingerprint(markdown: str) -> str:
    blocks = reviewable_semantic_blocks(analyze_markdown(markdown))
    canonical = "\n\0\n".join(review_block_fingerprint(block.markdown) for block in blocks)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def review_block_fingerprint(markdown: str) -> str:
    public_markdown = re.sub(r"<!--[\s\S]*?-->", " ", strip_pdf_page_markers(markdown))
    visible = natural_language_text(public_markdown)
    numbers = "|".join(re.findall(r"\d+(?:[.,]\d+)*", public_markdown))
    return hashlib.sha256(f"{visible}\0{numbers}".encode()).hexdigest()


def resolve_targeted_review_positions(
    markdown: str,
    recommendation: ReviewRecommendation,
) -> tuple[int, ...] | None:
    blocks = reviewable_semantic_blocks(analyze_markdown(markdown))
    return resolve_review_positions(blocks, recommendation)


def resolve_review_positions(
    blocks: tuple[SemanticBlock, ...],
    recommendation: ReviewRecommendation,
) -> tuple[int, ...] | None:
    positions = recommendation.block_positions
    valid_positions = bool(positions) and max(positions) < len(blocks)
    current_scope = hashlib.sha256(
        "\n\0\n".join(review_block_fingerprint(block.markdown) for block in blocks).encode("utf-8")
    ).hexdigest()
    if recommendation.scope_fingerprint is None or (
        valid_positions and recommendation.scope_fingerprint == current_scope
    ):
        return positions if valid_positions else None
    if not recommendation.block_fingerprints:
        return None
    candidates: dict[str, list[int]] = {}
    for position, block in enumerate(blocks):
        candidates.setdefault(review_block_fingerprint(block.markdown), []).append(position)
    resolved: list[int] = []
    for fingerprint in recommendation.block_fingerprints:
        matches = candidates.get(fingerprint, [])
        if len(matches) != 1:
            return None
        resolved.append(matches[0])
    ordered = tuple(sorted(set(resolved)))
    return ordered if len(ordered) == len(resolved) else None


def contains_conversion_damage(markdown: str) -> bool:
    visible = re.sub(r"<!--[\s\S]*?-->", "", markdown)
    return bool(
        "\ufffd" in visible
        or re.search(r"(?i)\b(?:aviso|warning)\s+OCR\b", visible)
        or re.search(r"\b(?:[^\W\d_]\s+){5,}[^\W\d_]\b", visible)
        or re.search(r"(?i)\b([^\W\d_]{3,})(?:\s+\1){2,}\b", visible)
    )


def translation_quality_report(
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


def linguistic_review_coverage(
    report: TranslationQualityReport | None,
    *,
    mode: LinguisticReviewMode,
    reviewed_blocks: int | None = None,
    independently_verified_blocks: int | None = None,
) -> LinguisticReviewCoverage | None:
    """Describe semantic coverage without treating heuristic checks as verification."""

    if report is None:
        return None
    translated_blocks = max(0, report.checked_segments, report.translated_blocks)
    if reviewed_blocks is None:
        reviewed_blocks = translated_blocks if mode is not LinguisticReviewMode.NOT_REVIEWED else 0
    if independently_verified_blocks is None:
        independently_verified_blocks = (
            translated_blocks if mode is LinguisticReviewMode.INDEPENDENT_BILINGUAL else 0
        )
    return LinguisticReviewCoverage(
        mode=mode,
        translated_blocks=translated_blocks,
        automatically_checked_blocks=min(translated_blocks, max(0, report.checked_segments)),
        semantically_reviewed_blocks=min(translated_blocks, max(0, reviewed_blocks)),
        independently_verified_blocks=min(
            translated_blocks,
            max(0, independently_verified_blocks),
        ),
        remaining_issues=max(0, report.total_issues),
    )


def translation_is_redundant(markdown: str, target_language: str | None) -> bool:
    target_language_code = resolve_language_code(target_language)
    if target_language_code is None:
        return False
    source_language_code = detect_language_code(markdown)
    return source_language_code is not None and source_language_code == target_language_code


def repair_translation_warnings(
    request: ProcessRequest,
    source: str,
    translated: str,
    *,
    settings: AppSettings | None,
    cancellation: CancellationToken | None,
    source_language_code: str | None = None,
    translation_glossary: tuple[GlossaryEntry, ...] | None = None,
    work_checkpoints: WorkCheckpoints | None = None,
) -> str:
    """Retry aligned blocks with source-language residue or a critical fidelity warning."""

    target_language = request.offline_translation_language or request.target_language
    target_language_code = resolve_language_code(target_language)
    if target_language_code is None:
        raise AssertionError("A validated translation always has a supported target language.")
    source_language_code = source_language_code or detect_language_code(source, minimum_letters=80)
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
            repair_arguments: _ImprovementArguments = {"plain_text": False}
            if cancellation is not None:
                repair_arguments["cancellation"] = cancellation
            if work_checkpoints is not None:
                repair_arguments["load_checkpoint"] = work_checkpoints.load
                repair_arguments["save_checkpoint"] = work_checkpoints.save
            repaired = improve_markdown(
                protected.text,
                ImprovementMode.TRANSLATE,
                settings,
                request.target_language,
                source_language_code=source_language_code,
                **repair_arguments,
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
            effective_ai_improvement_mode(request) is not ImprovementMode.CLEAN_AND_TRANSLATE
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
    restored = restore_changed_third_language_headings(
        source,
        repair.translated,
        source_language=source_language_code,
        target_language=target_language_code,
    )
    if not numeric_tokens_are_conserved(repair.translated, restored):
        LOGGER.warning("translation_third_language_heading_restore_preserved numbers_changed=true")
        return repair.translated
    return restored


def _announce(on_stage: StageCallback | None, stage: ProcessStage) -> None:
    if on_stage is not None:
        on_stage(stage)


__all__ = [
    "effective_ai_improvement_mode",
    "improve_with_checkpoints",
    "linguistic_review_coverage",
    "repair_translation_warnings",
    "review_is_required",
    "resolve_targeted_review_positions",
    "review_block_fingerprint",
    "review_scope_fingerprint",
    "review_translation_with_checkpoints",
    "transform_prepared_document",
    "translation_quality_report",
]
