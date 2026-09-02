"""Restore, preserve and prune durable workspace state without Qt."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from parsezen.application.job_execution import JobExecutionController
from parsezen.application.job_queue import JobQueue
from parsezen.application.job_runtime import JobRuntime
from parsezen.application.queue_session import QueueSession
from parsezen.application.workspace_recovery import (
    SOURCE_CHANGED_MESSAGE,
    ResultSnapshotLoader,
    recover_workspace,
)
from parsezen.domain.jobs import DocumentJob
from parsezen.settings import AppSettings

LOGGER = logging.getLogger(__name__)


class WorkspaceStateRepository(Protocol):
    @property
    def path(self) -> Path: ...

    def load_jobs(self) -> tuple[DocumentJob, ...]: ...

    def backup_to(self, destination: Path) -> Path: ...

    def reset_queue(self) -> None: ...

    def delete_review_material(self, job_id: str) -> None: ...


class WorkspaceArtifactRepository(Protocol):
    @property
    def root(self) -> Path: ...

    def remove_job(self, job_id: str) -> None: ...

    def prune_orphaned_jobs(self, retained_job_ids: frozenset[str]) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class WorkspaceRestoreOutcome:
    retained_artifact_job_ids: frozenset[str] | None
    notice: str | None = None
    persistence_unavailable: bool = False


class WorkspaceRecoveryController:
    """Own recovery mutations while presentation only displays their outcome."""

    def __init__(
        self,
        state: WorkspaceStateRepository,
        artifacts: WorkspaceArtifactRepository,
        snapshots: ResultSnapshotLoader,
        queue: JobQueue,
        execution: JobExecutionController,
        session: QueueSession,
        settings: AppSettings,
    ) -> None:
        self._state = state
        self._artifacts = artifacts
        self._snapshots = snapshots
        self._queue = queue
        self._execution = execution
        self._session = session
        self._settings = settings

    def restore(self) -> WorkspaceRestoreOutcome:
        try:
            saved_jobs = self._state.load_jobs()
        except (OSError, RuntimeError, ValueError):
            notice = self._preserve_unreadable_state()
            return WorkspaceRestoreOutcome(
                None,
                notice,
                persistence_unavailable=notice is None,
            )
        recovered = recover_workspace(saved_jobs, self._snapshots, self._settings)
        if recovered.jobs:
            self._queue.restore(recovered.jobs)
            for job_id in recovered.reset_paused_job_ids:
                paused = self._execution.reset_paused(job_id)
                if job_id in recovered.source_changed_job_ids:
                    self._queue.replace(
                        replace(
                            paused,
                            warnings=(*paused.warnings, SOURCE_CHANGED_MESSAGE),
                        )
                    )
            for job_id in recovered.interrupted_job_ids:
                self._execution.recover_interrupted(job_id)
            self._session.replace_runtime(dict(recovered.runtime))
        return WorkspaceRestoreOutcome(recovered.retained_artifact_job_ids)

    def invalidate_changed_source_review(
        self,
        runtime: JobRuntime,
        job: DocumentJob,
    ) -> None:
        runtime.result = None
        paused = self._execution.reset_paused(job.id)
        if SOURCE_CHANGED_MESSAGE not in paused.warnings:
            self._queue.replace(
                replace(paused, warnings=(*paused.warnings, SOURCE_CHANGED_MESSAGE))
            )
        try:
            self._state.delete_review_material(job.id)
            self._artifacts.remove_job(job.id)
        except (OSError, RuntimeError, ValueError):
            LOGGER.warning("changed_source_review_cleanup_failed")

    def prune_orphaned_review_artifacts(self, retained_job_ids: frozenset[str]) -> None:
        try:
            removed = self._artifacts.prune_orphaned_jobs(retained_job_ids)
        except (OSError, RuntimeError, ValueError):
            LOGGER.warning("orphaned_review_artifact_cleanup_failed")
            return
        if removed:
            LOGGER.info("orphaned_review_artifacts_removed count=%d", len(removed))

    def _preserve_unreadable_state(self) -> str | None:
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        source = self._state.path
        backup = source.with_name(f"{source.stem}.unreadable-{timestamp}{source.suffix}")
        artifact_source = self._artifacts.root
        artifact_backup = artifact_source.with_name(
            f"{artifact_source.name}.unreadable-{timestamp}"
        )
        try:
            self._state.backup_to(backup)
            if artifact_source.exists():
                if artifact_backup.exists():
                    raise FileExistsError("The artifact backup already exists.")
                artifact_source.replace(artifact_backup)
            self._state.reset_queue()
        except (FileExistsError, OSError, RuntimeError, ValueError):
            LOGGER.warning("unreadable_state_preservation_failed")
            return None
        preserved = backup.name
        if artifact_backup.exists():
            preserved = f"{backup.name} y {artifact_backup.name}"
        LOGGER.warning(
            "unreadable_state_preserved backup_created=true artifacts_preserved=%s",
            artifact_backup.exists(),
        )
        return (
            "No se pudo recuperar la cola anterior. Se conservó una copia local "
            f"segura ({preserved})."
        )


__all__ = ["WorkspaceRecoveryController", "WorkspaceRestoreOutcome"]
