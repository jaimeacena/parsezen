"""Rebuild a resumable Qt-free queue projection from durable jobs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from parsezen.application.job_runtime import JobRuntime
from parsezen.application.runtime_mapping import request_and_settings_from_job
from parsezen.domain.jobs import DocumentJob, DocumentSource, JobStatus
from parsezen.domain.source_identity import sha256_file
from parsezen.processing import ProcessResult
from parsezen.settings import AppSettings

SOURCE_CHANGED_MESSAGE = (
    "El original cambió desde que se añadió a la cola. Para evitar mezclar versiones, "
    "quítalo y vuelve a añadirlo antes de procesarlo."
)


class ResultSnapshotLoader(Protocol):
    def load(self, job_id: str) -> ProcessResult | None: ...


@dataclass(frozen=True, slots=True)
class RecoveredWorkspace:
    jobs: tuple[DocumentJob, ...]
    runtime: tuple[tuple[str, JobRuntime], ...]
    retained_artifact_job_ids: frozenset[str]
    reset_paused_job_ids: frozenset[str]
    interrupted_job_ids: frozenset[str]
    source_changed_job_ids: frozenset[str]


def recover_workspace(
    saved_jobs: tuple[DocumentJob, ...],
    snapshots: ResultSnapshotLoader,
    settings: AppSettings,
) -> RecoveredWorkspace:
    runtime: list[tuple[str, JobRuntime]] = []
    restored_jobs: list[DocumentJob] = []
    retained_artifacts: set[str] = set()
    reset_paused: set[str] = set()
    interrupted: set[str] = set()
    source_changed: set[str] = set()

    for job in saved_jobs:
        if not job.source.path.is_file():
            continue
        restored_jobs.append(job)
        request = None
        configuration_unavailable = False
        if job.is_configured:
            try:
                request, _runtime_settings = request_and_settings_from_job(
                    job,
                    timeout_seconds=settings.timeout_seconds,
                    checkpoint_retention_days=settings.checkpoint_retention_days,
                )
            except (OSError, ValueError):
                configuration_unavailable = True

        source_unchanged = source_is_unchanged(job.source)
        result = None
        if configuration_unavailable:
            reset_paused.add(job.id)
        elif job.status is JobStatus.COMPLETED and job.result_path and job.result_path.is_file():
            result = ProcessResult(final_path=job.result_path)
        elif job.status is JobStatus.WAITING_REVIEW and source_unchanged:
            try:
                result = snapshots.load(job.id)
            except (OSError, RuntimeError, ValueError):
                result = None
            if result is not None and result.final_path.is_file():
                retained_artifacts.add(job.id)
            else:
                reset_paused.add(job.id)
        elif job.status is JobStatus.WAITING_REVIEW:
            reset_paused.add(job.id)
            source_changed.add(job.id)
        elif job.status is not JobStatus.QUEUED:
            if job.status is JobStatus.RUNNING:
                interrupted.add(job.id)

        runtime.append(
            (
                job.id,
                JobRuntime(
                    pdf_page_range=request.pdf_page_range if request is not None else None,
                    result=result,
                ),
            )
        )

    return RecoveredWorkspace(
        tuple(restored_jobs),
        tuple(runtime),
        frozenset(retained_artifacts),
        frozenset(reset_paused),
        frozenset(interrupted),
        frozenset(source_changed),
    )


def source_is_unchanged(source: DocumentSource) -> bool:
    try:
        statistics = source.path.stat()
    except OSError:
        return False
    if statistics.st_size != source.size_bytes or statistics.st_mtime_ns != source.modified_ns:
        return False
    if source.content_sha256 is None:
        return True
    try:
        return sha256_file(source.path) == source.content_sha256
    except OSError:
        return False
