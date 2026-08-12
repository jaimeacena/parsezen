from pathlib import Path

import pytest

from parsezen.application.job_execution import JobExecutionController
from parsezen.application.job_queue import JobQueue
from parsezen.application.run_preparation import prepare_queue_run
from parsezen.application.scheduler import QueueRunPlan, RunMode
from parsezen.cancellation import CancellationToken
from parsezen.domain.jobs import (
    DocumentFormat,
    DocumentSource,
    JobConfiguration,
    OutputConfiguration,
)
from parsezen.domain.stages import StageKind
from parsezen.errors import ProcessingCancelledError


def test_preparation_maps_and_validates_only_the_planned_jobs(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("First", encoding="utf-8")
    second.write_text("Second", encoding="utf-8")
    queue = JobQueue()
    first_job = queue.add(
        DocumentSource.inspect(first),
        JobConfiguration(
            output=OutputConfiguration(
                format=DocumentFormat.MARKDOWN,
                directory=tmp_path,
            )
        ),
        job_id="first",
    )
    second_job = queue.add(
        DocumentSource.inspect(second),
        JobConfiguration(),
        job_id="second",
    )
    execution = JobExecutionController(queue)
    execution.start_next(first_job.id)
    execution.fail(
        first_job.id,
        StageKind.PREPARE,
        error_code="failed",
        error_message="Error",
    )

    prepared = prepare_queue_run(queue.jobs, timeout_seconds=45)

    assert prepared.plan.mode is RunMode.NEW
    assert prepared.plan.job_ids == (second_job.id,)
    assert tuple(item.job_id for item in prepared.items) == (second_job.id,)
    assert prepared.items[0].request.source_path == second
    assert prepared.items[0].settings.timeout_seconds == 45
    assert prepared.issues == ()
    assert prepared.item(first_job.id) is None
    assert prepared.item(second_job.id) == prepared.items[0]


def test_explicit_retry_prepares_only_the_failed_document(tmp_path: Path) -> None:
    failed_source = tmp_path / "failed.txt"
    queued_source = tmp_path / "queued.txt"
    failed_source.write_text("Failed", encoding="utf-8")
    queued_source.write_text("Queued", encoding="utf-8")
    queue = JobQueue()
    configuration = JobConfiguration(
        output=OutputConfiguration(
            format=DocumentFormat.MARKDOWN,
            directory=tmp_path,
        )
    )
    failed_job = queue.add(
        DocumentSource.inspect(failed_source),
        configuration,
        job_id="failed",
    )
    queue.add(
        DocumentSource.inspect(queued_source),
        configuration,
        job_id="queued",
    )
    execution = JobExecutionController(queue)
    execution.start_next(failed_job.id)
    execution.fail(
        failed_job.id,
        StageKind.PREPARE,
        error_code="source",
        error_message="Error",
    )
    plan = QueueRunPlan(RunMode.RETRY, (failed_job.id,))

    prepared = prepare_queue_run(
        queue.jobs,
        timeout_seconds=45,
        plan=plan,
    )
    execution.begin_run(plan, allow_subset=True)

    assert prepared.plan == plan
    assert tuple(item.job_id for item in prepared.items) == (failed_job.id,)
    assert execution.start_next_available().id == failed_job.id


def test_preparation_honours_cancellation_before_reading_a_document(tmp_path: Path) -> None:
    source = tmp_path / "cancelled.txt"
    source.write_text("Cancelled", encoding="utf-8")
    queue = JobQueue()
    queue.add(DocumentSource.inspect(source), JobConfiguration(), job_id="cancelled")
    cancellation = CancellationToken()
    cancellation.cancel()

    with pytest.raises(ProcessingCancelledError):
        prepare_queue_run(
            queue.jobs,
            timeout_seconds=45,
            cancellation=cancellation,
        )
