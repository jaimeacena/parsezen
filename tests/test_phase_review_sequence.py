from pathlib import Path

import pytest

from parsezen.application.job_execution import JobExecutionController
from parsezen.application.job_queue import JobQueue
from parsezen.application.phase_review_sequence import PhaseReviewSequenceCoordinator
from parsezen.domain.jobs import (
    DocumentFormat,
    DocumentSource,
    JobConfiguration,
    RefinementConfiguration,
    TranslationConfiguration,
)
from parsezen.domain.reviews import (
    ReviewChoice,
    ReviewKind,
    ReviewSession,
    ReviewStatus,
    ReviewUnit,
)
from parsezen.domain.stages import StageKind, StageStatus
from parsezen.infrastructure.state_store import StateStore
from parsezen.processing import ProcessResult
from parsezen.revision import RevisionChange, RevisionDraft, RevisionKind
from parsezen.translation_quality import (
    TranslationIssueKind,
    TranslationQualityIssue,
    TranslationQualityReport,
)


class ReviewRepository:
    def __init__(self) -> None:
        self._reviews: dict[str, ReviewSession] = {}

    def save_review(self, review: ReviewSession) -> None:
        self._reviews[review.id] = review

    def load_reviews(self, *, job_id: str | None = None) -> tuple[ReviewSession, ...]:
        return tuple(
            review for review in self._reviews.values() if job_id is None or review.job_id == job_id
        )


def review_result() -> ProcessResult:
    return ProcessResult(
        Path("book.md"),
        translation_quality_report=TranslationQualityReport(
            source_language="en",
            target_language="es",
            detected_language="es",
            checked_segments=1,
            source_characters=4,
            translated_characters=5,
            total_issues=1,
            issues=(
                TranslationQualityIssue(
                    1,
                    TranslationIssueKind.SOURCE_TEXT,
                    "Sin traducir",
                    "Text",
                    "Text",
                ),
            ),
        ),
        revision_draft=RevisionDraft(
            "Text\n",
            "Texto\n",
            (
                RevisionChange(
                    "content",
                    RevisionKind.CONTENT,
                    0,
                    1,
                    "Text\n",
                    "Texto\n",
                    "Corrección",
                ),
            ),
            frozenset({RevisionKind.CONTENT}),
        ),
        review_required=True,
    )


def review(
    job_id: str,
    version: int,
    *,
    stage: StageKind,
    kind: ReviewKind,
) -> ReviewSession:
    return ReviewSession.create(
        job_id=job_id,
        stage=stage,
        kind=kind,
        input_artifact_id="input",
        input_version=version,
        units=(ReviewUnit(kind.value, "original", "proposed"),),
    ).decide(kind.value, ReviewChoice.PROPOSED)


def prepared_sequence() -> tuple[
    JobQueue,
    JobExecutionController,
    ReviewRepository,
    PhaseReviewSequenceCoordinator,
    str,
    ProcessResult,
]:
    queue = JobQueue()
    job = queue.add(
        DocumentSource(Path("book.md"), DocumentFormat.MARKDOWN, 100, 1),
        JobConfiguration(
            translation=TranslationConfiguration(enabled=True, target_language="es"),
            refinement=RefinementConfiguration(enabled=True),
        ),
        job_id="job",
    )
    execution = JobExecutionController(queue)
    execution.start_next(job.id)
    execution.advance(job.id, StageKind.PUBLISH)
    result = review_result()
    execution.block_completed_result_for_review(
        job.id,
        StageKind.TRANSLATE,
        review_id="initial-gate",
    )
    repository = ReviewRepository()
    sequence = PhaseReviewSequenceCoordinator(queue, execution, repository)
    return queue, execution, repository, sequence, job.id, result


def test_each_applied_review_advances_only_to_the_next_review_gate() -> None:
    queue, _execution, _repository, sequence, job_id, result = prepared_sequence()
    job = queue.get(job_id)
    assert job is not None
    translation_attempt = job.stage(StageKind.TRANSLATE).attempt
    translation = review(
        job_id,
        job.configuration_revision,
        stage=StageKind.TRANSLATE,
        kind=ReviewKind.TRANSLATION,
    )

    first = sequence.apply(job_id, result, translation)

    assert first.applied_review is not None
    assert first.applied_review.status is ReviewStatus.APPLIED
    assert first.next_step is not None
    assert first.next_step.kind is ReviewKind.REFINEMENT
    assert first.job.stage(StageKind.TRANSLATE).status is StageStatus.COMPLETED
    assert first.job.stage(StageKind.TRANSLATE).attempt == translation_attempt
    assert first.job.stage(StageKind.REFINE).status is StageStatus.BLOCKED_FOR_REVIEW

    refinement = review(
        job_id,
        first.job.configuration_revision,
        stage=StageKind.REFINE,
        kind=ReviewKind.REFINEMENT,
    )
    second = sequence.apply(job_id, result, refinement)

    assert second.next_step is None
    assert second.job.stage(StageKind.REFINE).status is StageStatus.COMPLETED
    assert second.job.stage(StageKind.PUBLISH).status is StageStatus.READY


def test_reconcile_repairs_a_crash_after_review_persistence() -> None:
    queue, execution, repository, sequence, job_id, result = prepared_sequence()
    job = queue.get(job_id)
    assert job is not None
    pending = review(
        job_id,
        job.configuration_revision,
        stage=StageKind.TRANSLATE,
        kind=ReviewKind.TRANSLATION,
    )
    sequence.prepare(job_id, result, pending)
    repository.save_review(pending.apply())

    recovered = sequence.reconcile(job_id, result)

    assert recovered.job.stage(StageKind.TRANSLATE).status is StageStatus.COMPLETED
    assert recovered.job.stage(StageKind.REFINE).status is StageStatus.BLOCKED_FOR_REVIEW
    assert recovered.next_step is not None
    assert recovered.next_step.kind is ReviewKind.REFINEMENT
    assert execution.plan_run().job_ids == ()


def test_pending_review_survives_reconcile_without_advancing() -> None:
    queue, _execution, _repository, sequence, job_id, result = prepared_sequence()
    job = queue.get(job_id)
    assert job is not None
    pending = review(
        job_id,
        job.configuration_revision,
        stage=StageKind.TRANSLATE,
        kind=ReviewKind.TRANSLATION,
    )
    sequence.prepare(job_id, result, pending)

    recovered = sequence.reconcile(job_id, result)

    assert recovered.job.stage(StageKind.TRANSLATE).status is StageStatus.BLOCKED_FOR_REVIEW
    assert recovered.job.stage(StageKind.TRANSLATE).review_id == pending.id
    assert recovered.next_step is not None
    assert recovered.next_step.kind is ReviewKind.TRANSLATION


def test_out_of_order_or_stale_review_is_rejected() -> None:
    queue, _execution, _repository, sequence, job_id, result = prepared_sequence()
    job = queue.get(job_id)
    assert job is not None
    refinement = review(
        job_id,
        job.configuration_revision,
        stage=StageKind.REFINE,
        kind=ReviewKind.REFINEMENT,
    )

    with pytest.raises(ValueError, match="next pending"):
        sequence.prepare(job_id, result, refinement)


def test_finalization_failure_can_reopen_the_last_applied_gate() -> None:
    queue, _execution, _repository, sequence, job_id, result = prepared_sequence()
    job = queue.get(job_id)
    assert job is not None
    translation = review(
        job_id,
        job.configuration_revision,
        stage=StageKind.TRANSLATE,
        kind=ReviewKind.TRANSLATION,
    )
    first = sequence.apply(job_id, result, translation)
    refinement = review(
        job_id,
        first.job.configuration_revision,
        stage=StageKind.REFINE,
        kind=ReviewKind.REFINEMENT,
    )
    sequence.apply(job_id, result, refinement)

    reopened = sequence.reopen_last_review(job_id, result)

    assert reopened.stage(StageKind.REFINE).status is StageStatus.BLOCKED_FOR_REVIEW
    assert reopened.stage(StageKind.REFINE).review_id == refinement.id
    assert reopened.stage(StageKind.PUBLISH).status is StageStatus.INVALIDATED


def test_pending_phase_review_resumes_from_sqlite_after_restart(tmp_path: Path) -> None:
    queue, _execution, _repository, sequence, job_id, result = prepared_sequence()
    job = queue.get(job_id)
    assert job is not None
    pending = review(
        job_id,
        job.configuration_revision,
        stage=StageKind.TRANSLATE,
        kind=ReviewKind.TRANSLATION,
    )
    sequence.prepare(job_id, result, pending)

    store = StateStore(tmp_path / "workspace.sqlite3")
    store.replace_jobs(queue.jobs)
    store.save_review(pending)
    restored_queue = JobQueue(store.load_jobs())
    restored = PhaseReviewSequenceCoordinator(
        restored_queue,
        JobExecutionController(restored_queue),
        store,
    )

    progress = restored.reconcile(job_id, result)

    assert progress.next_step is not None
    assert progress.next_step.kind is ReviewKind.TRANSLATION
    assert progress.job.stage(StageKind.TRANSLATE).status is StageStatus.BLOCKED_FOR_REVIEW
    assert progress.job.stage(StageKind.TRANSLATE).review_id == pending.id
