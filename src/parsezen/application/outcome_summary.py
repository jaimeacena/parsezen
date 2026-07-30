"""Build truthful, content-free summaries from completed processing results."""

from __future__ import annotations

from parsezen.domain.estimates import DurationEstimate
from parsezen.domain.jobs import DocumentFormat, DocumentJob
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
    integrity = result.final_integrity_report
    operations: list[str] = ["Conversión"]
    if job.configuration.translation.enabled:
        operations.append("Traducción")
    if job.configuration.refinement.enabled:
        operations.append("Corrección")
    if job.configuration.structure.enabled:
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
            (job.configuration.translation.enabled and job.configuration.translation.manual_review)
            or (job.configuration.refinement.enabled and job.configuration.refinement.manual_review)
            or (job.configuration.structure.enabled and job.configuration.structure.manual_review)
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
    )
