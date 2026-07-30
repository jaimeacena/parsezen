"""Apply one phase review without repeating unrelated completed work."""

from __future__ import annotations

from dataclasses import dataclass

from parsezen.application.planner import activate_next_stage
from parsezen.domain.jobs import DocumentJob
from parsezen.domain.reviews import ReviewChoice, ReviewSession, ReviewStatus, ReviewUnit
from parsezen.domain.stages import StageStatus


@dataclass(frozen=True, slots=True)
class AppliedReview:
    job: DocumentJob
    review: ReviewSession
    selected_artifact_ids: tuple[str, ...]


def apply_phase_review(job: DocumentJob, review: ReviewSession) -> AppliedReview:
    """Complete the exact blocked phase after every required decision exists."""

    if review.job_id != job.id or review.input_version != job.configuration_revision:
        raise ValueError("The review no longer matches this document configuration.")
    if review.status is not ReviewStatus.PENDING:
        raise ValueError("Only a pending review can be applied.")
    stage = job.stage(review.stage)
    if stage.status is not StageStatus.BLOCKED_FOR_REVIEW or stage.review_id != review.id:
        raise ValueError("The document is not blocked by this review.")
    applied = review.apply()
    selected = selected_artifact_ids(applied)
    completed = stage.transition(
        StageStatus.COMPLETED,
        artifact_ids=selected,
    )
    return AppliedReview(
        activate_next_stage(job.replace_stage(completed)),
        applied,
        selected,
    )


def _selected_artifact(unit: ReviewUnit) -> str:
    if unit.choice is ReviewChoice.EDITED:
        if unit.edited_artifact_id is None:
            raise ValueError("An edited review unit is missing its artifact.")
        return unit.edited_artifact_id
    if unit.choice is ReviewChoice.PROPOSED:
        if unit.proposed_artifact_id is None:
            raise ValueError("A proposed review unit is missing its artifact.")
        return unit.proposed_artifact_id
    return unit.original_artifact_id


def selected_artifact_ids(review: ReviewSession) -> tuple[str, ...]:
    """Return the immutable artifacts chosen by one complete review."""

    if not review.complete:
        raise ValueError("The review must be complete before selecting its artifacts.")
    return tuple(_selected_artifact(unit) for unit in review.units)
