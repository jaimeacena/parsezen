import pytest

from parsezen.domain.reviews import (
    ReviewChoice,
    ReviewKind,
    ReviewSession,
    ReviewSeverity,
    ReviewStatus,
    ReviewUnit,
)
from parsezen.domain.stages import StageKind


@pytest.mark.parametrize(
    "unit",
    [
        ReviewUnit("missing-proposal", "original"),
        ReviewUnit(
            "quarantined-proposal",
            "original",
            "proposal",
            proposed_selectable=False,
        ),
    ],
)
def test_no_text_requires_a_selectable_proposed_artifact(unit: ReviewUnit) -> None:
    review = ReviewSession.create(
        job_id="job",
        stage=StageKind.PREPARE,
        kind=ReviewKind.OCR,
        input_artifact_id="source",
        input_version=1,
        units=(unit,),
    )

    with pytest.raises(ValueError, match="propuesta seleccionable"):
        review.decide(unit.id, ReviewChoice.NO_TEXT)


def test_review_preserves_manual_decisions_until_apply() -> None:
    review = ReviewSession.create(
        job_id="job",
        stage=StageKind.TRANSLATE,
        kind=ReviewKind.TRANSLATION,
        input_artifact_id="source-v1",
        input_version=1,
        units=(
            ReviewUnit("one", "original-one", "proposal-one"),
            ReviewUnit("two", "original-two", "proposal-two"),
        ),
    )
    review = review.decide("one", ReviewChoice.PROPOSED)
    review = review.decide(
        "two",
        ReviewChoice.EDITED,
        edited_artifact_id="edited-two",
    )

    assert review.complete
    applied = review.apply()
    assert applied.status is ReviewStatus.APPLIED
    assert applied.units[1].edited_artifact_id == "edited-two"


def test_review_orders_independent_units_by_severity_stably() -> None:
    review = ReviewSession.create(
        job_id="job",
        stage=StageKind.REFINE,
        kind=ReviewKind.REFINEMENT,
        input_artifact_id="source-v1",
        input_version=1,
        units=(
            ReviewUnit(
                "low",
                "original-low",
                "proposal-low",
                severity=ReviewSeverity.LOW,
            ),
            ReviewUnit(
                "high-first",
                "original-high-first",
                "proposal-high-first",
                severity=ReviewSeverity.HIGH,
            ),
            ReviewUnit(
                "high-second",
                "original-high-second",
                "proposal-high-second",
                severity=ReviewSeverity.HIGH,
            ),
        ),
    )

    assert [unit.id for unit in review.units] == [
        "high-first",
        "high-second",
        "low",
    ]


def test_review_exposes_only_unresolved_priority_work() -> None:
    review = ReviewSession.create(
        job_id="job",
        stage=StageKind.REFINE,
        kind=ReviewKind.REFINEMENT,
        input_artifact_id="source",
        input_version=1,
        units=(
            ReviewUnit(
                "high",
                "original-high",
                "proposal-high",
                severity=ReviewSeverity.HIGH,
            ),
            ReviewUnit(
                "low",
                "original-low",
                "proposal-low",
                severity=ReviewSeverity.LOW,
            ),
        ),
    ).decide("high", ReviewChoice.ORIGINAL)

    assert review.resolved_count == 1
    assert review.remaining_count == 1
    assert review.priority_remaining_count == 0
    assert review.first_unresolved_index() == 1
    assert review.next_unresolved_index(1) is None
