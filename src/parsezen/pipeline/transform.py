"""Apply translation and review transformations without publishing outputs."""

from __future__ import annotations

import hashlib
import inspect
import logging
import re
from collections.abc import Callable
from typing import TypedDict

from parsezen.cancellation import CancellationToken, check_cancelled
from parsezen.domain.execution_plan import ExecutionStep
from parsezen.domain.jobs import ReviewRecommendation
from parsezen.domain.process_lifecycle import ProcessStage
from parsezen.epub_builder import reconcile_generated_html_tables
from parsezen.errors import ParsezenError, ProcessingCancelledError
from parsezen.glossary import GlossaryEntry, glossary_fingerprint, protect_glossary
from parsezen.improvement import (
    MAX_INPUT_CHARACTERS,
    ImprovementMode,
    improve_markdown,
    retranslate_residual_title,
    review_translation_markdown,
)
from parsezen.offline_translation import (
    is_offline_translation_pair_installed,
    translate_markdown_offline,
)
from parsezen.pdf_conversion import (
    PdfQualityReport,
    strip_pdf_page_markers,
    strip_pdf_public_markers,
)
from parsezen.pipeline.contracts import (
    PreparedDocument,
    ProcessRequest,
    ProgressCallback,
    StageCallback,
    TransformedDocument,
)
from parsezen.revision import (
    RevisionDraft,
    RevisionKind,
    build_revision_draft,
    split_markdown_blocks,
)
from parsezen.semantic_blocks import (
    DocumentTerm,
    SemanticBlock,
    SemanticDocument,
    SemanticRole,
    analyze_markdown,
    terminology_fingerprint,
)
from parsezen.settings import (
    AppSettings,
    has_specialized_ai_profiles,
    settings_for_review,
    settings_for_translation,
)
from parsezen.translation_quality import (
    LinguisticReviewCoverage,
    LinguisticReviewMode,
    TranslationIssueKind,
    TranslationQualityReport,
    build_translation_quality_report,
    detect_language_code,
    find_titles_with_source_language_residue,
    find_untranslated_title_lines,
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
    focused_source_repair: bool


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
    translation_repair_attempted = 0
    translation_repair_accepted = 0
    translation_settings = settings_for_translation(settings) if settings is not None else None
    review_settings = settings_for_review(settings) if settings is not None else None
    execution_plan = request.execution_plan
    ai_translation_planned = (
        execution_plan.includes(ExecutionStep.TRANSLATE_AI)
        if execution_plan is not None
        else request.improvement_mode is not None
    )
    offline_translation_planned = (
        execution_plan.includes(ExecutionStep.TRANSLATE_OFFLINE)
        if execution_plan is not None
        else request.offline_translation_language is not None
    )
    content_review_planned = (
        execution_plan.includes(ExecutionStep.REVIEW_CONTENT)
        if execution_plan is not None
        else request.review_content
    )
    structure_review_planned = (
        execution_plan.includes(ExecutionStep.REVIEW_STRUCTURE)
        if execution_plan is not None
        else request.review_structure
    )

    def record_translation_repair(attempted: int, accepted: int) -> None:
        nonlocal translation_repair_attempted, translation_repair_accepted
        translation_repair_attempted += attempted
        translation_repair_accepted += accepted

    effective_ai_mode = effective_ai_improvement_mode(request)
    ai_translation_is_redundant = (
        ai_translation_planned
        and request.improvement_mode is ImprovementMode.TRANSLATE
        and translation_is_redundant(
            transformed_markdown,
            request.target_language,
        )
    )
    if ai_translation_planned and not ai_translation_is_redundant:
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
        improvement_settings = (
            translation_settings
            if effective_ai_mode in {ImprovementMode.TRANSLATE, ImprovementMode.CLEAN_AND_TRANSLATE}
            else review_settings
        )
        if improvement_settings is None:
            raise AssertionError("Validated improvement requests always have phase settings.")
        transformed_markdown = improve_markdown(
            protected.text if protected is not None else transformed_markdown,
            effective_ai_mode,
            improvement_settings,
            request.target_language,
            **improvement_arguments,
        )
        if protected is not None:
            transformed_markdown = protected.restore(transformed_markdown)
        if translation_source is not None:
            transformed_markdown = reconcile_generated_html_tables(
                translation_source,
                transformed_markdown,
            )

        check_cancelled(cancellation)
        if translation_source is not None:
            transformed_markdown = repair_translation_warnings(
                request,
                translation_source,
                transformed_markdown,
                settings=translation_settings,
                cancellation=cancellation,
                translation_glossary=translation_glossary,
                work_checkpoints=work_checkpoints,
                on_result=record_translation_repair,
            )
    elif ai_translation_is_redundant:
        LOGGER.info("translation_skipped already_in_target_language=true engine=ai")

    offline_translation_is_redundant = (
        offline_translation_planned
        and request.offline_translation_language is not None
        and translation_is_redundant(
            transformed_markdown,
            request.offline_translation_language,
        )
    )
    if offline_translation_planned and not offline_translation_is_redundant:
        assert request.offline_translation_language is not None
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
        transformed_markdown = reconcile_generated_html_tables(
            translation_source,
            transformed_markdown,
        )
        announce_translation_stage()
        check_cancelled(cancellation)
        transformed_markdown = repair_translation_warnings(
            request,
            translation_source,
            transformed_markdown,
            settings=translation_settings,
            cancellation=cancellation,
            translation_glossary=translation_glossary,
            work_checkpoints=work_checkpoints,
            on_result=record_translation_repair,
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
    bilingual_reviewed_blocks = 0

    def record_bilingual_review_coverage(reviewed_blocks: int) -> None:
        nonlocal bilingual_reviewed_blocks
        bilingual_reviewed_blocks = max(0, reviewed_blocks)

    if content_review_planned:
        if settings is None:
            raise AssertionError("Validated review requests always have settings.")
        if review_settings is None:
            raise AssertionError("Validated review requests always have phase settings.")
        _announce(on_stage, ProcessStage.REVIEWING_CONTENT)
        selected_translation_cleanup = _uses_local_ai_translation_content_cleanup(
            request,
            translation_source,
        )
        if translation_source is not None:
            translation_review_source = translation_source
            transformed_markdown = review_translation_with_checkpoints(
                translation_source,
                transformed_markdown,
                review_settings,
                request,
                on_progress,
                cancellation,
                work_checkpoints,
                quality_report=translation_quality,
                on_reviewed_segments=record_bilingual_review_coverage,
            )
            translated_blocks = (
                max(translation_quality.checked_segments, translation_quality.translated_blocks)
                if translation_quality is not None
                else 0
            )
            if bilingual_reviewed_blocks:
                linguistic_review_mode = (
                    LinguisticReviewMode.INDEPENDENT_BILINGUAL
                    if translated_blocks and bilingual_reviewed_blocks >= translated_blocks
                    else LinguisticReviewMode.TARGETED_BILINGUAL
                )
            if selected_translation_cleanup:
                transformed_markdown = improve_selected_content(
                    transformed_markdown,
                    review_settings,
                    request,
                    on_progress,
                    cancellation,
                    work_checkpoints,
                    semantic_document=analyze_markdown(transformed_markdown),
                    pdf_quality_report=pdf_quality_report,
                )
        elif pdf_quality_report is not None:
            transformed_markdown = improve_selected_content(
                transformed_markdown,
                review_settings,
                request,
                on_progress,
                cancellation,
                work_checkpoints,
                semantic_document=analyze_markdown(transformed_markdown),
                pdf_quality_report=pdf_quality_report,
            )
        elif not selected_translation_cleanup:
            transformed_markdown = improve_with_checkpoints(
                transformed_markdown,
                ImprovementMode.REVIEW_CONTENT,
                review_settings,
                request,
                on_progress,
                cancellation,
                work_checkpoints,
            )
        if transformed_markdown != revision_source:
            revision_kinds.add(RevisionKind.CONTENT)
    if structure_review_planned:
        if settings is None:
            raise AssertionError("Validated review requests always have settings.")
        if review_settings is None:
            raise AssertionError("Validated review requests always have phase settings.")
        _announce(on_stage, ProcessStage.ORGANIZING_STRUCTURE)
        transformed_markdown = improve_with_checkpoints(
            transformed_markdown,
            ImprovementMode.REVIEW_STRUCTURE,
            review_settings,
            request,
            on_progress,
            cancellation,
            work_checkpoints,
        )
        revision_kinds.add(RevisionKind.STRUCTURE)
    transformed_markdown = reconcile_generated_html_tables(
        revision_source,
        transformed_markdown,
    )
    # Quality belongs to the translation that will actually be published. A
    # bilingual or structural review can create a pending revision draft, but
    # its unaccepted proposal must never hide residual source text in the base
    # EPUB. The report was already built from ``revision_source`` immediately
    # after the guarded automatic repair.
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
    review_translation_quality = translation_quality
    if translation_source is not None and transformed_markdown != revision_source:
        review_translation_quality = translation_quality_report(
            request,
            translation_source,
            transformed_markdown,
        )
    linguistic_coverage = linguistic_review_coverage(
        review_translation_quality,
        mode=linguistic_review_mode,
        reviewed_blocks=bilingual_reviewed_blocks,
        independently_verified_blocks=bilingual_reviewed_blocks,
    )

    if preserved_translation_chunks and _preserved_translation_was_fully_repaired(
        len(preserved_translation_chunks),
        attempted=translation_repair_attempted,
        accepted=translation_repair_accepted,
        report=translation_quality,
    ):
        LOGGER.info(
            "translation_preserved_chunks_resolved count=%d repaired_segments=%d",
            len(preserved_translation_chunks),
            translation_repair_accepted,
        )
        preserved_translation_chunks.clear()

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
        strip_pdf_public_markers(published_markdown)
        if source_path.suffix.lower() == ".pdf"
        and not review_required
        and not (
            request.output_format is OutputFormat.MARKDOWN
            and request.markdown_include_page_references
        )
        else published_markdown
    )

    return TransformedDocument(
        transformed_markdown=transformed_markdown,
        translation_quality_report=translation_quality,
        review_translation_quality_report=review_translation_quality,
        linguistic_review_coverage=linguistic_coverage,
        preserved_translation_chunks=tuple(preserved_translation_chunks),
        revision_draft=revision_draft,
        published_markdown=published_markdown,
        review_required=review_required,
        public_markdown=public_markdown,
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
    runtime_settings = (
        settings_for_translation(settings)
        if mode in {ImprovementMode.TRANSLATE, ImprovementMode.CLEAN_AND_TRANSLATE}
        else settings_for_review(settings)
    )
    return improve_markdown(markdown, mode, runtime_settings, **arguments)


def review_translation_with_checkpoints(
    source_markdown: str,
    translated_markdown: str,
    settings: AppSettings,
    request: ProcessRequest,
    on_progress: ProgressCallback | None,
    cancellation: CancellationToken | None,
    checkpoints: WorkCheckpoints | None,
    *,
    quality_report: TranslationQualityReport | None = None,
    on_reviewed_segments: Callable[[int], None] | None = None,
) -> str:
    """Review a translation against its aligned source using only local Ollama."""

    target_language = request.offline_translation_language or request.target_language
    if target_language is None:
        return translated_markdown
    runtime_settings = settings_for_review(settings)
    return review_translation_markdown(
        source_markdown,
        translated_markdown,
        runtime_settings,
        target_language,
        on_progress=on_progress,
        cancellation=cancellation,
        load_checkpoint=checkpoints.load if checkpoints is not None else None,
        save_checkpoint=checkpoints.save if checkpoints is not None else None,
        priority_block_count=max(analyze_markdown(source_markdown).front_matter_blocks, 8),
        quality_report=quality_report,
        on_reviewed_segments=on_reviewed_segments,
    )


def effective_ai_improvement_mode(request: ProcessRequest) -> ImprovementMode | None:
    """Keep translation aligned so a later bilingual verification remains possible."""

    return request.improvement_mode


def epub_translation_resume_key(
    request: ProcessRequest,
    settings: AppSettings | None,
    language_code: str,
    terminology: tuple[DocumentTerm, ...] = (),
    *,
    translation_glossary: tuple[GlossaryEntry, ...] | None = None,
) -> str:
    """Identify text-changing settings for resumable EPUB transformations."""

    effective_mode = effective_ai_improvement_mode(request)
    common = (
        language_code,
        effective_mode.value if effective_mode is not None else "none",
        request.review_content,
        request.offline_translation_language is not None,
    )
    glossary = glossary_fingerprint(
        request.glossary if translation_glossary is None else translation_glossary
    )
    terminology_digest = terminology_fingerprint(terminology)
    if not has_specialized_ai_profiles(settings):
        # Keep this tuple byte-for-byte compatible with pre-specialized jobs.
        return repr(
            (
                "epub-translation-v12",
                *common,
                settings.model if settings is not None else None,
                settings.context_window if settings is not None else None,
                glossary,
                terminology_digest,
            )
        )

    assert settings is not None
    translation_settings = settings_for_translation(settings)
    review_settings = settings_for_review(settings)
    return repr(
        (
            "epub-translation-v13-specialized",
            *common,
            translation_settings.model,
            translation_settings.context_window,
            review_settings.model,
            review_settings.context_window,
            glossary,
            terminology_digest,
        )
    )


def _uses_local_ai_translation_content_cleanup(
    request: ProcessRequest,
    translation_source: str | None,
) -> bool:
    return bool(
        request.review_content
        and translation_source is not None
        and request.offline_translation_language is None
        and request.improvement_mode is ImprovementMode.TRANSLATE
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
        if block.role
        not in {
            SemanticRole.PROVENANCE,
            SemanticRole.CODE,
            SemanticRole.IMAGE,
            SemanticRole.TABLE,
            SemanticRole.TOC,
        }
        and natural_language_text(block.markdown).strip()
        and (block.page_number in problem_pages or contains_conversion_damage(block.markdown))
    )
    if not selected:
        if on_progress is not None:
            on_progress(1, 1)
        return markdown

    replacements: dict[str, str] = {}
    groups = _selected_content_review_groups(selected)
    total = len(groups)
    for current, group in enumerate(groups, start=1):
        check_cancelled(cancellation)
        if on_progress is not None:
            on_progress(current, total)
        source_group = "".join(block.markdown for block in group)
        improved = improve_with_checkpoints(
            source_group,
            ImprovementMode.REVIEW_CONTENT,
            settings,
            request,
            None,
            cancellation,
            checkpoints,
        )
        if improved == source_group:
            continue
        improved_blocks = split_markdown_blocks(improved)
        if len(improved_blocks) != len(group):
            LOGGER.warning(
                "selected_content_review_group_preserved blocks=%d reason=alignment",
                len(group),
            )
            continue
        for block, improved_block in zip(group, improved_blocks, strict=True):
            if improved_block.markdown != block.markdown:
                replacements[block.identifier] = improved_block.markdown
    return "".join(
        replacements.get(block.identifier, block.markdown) for block in semantic_document.blocks
    )


def _selected_content_review_groups(
    selected: tuple[SemanticBlock, ...],
) -> tuple[tuple[SemanticBlock, ...], ...]:
    """Batch selected blocks per source page while retaining exact reassembly boundaries."""

    groups: list[tuple[SemanticBlock, ...]] = []
    pending: list[SemanticBlock] = []
    pending_characters = 0
    pending_page: int | None = None

    def flush() -> None:
        nonlocal pending_characters, pending_page
        if pending:
            groups.append(tuple(pending))
            pending.clear()
        pending_characters = 0
        pending_page = None

    for block in selected:
        page_changed = bool(pending and block.page_number != pending_page)
        exceeds_limit = bool(
            pending and pending_characters + len(block.markdown) > MAX_INPUT_CHARACTERS
        )
        if page_changed or exceeds_limit:
            flush()
        pending.append(block)
        pending_characters += len(block.markdown)
        pending_page = block.page_number
    flush()
    return tuple(groups)


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
    if target_language is None:
        raise AssertionError("A validated translation always has a target language.")
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
        reviewed_blocks = 0
    if independently_verified_blocks is None:
        independently_verified_blocks = 0
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
    on_result: Callable[[int, int], None] | None = None,
) -> str:
    """Retry aligned blocks with source-language residue or a critical fidelity warning."""

    target_language = request.offline_translation_language or request.target_language
    if target_language is None:
        raise AssertionError("A validated translation always has a target language.")
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
            has_title_residue = bool(
                source_language_code
                and (
                    find_untranslated_title_lines(
                        source_segment,
                        current_segment,
                        source_language_code,
                    )
                    or find_titles_with_source_language_residue(
                        source_segment,
                        current_segment,
                        source_language_code,
                    )
                )
            )
            if has_title_residue and source_language_code is not None:
                repaired_title = retranslate_residual_title(
                    source_segment,
                    current_segment,
                    settings,
                    target_language,
                    source_language_code=source_language_code,
                    cancellation=cancellation,
                )
                remaining_title_residue = bool(
                    find_untranslated_title_lines(
                        source_segment,
                        repaired_title,
                        source_language_code,
                    )
                    or find_titles_with_source_language_residue(
                        source_segment,
                        repaired_title,
                        source_language_code,
                    )
                )
                if (
                    remaining_title_residue
                    and "\n" not in source_segment.strip()
                    and is_offline_translation_pair_installed(
                        source_language_code,
                        target_language_code,
                    )
                ):
                    try:
                        offline_title = translate_markdown_offline(
                            source_segment,
                            target_language,
                            source_language_code=source_language_code,
                            cancellation=cancellation,
                        )
                    except ParsezenError:
                        return repaired_title
                    if not (
                        find_untranslated_title_lines(
                            source_segment,
                            offline_title,
                            source_language_code,
                        )
                        or find_titles_with_source_language_residue(
                            source_segment,
                            offline_title,
                            source_language_code,
                        )
                    ):
                        LOGGER.info("translation_title_offline_fallback accepted=true")
                        return offline_title
                return repaired_title
            repair_arguments: _ImprovementArguments = {
                "plain_text": False,
                "focused_source_repair": True,
            }
            if cancellation is not None:
                repair_arguments["cancellation"] = cancellation
            if work_checkpoints is not None:
                repair_arguments["load_checkpoint"] = work_checkpoints.load
                repair_arguments["save_checkpoint"] = work_checkpoints.save
            repaired = improve_markdown(
                protected.text,
                ImprovementMode.TRANSLATE,
                settings,
                target_language,
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
    if on_result is not None:
        on_result(repair.attempted_segments, repair.repaired_segments)
    if source_language_code is None or source_language_code == target_language_code:
        return reconcile_generated_html_tables(translated, repair.translated)
    restored = restore_changed_third_language_headings(
        source,
        repair.translated,
        source_language=source_language_code,
        target_language=target_language_code,
    )
    if not numeric_tokens_are_conserved(repair.translated, restored):
        LOGGER.warning("translation_third_language_heading_restore_preserved numbers_changed=true")
        restored = repair.translated
    return reconcile_generated_html_tables(translated, restored)


def _preserved_translation_was_fully_repaired(
    preserved_chunks: int,
    *,
    attempted: int,
    accepted: int,
    report: TranslationQualityReport | None,
) -> bool:
    """Resolve historical rejections only with complete, independently checked evidence."""

    return bool(
        preserved_chunks > 0
        and attempted >= preserved_chunks
        and accepted >= preserved_chunks
        and report is not None
        and TranslationIssueKind.SOURCE_TEXT not in report.issues_by_kind
    )


def _announce(on_stage: StageCallback | None, stage: ProcessStage) -> None:
    if on_stage is not None:
        on_stage(stage)


__all__ = [
    "effective_ai_improvement_mode",
    "epub_translation_resume_key",
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
