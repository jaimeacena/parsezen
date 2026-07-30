"""Compile and advance one document's dependency-ordered execution plan."""

from __future__ import annotations

from parsezen.domain.jobs import DocumentJob
from parsezen.domain.stages import STAGE_ORDER, StageKind, StageStatus


def activate_next_stage(job: DocumentJob) -> DocumentJob:
    """Mark the first dependency-satisfied pending phase as ready."""

    if any(
        stage.status in {StageStatus.RUNNING, StageStatus.BLOCKED_FOR_REVIEW, StageStatus.READY}
        for stage in job.stages
        if stage.participates
    ):
        return job

    for index, stage in enumerate(job.stages):
        if not stage.participates:
            continue
        if stage.status not in {StageStatus.PENDING, StageStatus.INVALIDATED}:
            continue
        dependencies = (dependency for dependency in job.stages[:index] if dependency.participates)
        if all(dependency.status is StageStatus.COMPLETED for dependency in dependencies):
            return job.replace_stage(stage.transition(StageStatus.READY))
        return job
    return job


def invalidate_after(job: DocumentJob, changed_stage: StageKind) -> DocumentJob:
    """Invalidate only completed phases downstream from an edited artifact."""

    changed_index = STAGE_ORDER.index(changed_stage)
    updated = job
    for stage in updated.stages[changed_index + 1 :]:
        if stage.status in {
            StageStatus.RUNNING,
            StageStatus.BLOCKED_FOR_REVIEW,
        }:
            raise ValueError("A downstream active phase cannot be invalidated.")
        if stage.status is StageStatus.COMPLETED:
            updated = updated.replace_stage(stage.transition(StageStatus.INVALIDATED))
    return activate_next_stage(updated)


def retry_failed_stage(job: DocumentJob, stage_kind: StageKind) -> DocumentJob:
    stage = job.stage(stage_kind)
    if stage.status is not StageStatus.FAILED:
        raise ValueError("Only a failed phase can be retried.")
    return activate_next_stage(job.replace_stage(stage.transition(StageStatus.READY)))
