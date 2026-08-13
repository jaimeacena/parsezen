"""Build truthful, content-free summaries from completed processing results."""

from __future__ import annotations

from parsezen.domain.estimates import DurationEstimate
from parsezen.domain.jobs import DocumentFormat, DocumentJob, ProcessingPlan
from parsezen.domain.outcomes import EarlyCheckReport, OutcomeSummary
from parsezen.domain.reviews import ReviewChoice, ReviewSession
from parsezen.processing import ProcessResult


def build_outcome_summary(
    job: DocumentJob,
    result: ProcessResult,
    *,
    reviews: tuple[ReviewSession, ...] = (),
    duration_seconds: int | None = None,
    estimate: DurationEstimate | None = None,
    early_check: EarlyCheckReport | None = None,
) -> OutcomeSummary:
    """Describe technical integrity, detected incidents and human review separately."""

    review_units = tuple(unit for review in reviews for unit in review.units)
    pdf_report = result.pdf_quality_report
    translation_report = result.translation_quality_report
    linguistic_coverage = result.linguistic_review_coverage
    integrity = result.final_integrity_report
    recommendation = job.review_recommendation
    operations: list[str] = ["Conversión"]
    if job.configuration.translation.enabled:
        operations.append("Traducción")
    reviewed = job.configuration.plan is ProcessingPlan.LOCAL_AI_REVIEWED
    if reviewed:
        operations.append("Corrección")
    if reviewed and job.configuration.output.format is DocumentFormat.EPUB:
        operations.append("Pre-organización")
    if job.configuration.output.format is DocumentFormat.EPUB:
        operations.append("Edición EPUB")

    return OutcomeSummary(
        output_format=job.configuration.output.format.value.upper(),
        operations=tuple(operations),
        processed_pages=len(pdf_report.processed_pages) if pdf_report is not None else 0,
        ocr_pages=len(pdf_report.ocr_pages) if pdf_report is not None else 0,
        preserved_images=result.preserved_images,
        chapters=result.epub_chapters,
        conversion_issues=len(pdf_report.issues) if pdf_report is not None else 0,
        translation_issues=(
            translation_report.total_issues if translation_report is not None else 0
        ),
        linguistic_review_mode=(
            linguistic_coverage.mode.value if linguistic_coverage is not None else None
        ),
        translation_checked_blocks=(
            linguistic_coverage.automatically_checked_blocks
            if linguistic_coverage is not None
            else 0
        ),
        translation_reviewed_blocks=(
            linguistic_coverage.semantically_reviewed_blocks
            if linguistic_coverage is not None
            else 0
        ),
        translation_independent_blocks=(
            linguistic_coverage.independently_verified_blocks
            if linguistic_coverage is not None
            else 0
        ),
        translation_unreviewed_blocks=(
            linguistic_coverage.semantically_unreviewed_blocks
            if linguistic_coverage is not None
            else 0
        ),
        preserved_segments=len(result.preserved_translation_chunks),
        review_units=len(review_units),
        review_changes=sum(
            unit.choice in {ReviewChoice.PROPOSED, ReviewChoice.EDITED} for unit in review_units
        ),
        review_edits=sum(unit.choice is ReviewChoice.EDITED for unit in review_units),
        review_originals=sum(unit.choice is ReviewChoice.ORIGINAL for unit in review_units),
        editor_completed=(
            job.configuration.output.format is DocumentFormat.EPUB and result.revision_approved
        ),
        manual_review_expected=(
            job.configuration.translation.enabled
            or reviewed
            or job.configuration.output.format is DocumentFormat.EPUB
        ),
        integrity_verified=bool(integrity is not None and integrity.verified),
        integrity_checks=len(integrity.checks) if integrity is not None else 0,
        integrity_warnings=(
            sum(finding.severity.value == "warning" for finding in integrity.findings)
            if integrity is not None
            else 0
        ),
        duration_seconds=duration_seconds,
        estimate_lower_seconds=(estimate.lower_seconds if estimate is not None else None),
        estimate_upper_seconds=(estimate.upper_seconds if estimate is not None else None),
        early_check_pages=(len(early_check.sampled_pages) if early_check is not None else 0),
        early_check_warnings=(early_check.warning_pages if early_check is not None else 0),
        ai_review_recommended=recommendation is not None,
        ai_review_blocks=(len(recommendation.block_positions) if recommendation is not None else 0),
        ai_review_signals=(recommendation.signal_total if recommendation is not None else 0),
    )
