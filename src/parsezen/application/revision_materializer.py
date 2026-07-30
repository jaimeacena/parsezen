"""Bridge conservative Markdown revision drafts to phase-specific reviews."""

from __future__ import annotations

from parsezen.domain.reviews import (
    ReviewChoice,
    ReviewKind,
    ReviewSession,
    ReviewSeverity,
    ReviewUnit,
)
from parsezen.domain.stages import StageKind
from parsezen.infrastructure.artifact_store import ArtifactStore
from parsezen.revision import (
    RevisionChange,
    RevisionDecision,
    RevisionDraft,
    RevisionKind,
    split_markdown_blocks,
)


def create_revision_review(
    draft: RevisionDraft,
    *,
    revision_kind: RevisionKind,
    job_id: str,
    configuration_revision: int,
    artifacts: ArtifactStore,
) -> ReviewSession | None:
    changes = tuple(change for change in draft.changes if change.kind is revision_kind)
    if not changes:
        return None
    source = artifacts.put_text(
        job_id=job_id,
        text=draft.original_markdown,
        media_type="text/markdown; charset=utf-8",
    )
    units: list[ReviewUnit] = []
    for change in changes:
        original = artifacts.put_text(
            job_id=job_id,
            text=change.original_markdown,
            media_type="text/markdown; charset=utf-8",
        )
        proposed = artifacts.put_text(
            job_id=job_id,
            text=change.proposed_markdown,
            media_type="text/markdown; charset=utf-8",
        )
        units.append(
            ReviewUnit(
                change.identifier,
                original.id,
                proposed.id,
                label=change.summary,
                recommended_choice=(
                    ReviewChoice.ORIGINAL
                    if change.recommended_decision is RevisionDecision.REJECTED
                    else ReviewChoice.PROPOSED
                ),
                warning=change.risk_reason,
                severity=_revision_severity(change),
            )
        )
    return ReviewSession.create(
        job_id=job_id,
        stage=(StageKind.REFINE if revision_kind is RevisionKind.CONTENT else StageKind.STRUCTURE),
        kind=(
            ReviewKind.REFINEMENT if revision_kind is RevisionKind.CONTENT else ReviewKind.STRUCTURE
        ),
        input_artifact_id=source.id,
        input_version=configuration_revision,
        units=tuple(units),
    )


def _revision_severity(change: RevisionChange) -> ReviewSeverity:
    original = change.original_markdown.strip()
    proposed = change.proposed_markdown.strip()
    if not original or not proposed:
        return ReviewSeverity.CRITICAL
    if change.risk.value == "high":
        return ReviewSeverity.HIGH
    if change.kind.value == "structure":
        return ReviewSeverity.LOW
    return ReviewSeverity.MEDIUM


def render_revision_reviews(
    draft: RevisionDraft,
    reviews: tuple[ReviewSession, ...],
    artifacts: ArtifactStore,
) -> str:
    """Render original/proposed/manual choices across every draft change."""

    units = {unit.id: (review.job_id, unit) for review in reviews for unit in review.units}
    blocks = split_markdown_blocks(draft.original_markdown)
    output: list[str] = []
    cursor = 0
    for change in draft.changes:
        output.extend(block.markdown for block in blocks[cursor : change.original_start])
        selected = units.get(change.identifier)
        if selected is None:
            output.append(change.original_markdown)
        else:
            job_id, unit = selected
            output.append(_unit_text(job_id, unit, artifacts))
        cursor = change.original_end
    output.extend(block.markdown for block in blocks[cursor:])
    return "".join(output)


def _unit_text(job_id: str, unit: ReviewUnit, artifacts: ArtifactStore) -> str:
    if unit.choice is ReviewChoice.EDITED:
        if unit.edited_artifact_id is None:
            raise ValueError("An edited revision is missing its artifact.")
        return artifacts.read_text(job_id, unit.edited_artifact_id)
    if unit.choice is ReviewChoice.PROPOSED:
        if unit.proposed_artifact_id is None:
            raise ValueError("A proposed revision is missing its artifact.")
        return artifacts.read_text(job_id, unit.proposed_artifact_id)
    if unit.choice is ReviewChoice.ORIGINAL:
        return artifacts.read_text(job_id, unit.original_artifact_id)
    raise ValueError("Every revision change must be decided before rendering.")
