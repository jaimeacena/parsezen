from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from parsezen.application.job_execution import JobExecutionController
from parsezen.application.job_queue import JobQueue
from parsezen.application.workspace_recovery import recover_workspace
from parsezen.domain.jobs import (
    DocumentFormat,
    DocumentJob,
    DocumentSource,
    JobConfiguration,
    JobStatus,
    OutputConfiguration,
)
from parsezen.domain.stages import StageKind
from parsezen.processing import ProcessResult
from parsezen.settings import AppSettings


@dataclass
class SnapshotLoaderStub:
    results: dict[str, ProcessResult] = field(default_factory=dict)

    def load(self, job_id: str) -> ProcessResult | None:
        return self.results.get(job_id)


def _job(path: Path, job_id: str, order: int = 0) -> DocumentJob:
    return DocumentJob.create(
        DocumentSource.inspect(path),
        JobConfiguration(
            output=OutputConfiguration(
                configured=True,
                format=DocumentFormat.MARKDOWN,
            )
        ),
        order=order,
        job_id=job_id,
    )


def test_workspace_recovery_classifies_completed_review_and_interrupted_jobs(
    tmp_path: Path,
) -> None:
    completed_source = tmp_path / "completed.txt"
    review_source = tmp_path / "review.txt"
    running_source = tmp_path / "running.txt"
    for path in (completed_source, review_source, running_source):
        path.write_text("Source", encoding="utf-8")
    completed_output = tmp_path / "completed.md"
    review_output = tmp_path / "review.md"
    completed_output.write_text("Completed", encoding="utf-8")
    review_output.write_text("Review", encoding="utf-8")

    queue = JobQueue(
        (
            _job(completed_source, "completed", 0),
            _job(review_source, "review", 1),
            _job(running_source, "running", 2),
        )
    )
    execution = JobExecutionController(queue)
    execution.complete("completed", completed_output)
    execution.block_completed_result_for_review(
        "review",
        StageKind.PREPARE,
        review_id="review-gate",
    )
    execution.start_next("running")
    snapshots = SnapshotLoaderStub({"review": ProcessResult(review_output)})

    recovered = recover_workspace(queue.jobs, snapshots, AppSettings())
    runtime = dict(recovered.runtime)

    assert runtime["completed"].result is not None
    assert runtime["review"].result is not None
    assert runtime["running"].result is None
    assert {job.id: job.status for job in recovered.jobs} == {
        "completed": JobStatus.COMPLETED,
        "review": JobStatus.WAITING_REVIEW,
        "running": JobStatus.RUNNING,
    }
    assert recovered.retained_artifact_job_ids == frozenset({"review"})
    assert recovered.interrupted_job_ids == frozenset({"running"})


def test_workspace_recovery_rejects_a_review_after_the_source_changes(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("Original", encoding="utf-8")
    queue = JobQueue((_job(source, "review"),))
    JobExecutionController(queue).block_completed_result_for_review(
        "review",
        StageKind.PREPARE,
        review_id="review-gate",
    )
    source.write_text("Changed source", encoding="utf-8")

    recovered = recover_workspace(queue.jobs, SnapshotLoaderStub(), AppSettings())

    assert dict(recovered.runtime)["review"].result is None
    assert recovered.reset_paused_job_ids == frozenset({"review"})
    assert recovered.source_changed_job_ids == frozenset({"review"})
    assert recovered.retained_artifact_job_ids == frozenset()


def test_workspace_recovery_ignores_sources_that_no_longer_exist(tmp_path: Path) -> None:
    source = tmp_path / "missing.txt"
    source.write_text("Original", encoding="utf-8")
    job = _job(source, "missing")
    source.unlink()

    recovered = recover_workspace((job,), SnapshotLoaderStub(), AppSettings())

    assert recovered.jobs == ()
    assert recovered.runtime == ()
