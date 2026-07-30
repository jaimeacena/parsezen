from dataclasses import replace
from pathlib import Path

import pytest

from parsezen.application.planner import activate_next_stage, invalidate_after
from parsezen.application.scheduler import (
    prepare_runnable_jobs,
    select_next_stage,
    start_selected_stage,
)
from parsezen.domain.jobs import (
    DocumentFormat,
    DocumentJob,
    DocumentSource,
    JobConfiguration,
    JobStatus,
    OutputConfiguration,
    RefinementConfiguration,
    StructureConfiguration,
    TranslationConfiguration,
    effective_ai_profile,
)
from parsezen.domain.stages import StageAvailability, StageKind, StageStatus


def source(path: str = "book.pdf") -> DocumentSource:
    return DocumentSource(Path(path), DocumentFormat.PDF, 100, 1)


def complete(job: DocumentJob, kind: StageKind) -> DocumentJob:
    stage = job.stage(kind)
    if stage.status is StageStatus.PENDING:
        job = activate_next_stage(job)
        stage = job.stage(kind)
    return job.replace_stage(
        stage.transition(StageStatus.RUNNING).transition(StageStatus.COMPLETED)
    )


def test_job_builds_independent_stage_availability() -> None:
    job = DocumentJob.create(
        source(),
        JobConfiguration(
            output=OutputConfiguration(format=DocumentFormat.EPUB),
            translation=TranslationConfiguration(enabled=True),
            refinement=RefinementConfiguration(enabled=False),
            structure=StructureConfiguration(enabled=True),
        ),
        order=0,
        job_id="one",
    )

    assert job.stage(StageKind.TRANSLATE).availability is StageAvailability.ENABLED
    assert job.stage(StageKind.REFINE).availability is StageAvailability.DISABLED
    assert job.stage(StageKind.STRUCTURE).availability is StageAvailability.ENABLED


def test_structure_is_unavailable_for_non_epub_output() -> None:
    job = DocumentJob.create(
        source(),
        JobConfiguration(
            output=OutputConfiguration(format=DocumentFormat.MARKDOWN),
            structure=StructureConfiguration(enabled=True),
        ),
        order=0,
    )

    assert job.stage(StageKind.STRUCTURE).availability is StageAvailability.UNAVAILABLE


def test_job_status_is_derived_from_its_stages() -> None:
    job = activate_next_stage(DocumentJob.create(source(), JobConfiguration(), order=0))
    assert job.status is JobStatus.QUEUED

    running = job.replace_stage(job.stage(StageKind.PREPARE).transition(StageStatus.RUNNING))
    assert running.status is JobStatus.RUNNING

    blocked = running.replace_stage(
        running.stage(StageKind.PREPARE).transition(
            StageStatus.BLOCKED_FOR_REVIEW,
            review_id="review",
        )
    )
    assert blocked.status is JobStatus.WAITING_REVIEW


def test_scheduler_skips_review_blocked_job_without_starting_in_parallel() -> None:
    first = activate_next_stage(
        DocumentJob.create(source("one.pdf"), JobConfiguration(), order=0, job_id="one")
    )
    first = first.replace_stage(
        first.stage(StageKind.PREPARE)
        .transition(StageStatus.RUNNING)
        .transition(StageStatus.BLOCKED_FOR_REVIEW, review_id="review")
    )
    second = DocumentJob.create(
        source("two.pdf"),
        JobConfiguration(),
        order=1,
        job_id="two",
    )
    prepared = prepare_runnable_jobs((first, second))
    decision = select_next_stage(prepared)

    assert decision is not None
    assert decision.job_id == "two"
    started = start_selected_stage(prepared, decision)
    assert started[1].stage(StageKind.PREPARE).status is StageStatus.RUNNING
    assert select_next_stage(started) is None


def test_completed_upstream_edit_invalidates_only_downstream_phases() -> None:
    job = DocumentJob.create(
        source(),
        JobConfiguration(
            output=OutputConfiguration(format=DocumentFormat.EPUB),
            translation=TranslationConfiguration(enabled=True),
            refinement=RefinementConfiguration(enabled=True),
            structure=StructureConfiguration(enabled=True),
        ),
        order=0,
    )
    for kind in (
        StageKind.PREPARE,
        StageKind.TRANSLATE,
        StageKind.REFINE,
        StageKind.STRUCTURE,
        StageKind.PUBLISH,
    ):
        job = complete(job, kind)

    invalidated = invalidate_after(job, StageKind.TRANSLATE)

    assert invalidated.stage(StageKind.PREPARE).status is StageStatus.COMPLETED
    assert invalidated.stage(StageKind.TRANSLATE).status is StageStatus.COMPLETED
    assert invalidated.stage(StageKind.REFINE).status is StageStatus.READY
    assert invalidated.stage(StageKind.STRUCTURE).status is StageStatus.INVALIDATED
    assert invalidated.stage(StageKind.PUBLISH).status is StageStatus.INVALIDATED


def test_running_job_cannot_be_reconfigured() -> None:
    job = activate_next_stage(DocumentJob.create(source(), JobConfiguration(), order=0))
    job = job.replace_stage(job.stage(StageKind.PREPARE).transition(StageStatus.RUNNING))

    with pytest.raises(ValueError):
        job.with_configuration(replace(job.configuration, force_pdf_ocr=True))


def test_legacy_phase_models_remain_available_through_the_shared_ai_profile() -> None:
    configuration = JobConfiguration(
        translation=TranslationConfiguration(
            enabled=True,
            model="legacy-model",
            context_window=4096,
        )
    )

    profile = effective_ai_profile(configuration)

    assert profile.model == "legacy-model"
    assert profile.context_window == 4096
