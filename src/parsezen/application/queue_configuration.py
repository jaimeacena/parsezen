"""Apply shared queue configuration without depending on Qt widgets."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from parsezen.application.job_queue import JobQueue
from parsezen.application.runtime_mapping import request_and_settings_from_job
from parsezen.domain.jobs import (
    AIProfileConfiguration,
    DocumentFormat,
    DocumentJob,
    JobConfiguration,
    JobStatus,
)
from parsezen.pdf_conversion import PdfPageRange

_EDITABLE_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.FAILED, JobStatus.CANCELLED})


@dataclass(frozen=True, slots=True)
class QueueConfigurationUpdate:
    job: DocumentJob
    pdf_page_range: PdfPageRange | None


class QueueConfigurationService:
    """Own propagation rules for inherited and compatible document settings."""

    def __init__(self, queue: JobQueue) -> None:
        self._queue = queue

    def propagate_output_directory(
        self,
        previous: Path | None,
        current: Path | None,
    ) -> tuple[DocumentJob, ...]:
        if current == previous:
            return ()
        updated_jobs: list[DocumentJob] = []
        for job in self._queue.jobs:
            if job.status not in _EDITABLE_STATUSES:
                continue
            if job.configuration.output.directory == current:
                continue
            try:
                updated = job.with_configuration(
                    replace(
                        job.configuration,
                        output=replace(job.configuration.output, directory=current),
                    )
                )
            except ValueError:
                continue
            updated_jobs.append(self._queue.replace(updated))
        return tuple(updated_jobs)

    def propagate_ai_profile(
        self,
        previous: AIProfileConfiguration,
        current: AIProfileConfiguration,
    ) -> tuple[DocumentJob, ...]:
        if current == previous:
            return ()
        updated_jobs: list[DocumentJob] = []
        for job in self._queue.jobs:
            if job.status not in _EDITABLE_STATUSES:
                continue
            if job.configuration.ai == current:
                continue
            try:
                updated = job.with_configuration(
                    replace(
                        job.configuration,
                        ai=current,
                    )
                )
            except ValueError:
                continue
            updated_jobs.append(self._queue.replace(updated))
        return tuple(updated_jobs)

    def apply_to_compatible_jobs(
        self,
        source_job: DocumentJob,
        configuration: JobConfiguration,
        *,
        timeout_seconds: float,
        checkpoint_retention_days: int,
    ) -> tuple[QueueConfigurationUpdate, ...]:
        updates: list[QueueConfigurationUpdate] = []
        for candidate in self._queue.jobs:
            if (
                candidate.id == source_job.id
                or candidate.source.format is not source_job.source.format
                or candidate.status not in _EDITABLE_STATUSES
            ):
                continue
            candidate_configuration = replace(
                configuration,
                page_range=candidate.configuration.page_range,
                force_pdf_ocr=candidate.configuration.force_pdf_ocr,
                output=replace(
                    configuration.output,
                    title=(
                        candidate.source.path.stem
                        if configuration.output.format is DocumentFormat.EPUB
                        else None
                    ),
                ),
            )
            try:
                updated = candidate.with_configuration(candidate_configuration)
                request, _runtime = request_and_settings_from_job(
                    updated,
                    timeout_seconds=timeout_seconds,
                    checkpoint_retention_days=checkpoint_retention_days,
                )
            except ValueError:
                continue
            self._queue.replace(updated)
            updates.append(QueueConfigurationUpdate(updated, request.pdf_page_range))
        return tuple(updates)
