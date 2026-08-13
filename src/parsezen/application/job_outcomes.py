"""Resolve physical worker outcomes into durable per-job state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from parsezen.application.job_execution import JobExecutionController
from parsezen.application.job_queue import JobQueue
from parsezen.application.runtime_mapping import review_stage_for_result
from parsezen.domain.jobs import DocumentJob
from parsezen.domain.source_identity import SourceIdentity
from parsezen.domain.stages import StageKind
from parsezen.pipeline.contracts import ProcessResult


class ReviewSnapshotRepository(Protocol):
    """Persistence boundary for sensitive results awaiting human review."""

    def save(
        self,
        job_id: str,
        result: ProcessResult,
        *,
        source_identity: SourceIdentity | None = None,
    ) -> str: ...

    def discard(self, job_id: str) -> None: ...


class OutcomeWarning(StrEnum):
    REVIEW_RECOVERY_UNAVAILABLE = "review_recovery_unavailable"
    STALE_SNAPSHOT_NOT_REMOVED = "stale_snapshot_not_removed"


@dataclass(frozen=True, slots=True)
class ResolvedSuccess:
    job: DocumentJob
    review_stage: StageKind | None = None
    warning: OutcomeWarning | None = None

    @property
    def review_required(self) -> bool:
        return self.review_stage is not None


class JobOutcomeCoordinator:
    """Apply one worker terminal event without depending on Qt widgets."""

    def __init__(
        self,
        queue: JobQueue,
        execution: JobExecutionController,
        snapshots: ReviewSnapshotRepository,
    ) -> None:
        self._queue = queue
        self._execution = execution
        self._snapshots = snapshots

    def resolve_success(self, job_id: str, result: ProcessResult) -> ResolvedSuccess:
        job = self._require(job_id)
        review_required = (
            result.review_required or result.revision_draft is not None
        ) and not result.revision_approved
        if review_required:
            review_stage = review_stage_for_result(result, job.configuration)
            snapshot_saved = self._save_review_snapshot(job_id, result)
            try:
                resolved = self._execution.block_completed_result_for_review(
                    job_id,
                    review_stage,
                    review_id=f"{job_id}:{review_stage.value}:{job.configuration_revision}",
                )
            except (KeyError, ValueError):
                if snapshot_saved:
                    self._discard_snapshot(job_id)
                raise
            return ResolvedSuccess(
                resolved,
                review_stage,
                (None if snapshot_saved else OutcomeWarning.REVIEW_RECOVERY_UNAVAILABLE),
            )

        resolved = self._execution.complete(job_id, result.final_path)
        discarded = self._discard_snapshot(job_id)
        return ResolvedSuccess(
            resolved,
            warning=(None if discarded else OutcomeWarning.STALE_SNAPSHOT_NOT_REMOVED),
        )

    def resolve_failure(
        self,
        job_id: str,
        stage: StageKind,
        message: str,
        *,
        error_code: str = "processing_failed",
    ) -> DocumentJob:
        return self._execution.fail(
            job_id,
            stage,
            error_code=error_code,
            error_message=message,
        )

    def resolve_cancellation(self, job_id: str, *, paused: bool) -> DocumentJob:
        if paused:
            return self._execution.pause(job_id)
        return self._execution.cancel(job_id)

    def _save_review_snapshot(self, job_id: str, result: ProcessResult) -> bool:
        job = self._require(job_id)
        source = job.source
        source_identity = (
            SourceIdentity(source.size_bytes, source.modified_ns, source.content_sha256)
            if source.content_sha256 is not None
            else None
        )
        try:
            self._snapshots.save(
                job_id,
                result,
                source_identity=source_identity,
            )
        except (OSError, RuntimeError, ValueError):
            return False
        return True

    def _discard_snapshot(self, job_id: str) -> bool:
        try:
            self._snapshots.discard(job_id)
        except (OSError, RuntimeError, ValueError):
            return False
        return True

    def _require(self, job_id: str) -> DocumentJob:
        job = self._queue.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job
