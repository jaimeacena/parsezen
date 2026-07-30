"""Derive the ordered human-review phases required by one worker result."""

from __future__ import annotations

from dataclasses import dataclass

from parsezen.domain.jobs import JobConfiguration
from parsezen.domain.reviews import ReviewKind
from parsezen.domain.stages import StageKind
from parsezen.processing import ProcessResult
from parsezen.revision import RevisionKind


@dataclass(frozen=True, slots=True)
class ReviewStep:
    kind: ReviewKind
    stage: StageKind


@dataclass(frozen=True, slots=True)
class ReviewWorkload:
    """Real number of review units represented by one progress segment."""

    kind: ReviewKind
    unit_count: int


def review_steps_for_result(
    result: ProcessResult,
    configuration: JobConfiguration,
) -> tuple[ReviewStep, ...]:
    """Return only real review work, in chronological processing order."""

    steps: list[ReviewStep] = []
    pdf_report = result.pdf_quality_report
    if result.problematic_pdf_pages or (pdf_report is not None and bool(pdf_report.issues)):
        steps.append(ReviewStep(ReviewKind.OCR, StageKind.PREPARE))
    translation_report = result.translation_quality_report
    if translation_report is not None and bool(translation_report.issues):
        steps.append(ReviewStep(ReviewKind.TRANSLATION, StageKind.TRANSLATE))
    draft = result.revision_draft
    if draft is not None and any(change.kind is RevisionKind.CONTENT for change in draft.changes):
        steps.append(ReviewStep(ReviewKind.REFINEMENT, StageKind.REFINE))
    if draft is not None and any(change.kind is RevisionKind.STRUCTURE for change in draft.changes):
        steps.append(ReviewStep(ReviewKind.STRUCTURE, StageKind.STRUCTURE))

    if steps or not result.review_required:
        return tuple(steps)
    if result.final_path.suffix.casefold() == ".epub" and result.revision_epub_metadata is not None:
        return (ReviewStep(ReviewKind.STRUCTURE, StageKind.PUBLISH),)
    if configuration.structure.enabled:
        return (ReviewStep(ReviewKind.STRUCTURE, StageKind.STRUCTURE),)
    if configuration.refinement.enabled:
        return (ReviewStep(ReviewKind.REFINEMENT, StageKind.REFINE),)
    if configuration.translation.enabled:
        return (ReviewStep(ReviewKind.TRANSLATION, StageKind.TRANSLATE),)
    return (ReviewStep(ReviewKind.CONVERSION_WARNING, StageKind.PREPARE),)


def review_workload_for_result(
    result: ProcessResult,
    configuration: JobConfiguration,
) -> tuple[ReviewWorkload, ...]:
    """Measure each planned review type instead of assigning equal visual widths."""

    steps = review_steps_for_result(result, configuration)
    draft = result.revision_draft
    counts = {
        ReviewKind.OCR: len(result.pdf_quality_report.issues)
        if result.pdf_quality_report is not None
        else len(result.problematic_pdf_pages),
        ReviewKind.TRANSLATION: len(result.translation_quality_report.issues)
        if result.translation_quality_report is not None
        else 0,
        ReviewKind.REFINEMENT: sum(change.kind is RevisionKind.CONTENT for change in draft.changes)
        if draft is not None
        else 0,
        ReviewKind.STRUCTURE: sum(change.kind is RevisionKind.STRUCTURE for change in draft.changes)
        if draft is not None
        else 0,
        ReviewKind.CONVERSION_WARNING: 1,
    }
    return tuple(ReviewWorkload(step.kind, max(1, counts[step.kind])) for step in steps)
