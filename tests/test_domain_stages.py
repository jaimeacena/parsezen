from datetime import UTC, datetime

import pytest

from parsezen.domain.stages import (
    InvalidStageTransitionError,
    StageAvailability,
    StageKind,
    StageState,
    StageStatus,
)


def test_stage_enforces_chronological_transitions() -> None:
    now = datetime(2026, 7, 26, tzinfo=UTC)
    stage = StageState(StageKind.PREPARE)
    ready = stage.transition(StageStatus.READY, now=now)
    running = ready.transition(StageStatus.RUNNING, now=now)
    completed = running.with_progress(3, 3).transition(StageStatus.COMPLETED, now=now)

    assert running.attempt == 1
    assert running.started_at == now
    assert completed.status is StageStatus.COMPLETED
    assert completed.finished_at == now


def test_stage_rejects_impossible_completion_and_progress() -> None:
    stage = StageState(StageKind.TRANSLATE)

    with pytest.raises(InvalidStageTransitionError):
        stage.transition(StageStatus.COMPLETED)
    with pytest.raises(InvalidStageTransitionError):
        stage.with_progress(1, 2)


def test_review_block_requires_review_identifier() -> None:
    running = (
        StageState(StageKind.REFINE).transition(StageStatus.READY).transition(StageStatus.RUNNING)
    )

    with pytest.raises(ValueError):
        running.transition(StageStatus.BLOCKED_FOR_REVIEW)
    blocked = running.transition(StageStatus.BLOCKED_FOR_REVIEW, review_id="review-1")
    assert blocked.review_id == "review-1"


def test_unavailable_stage_cannot_run() -> None:
    stage = StageState(
        StageKind.STRUCTURE,
        availability=StageAvailability.UNAVAILABLE,
    )

    with pytest.raises(InvalidStageTransitionError):
        stage.transition(StageStatus.READY)


def test_cached_review_gate_does_not_count_as_another_automatic_attempt() -> None:
    completed = (
        StageState(StageKind.TRANSLATE)
        .transition(StageStatus.READY)
        .transition(StageStatus.RUNNING)
        .transition(StageStatus.COMPLETED)
    )

    blocked = completed.require_cached_result_review("translation-review")

    assert blocked.status is StageStatus.BLOCKED_FOR_REVIEW
    assert blocked.review_id == "translation-review"
    assert blocked.attempt == completed.attempt


def test_prepared_downstream_cache_can_be_hidden_behind_an_earlier_review() -> None:
    ready = StageState(StageKind.PUBLISH).transition(StageStatus.READY)

    invalidated = ready.invalidate_cached_result()

    assert invalidated.status is StageStatus.INVALIDATED
    with pytest.raises(InvalidStageTransitionError):
        StageState(StageKind.PUBLISH).invalidate_cached_result()
