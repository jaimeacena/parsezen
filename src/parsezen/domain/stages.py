"""Execution states and transition rules for one document stage."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum


class StageKind(StrEnum):
    """Stable, user-visible phases in chronological order."""

    PREPARE = "prepare"
    TRANSLATE = "translate"
    REFINE = "refine"
    STRUCTURE = "structure"
    PUBLISH = "publish"


STAGE_ORDER = (
    StageKind.PREPARE,
    StageKind.TRANSLATE,
    StageKind.REFINE,
    StageKind.STRUCTURE,
    StageKind.PUBLISH,
)


class StageAvailability(StrEnum):
    """Whether a phase can and should participate in this job."""

    ENABLED = "enabled"
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"


class StageStatus(StrEnum):
    """Execution status for an enabled phase."""

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    BLOCKED_FOR_REVIEW = "blocked_for_review"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    INVALIDATED = "invalidated"


class InvalidStageTransitionError(ValueError):
    """Raised when a caller attempts a chronologically impossible transition."""


_ALLOWED_TRANSITIONS: dict[StageStatus, frozenset[StageStatus]] = {
    StageStatus.PENDING: frozenset({StageStatus.READY, StageStatus.CANCELLED}),
    StageStatus.READY: frozenset({StageStatus.RUNNING, StageStatus.PAUSED, StageStatus.CANCELLED}),
    StageStatus.RUNNING: frozenset(
        {
            StageStatus.BLOCKED_FOR_REVIEW,
            StageStatus.COMPLETED,
            StageStatus.FAILED,
            StageStatus.PAUSED,
            StageStatus.CANCELLED,
        }
    ),
    StageStatus.BLOCKED_FOR_REVIEW: frozenset({StageStatus.COMPLETED, StageStatus.CANCELLED}),
    StageStatus.COMPLETED: frozenset({StageStatus.INVALIDATED}),
    StageStatus.FAILED: frozenset({StageStatus.READY, StageStatus.CANCELLED}),
    StageStatus.PAUSED: frozenset({StageStatus.READY, StageStatus.CANCELLED}),
    StageStatus.CANCELLED: frozenset({StageStatus.READY}),
    StageStatus.INVALIDATED: frozenset({StageStatus.READY, StageStatus.CANCELLED}),
}


@dataclass(frozen=True, slots=True)
class StageState:
    """Immutable state projection for one phase of one job."""

    kind: StageKind
    availability: StageAvailability = StageAvailability.ENABLED
    status: StageStatus = StageStatus.PENDING
    progress_current: int = 0
    progress_total: int = 0
    progress_message: str | None = None
    attempt: int = 0
    review_id: str | None = None
    artifact_ids: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.progress_current < 0 or self.progress_total < 0:
            raise ValueError("Stage progress cannot be negative.")
        if self.progress_total and self.progress_current > self.progress_total:
            raise ValueError("Stage progress cannot exceed its total.")
        if self.availability is not StageAvailability.ENABLED and self.status not in {
            StageStatus.PENDING,
            StageStatus.CANCELLED,
        }:
            raise ValueError("A disabled or unavailable stage cannot execute.")
        if self.status is StageStatus.BLOCKED_FOR_REVIEW and not self.review_id:
            raise ValueError("A review-blocked stage must reference its review.")
        if self.status is not StageStatus.BLOCKED_FOR_REVIEW and self.review_id is not None:
            raise ValueError("Only a review-blocked stage may reference an active review.")
        if self.attempt < 0:
            raise ValueError("Stage attempt cannot be negative.")

    @property
    def participates(self) -> bool:
        return self.availability is StageAvailability.ENABLED

    @property
    def progress_ratio(self) -> float | None:
        if self.progress_total <= 0:
            return None
        return min(1.0, self.progress_current / self.progress_total)

    def transition(
        self,
        status: StageStatus,
        *,
        now: datetime | None = None,
        review_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        artifact_ids: tuple[str, ...] | None = None,
    ) -> StageState:
        """Return the next valid state or reject an impossible chronology."""

        if status is self.status:
            return self
        if not self.participates and status is not StageStatus.CANCELLED:
            raise InvalidStageTransitionError(f"{self.kind.value} is not enabled.")
        if status not in _ALLOWED_TRANSITIONS[self.status]:
            raise InvalidStageTransitionError(
                f"{self.kind.value} cannot transition from {self.status.value} to {status.value}."
            )

        timestamp = now if now is not None else datetime.now(UTC)
        started_at = self.started_at
        finished_at = self.finished_at
        attempt = self.attempt
        if status is StageStatus.RUNNING:
            started_at = timestamp
            finished_at = None
            attempt += 1
        elif status in {
            StageStatus.COMPLETED,
            StageStatus.FAILED,
            StageStatus.CANCELLED,
        }:
            finished_at = timestamp
        elif status in {StageStatus.READY, StageStatus.INVALIDATED}:
            finished_at = None

        return replace(
            self,
            status=status,
            attempt=attempt,
            review_id=review_id if status is StageStatus.BLOCKED_FOR_REVIEW else None,
            artifact_ids=self.artifact_ids if artifact_ids is None else artifact_ids,
            error_code=error_code if status is StageStatus.FAILED else None,
            error_message=error_message if status is StageStatus.FAILED else None,
            started_at=started_at,
            finished_at=finished_at,
            progress_current=(
                self.progress_total
                if status is StageStatus.COMPLETED and self.progress_total
                else self.progress_current
            ),
        )

    def with_progress(self, current: int, total: int, message: str | None = None) -> StageState:
        if self.status is not StageStatus.RUNNING:
            raise InvalidStageTransitionError("Only a running stage can report progress.")
        return replace(
            self,
            progress_current=current,
            progress_total=total,
            progress_message=message,
        )

    def require_cached_result_review(self, review_id: str) -> StageState:
        """Gate an already-computed result without recording another attempt."""

        if self.status not in {
            StageStatus.PENDING,
            StageStatus.READY,
            StageStatus.COMPLETED,
            StageStatus.INVALIDATED,
        }:
            raise InvalidStageTransitionError(
                "Only an available cached result can request a late review."
            )
        if not review_id:
            raise ValueError("A review gate must reference its review.")
        return replace(
            self,
            status=StageStatus.BLOCKED_FOR_REVIEW,
            review_id=review_id,
            error_code=None,
            error_message=None,
        )

    def invalidate_cached_result(self) -> StageState:
        """Hide a prepared or completed downstream result behind an earlier gate."""

        if self.status is StageStatus.COMPLETED:
            return self.transition(StageStatus.INVALIDATED)
        if self.status is not StageStatus.READY:
            raise InvalidStageTransitionError(
                "Only a prepared or completed cached result can be invalidated."
            )
        return replace(
            self,
            status=StageStatus.INVALIDATED,
            finished_at=None,
        )
