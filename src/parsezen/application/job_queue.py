"""Authoritative ordered collection of independent document jobs."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from parsezen.domain.jobs import (
    DocumentJob,
    DocumentSource,
    JobConfiguration,
    JobStatus,
)


class JobQueue:
    """Own job identity, configuration and order outside the presentation layer."""

    def __init__(self, jobs: tuple[DocumentJob, ...] = ()) -> None:
        self._jobs: dict[str, DocumentJob] = {}
        self._source_ids: dict[str, str] = {}
        self.restore(jobs)

    @property
    def jobs(self) -> tuple[DocumentJob, ...]:
        return tuple(sorted(self._jobs.values(), key=lambda job: job.order))

    def __len__(self) -> int:
        return len(self._jobs)

    def get(self, job_id: str) -> DocumentJob | None:
        return self._jobs.get(job_id)

    def for_source(self, path: Path) -> DocumentJob | None:
        job_id = self._source_ids.get(_source_key(path))
        return self._jobs.get(job_id) if job_id is not None else None

    def add(
        self,
        source: DocumentSource,
        configuration: JobConfiguration,
        *,
        job_id: str | None = None,
    ) -> DocumentJob:
        key = _source_key(source.path)
        if key in self._source_ids:
            raise ValueError("A source document can appear only once in the queue.")
        job = DocumentJob.create(
            source,
            configuration,
            order=len(self._jobs),
            job_id=job_id,
        )
        self._jobs[job.id] = job
        self._source_ids[key] = job.id
        return job

    def replace(self, job: DocumentJob) -> DocumentJob:
        current = self._jobs.get(job.id)
        if current is None:
            raise KeyError(job.id)
        if _source_key(current.source.path) != _source_key(job.source.path):
            raise ValueError("A job cannot be reassigned to another source path.")
        if any(
            candidate.id != job.id and candidate.order == job.order
            for candidate in self._jobs.values()
        ):
            raise ValueError("Job order must be unique.")
        self._jobs[job.id] = job
        return job

    def configure(self, job_id: str, configuration: JobConfiguration) -> DocumentJob:
        job = self._require(job_id)
        return self.replace(job.with_configuration(configuration))

    def refresh_source(self, job_id: str, source: DocumentSource) -> DocumentJob:
        job = self._require(job_id)
        if _source_key(job.source.path) != _source_key(source.path):
            raise ValueError("A source snapshot cannot change its path.")
        if source == job.source:
            return job
        if job.status in {JobStatus.RUNNING, JobStatus.WAITING_REVIEW, JobStatus.COMPLETED}:
            raise ValueError("An active or completed job cannot change its source snapshot.")
        refreshed = DocumentJob.create(
            source,
            job.configuration,
            order=job.order,
            job_id=job.id,
        )
        return self.replace(
            replace(
                refreshed,
                configuration_revision=job.configuration_revision,
            )
        )

    def move(self, job_id: str, target_order: int) -> tuple[DocumentJob, ...]:
        ordered = list(self.jobs)
        source_order = next(
            (index for index, job in enumerate(ordered) if job.id == job_id),
            None,
        )
        if source_order is None:
            raise KeyError(job_id)
        target = max(0, min(target_order, len(ordered) - 1))
        moved = ordered.pop(source_order)
        ordered.insert(target, moved)
        self.restore(tuple(replace(job, order=order) for order, job in enumerate(ordered)))
        return self.jobs

    def remove(self, job_id: str) -> DocumentJob:
        removed = self._require(job_id)
        remaining = tuple(job for job in self.jobs if job.id != job_id)
        self.restore(remaining)
        return removed

    def retain_sources(self, paths: tuple[Path, ...]) -> tuple[DocumentJob, ...]:
        retained = {_source_key(path) for path in paths}
        if retained == set(self._source_ids):
            return self.jobs
        self.restore(tuple(job for job in self.jobs if _source_key(job.source.path) in retained))
        return self.jobs

    def order_sources(self, paths: tuple[Path, ...]) -> tuple[DocumentJob, ...]:
        if {_source_key(path) for path in paths} != set(self._source_ids):
            raise ValueError("Every queued source must appear exactly once in the visible order.")
        ordered = tuple(self.for_source(path) for path in paths)
        if any(job is None for job in ordered):
            raise ValueError("The visible order references an unknown source.")
        self.restore(
            tuple(replace(job, order=order) for order, job in enumerate(ordered) if job is not None)
        )
        return self.jobs

    def restore(self, jobs: tuple[DocumentJob, ...]) -> None:
        ordered = tuple(sorted(jobs, key=lambda job: job.order))
        identifiers = {job.id for job in ordered}
        source_keys = {_source_key(job.source.path) for job in ordered}
        if len(identifiers) != len(ordered):
            raise ValueError("Job identifiers must be unique.")
        if len(source_keys) != len(ordered):
            raise ValueError("Source documents must be unique.")
        normalized = tuple(replace(job, order=order) for order, job in enumerate(ordered))
        self._jobs = {job.id: job for job in normalized}
        self._source_ids = {_source_key(job.source.path): job.id for job in normalized}

    def clear(self) -> None:
        self._jobs.clear()
        self._source_ids.clear()

    def _require(self, job_id: str) -> DocumentJob:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job


def _source_key(path: Path) -> str:
    return str(path.resolve(strict=False)).casefold()
