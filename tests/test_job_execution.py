from dataclasses import replace
from pathlib import Path

import pytest

from parsezen.application.job_execution import JobExecutionController
from parsezen.application.job_queue import JobQueue
from parsezen.application.scheduler import RunMode
from parsezen.domain.jobs import (
    DocumentFormat,
    DocumentSource,
    JobConfiguration,
    JobStatus,
    ProcessingPlan,
    ReviewRecommendation,
    ReviewSignal,
    TranslationConfiguration,
)
from parsezen.domain.stages import StageKind, StageStatus


def add_job(
    queue: JobQueue,
    identifier: str,
    *,
    translation: bool = False,
    refinement: bool = False,
) -> str:
    job = queue.add(
        DocumentSource(
            Path(f"{identifier}.pdf"),
            DocumentFormat.PDF,
            100,
            1,
        ),
        JobConfiguration(
            translation=TranslationConfiguration(enabled=translation),
            plan=(ProcessingPlan.LOCAL_AI_REVIEWED if refinement else ProcessingPlan.STANDARD),
        ),
        job_id=identifier,
    )
    return job.id


def test_execution_advances_chronologically_and_preserves_attempts() -> None:
    queue = JobQueue()
    job_id = add_job(queue, "one", translation=True)
    execution = JobExecutionController(queue)

    started = execution.start_next(job_id)
    progressed = execution.report_progress(
        job_id,
        StageKind.PREPARE,
        3,
        10,
        "converting",
    )
    translated = execution.advance(job_id, StageKind.TRANSLATE)

    assert started.stage(StageKind.PREPARE).status is StageStatus.RUNNING
    assert progressed.stage(StageKind.PREPARE).progress_ratio == 0.3
    assert translated.stage(StageKind.PREPARE).status is StageStatus.COMPLETED
    assert translated.stage(StageKind.PREPARE).attempt == 1
    assert translated.stage(StageKind.PREPARE).started_at is not None
    assert translated.stage(StageKind.PREPARE).finished_at is not None
    assert translated.stage(StageKind.TRANSLATE).status is StageStatus.RUNNING


def test_targeted_review_temporarily_enables_only_refinement_on_completed_result() -> None:
    queue = JobQueue()
    job_id = add_job(queue, "late")
    execution = JobExecutionController(queue)
    completed = execution.complete(job_id, Path("late.md"))
    queue.replace(
        replace(
            completed,
            review_recommendation=ReviewRecommendation(
                ((ReviewSignal.CONVERSION_DAMAGE, 1),),
                (0,),
            ),
        )
    )

    reviewing = execution.begin_targeted_review(job_id)
    restored = execution.abort_targeted_review(job_id)

    assert reviewing.stage(StageKind.REFINE).status is StageStatus.READY
    assert reviewing.stage(StageKind.REFINE).participates
    assert not restored.stage(StageKind.REFINE).participates
    assert restored.status is JobStatus.COMPLETED


def test_review_blocks_only_its_job_and_another_can_start() -> None:
    queue = JobQueue()
    first_id = add_job(queue, "one", refinement=True)
    second_id = add_job(queue, "two")
    execution = JobExecutionController(queue)

    execution.start_next(first_id)
    blocked = execution.block_for_review(
        first_id,
        StageKind.REFINE,
        review_id="one-refine-review",
    )
    second = execution.start_next(second_id)

    assert blocked.stage(StageKind.REFINE).status is StageStatus.BLOCKED_FOR_REVIEW
    assert second.stage(StageKind.PREPARE).status is StageStatus.RUNNING


def test_active_run_consumes_failed_job_and_continues_with_next_document() -> None:
    queue = JobQueue()
    first_id = add_job(queue, "one")
    second_id = add_job(queue, "two")
    execution = JobExecutionController(queue)

    plan = execution.begin_run()
    first = execution.start_next_available()
    assert first is not None
    execution.fail(
        first.id,
        StageKind.PREPARE,
        error_code="conversion_failed",
        error_message="No se pudo convertir.",
    )
    second = execution.start_next_available()

    assert plan.mode is RunMode.NEW
    assert plan.job_ids == (first_id, second_id)
    assert first.id == first_id
    assert second is not None
    assert second.id == second_id
    failed = queue.get(first_id)
    assert failed is not None
    assert failed.status is JobStatus.FAILED


def test_execution_rejects_a_prepared_plan_after_the_queue_changes() -> None:
    queue = JobQueue()
    add_job(queue, "one")
    execution = JobExecutionController(queue)
    prepared_plan = execution.plan_run()
    add_job(queue, "two")

    with pytest.raises(ValueError, match="prepared queue run is stale"):
        execution.begin_run(prepared_plan)


def test_run_modes_do_not_mix_new_paused_and_failed_work() -> None:
    queue = JobQueue()
    paused_id = add_job(queue, "paused")
    queued_id = add_job(queue, "queued")
    failed_id = add_job(queue, "failed")
    execution = JobExecutionController(queue)
    execution.start_next(paused_id)
    execution.pause(paused_id)
    execution.start_next(failed_id)
    execution.fail(
        failed_id,
        StageKind.PREPARE,
        error_code="failed",
        error_message="Error",
    )

    resume = execution.plan_run()

    assert resume.mode is RunMode.RESUME
    assert resume.job_ids == (paused_id, queued_id)

    execution.start_next(paused_id)
    execution.cancel(paused_id)
    execution.start_next(queued_id)
    execution.cancel(queued_id)
    retry = execution.plan_run()

    assert retry.mode is RunMode.RETRY
    assert retry.job_ids == (paused_id, queued_id, failed_id)


def test_execution_rejects_parallel_automatic_work() -> None:
    queue = JobQueue()
    first_id = add_job(queue, "one")
    second_id = add_job(queue, "two")
    execution = JobExecutionController(queue)
    execution.start_next(first_id)

    with pytest.raises(ValueError, match="Only one"):
        execution.start_next(second_id)


def test_pause_resume_failure_and_cancellation_use_valid_transitions() -> None:
    queue = JobQueue()
    paused_id = add_job(queue, "paused")
    failed_id = add_job(queue, "failed")
    cancelled_id = add_job(queue, "cancelled")
    execution = JobExecutionController(queue)

    execution.start_next(paused_id)
    paused = execution.pause(paused_id)
    resumed = execution.start_next(paused_id)
    execution.fail(
        paused_id,
        StageKind.PREPARE,
        error_code="conversion_failed",
        error_message="No se pudo convertir.",
    )
    failed = queue.get(paused_id)
    retried = execution.start_next(paused_id)
    execution.pause(paused_id)

    execution.start_next(failed_id)
    cancelled = execution.cancel(failed_id)

    assert paused.stage(StageKind.PREPARE).status is StageStatus.PAUSED
    assert resumed.stage(StageKind.PREPARE).status is StageStatus.RUNNING
    assert failed is not None
    assert failed.stage(StageKind.PREPARE).status is StageStatus.FAILED
    assert failed.stage(StageKind.PREPARE).error_message == "No se pudo convertir."
    assert retried.stage(StageKind.PREPARE).attempt == 3
    assert cancelled.stage(StageKind.PREPARE).status is StageStatus.CANCELLED
    assert cancelled.status is JobStatus.CANCELLED
    assert queue.get(cancelled_id) is not None


def test_completed_review_publishes_every_phase_and_result() -> None:
    queue = JobQueue()
    job_id = add_job(queue, "one", translation=True, refinement=True)
    execution = JobExecutionController(queue)
    execution.start_next(job_id)
    execution.block_for_review(
        job_id,
        StageKind.TRANSLATE,
        review_id="translation-review",
    )

    completed = execution.complete(job_id, Path("one.epub"))

    assert all(
        stage.status is StageStatus.COMPLETED for stage in completed.stages if stage.participates
    )
    assert completed.result_path == Path("one.epub")


def test_combined_worker_result_reopens_exact_review_phase() -> None:
    queue = JobQueue()
    job_id = add_job(queue, "one", translation=True, refinement=True)
    execution = JobExecutionController(queue)
    execution.start_next(job_id)
    execution.advance(job_id, StageKind.PUBLISH)
    before_review = queue.get(job_id)
    assert before_review is not None
    translation_attempts = before_review.stage(StageKind.TRANSLATE).attempt

    blocked = execution.block_completed_result_for_review(
        job_id,
        StageKind.TRANSLATE,
        review_id="translation-review",
    )

    assert blocked.stage(StageKind.PREPARE).status is StageStatus.COMPLETED
    assert blocked.stage(StageKind.TRANSLATE).status is StageStatus.BLOCKED_FOR_REVIEW
    assert blocked.stage(StageKind.TRANSLATE).attempt == translation_attempts
    assert blocked.stage(StageKind.REFINE).status is StageStatus.INVALIDATED
    assert blocked.stage(StageKind.PUBLISH).status is StageStatus.INVALIDATED


def test_interrupted_and_invalid_review_jobs_become_safely_paused() -> None:
    queue = JobQueue()
    running_id = add_job(queue, "running")
    review_id = add_job(queue, "review", refinement=True)
    execution = JobExecutionController(queue)

    execution.start_next(running_id)
    interrupted = execution.recover_interrupted(running_id)
    execution.start_next(review_id)
    execution.block_for_review(
        review_id,
        StageKind.REFINE,
        review_id="review-id",
    )
    reset = execution.reset_paused(review_id)

    assert interrupted.stage(StageKind.PREPARE).status is StageStatus.PAUSED
    assert reset.stage(StageKind.PREPARE).status is StageStatus.PAUSED
    assert reset.result_path is None
