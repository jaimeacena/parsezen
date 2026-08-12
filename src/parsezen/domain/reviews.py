"""Persistent, phase-specific review contracts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from itertools import chain
from uuid import uuid4

from parsezen.domain.stages import StageKind


class ReviewKind(StrEnum):
    OCR = "ocr"
    TRANSLATION = "translation"
    REFINEMENT = "refinement"
    STRUCTURE = "structure"
    CONVERSION_WARNING = "conversion_warning"


class ReviewStatus(StrEnum):
    DRAFT = "draft"
    PENDING = "pending"
    APPLIED = "applied"
    DISMISSED = "dismissed"


class ReviewChoice(StrEnum):
    ORIGINAL = "original"
    PROPOSED = "proposed"
    EDITED = "edited"
    NO_TEXT = "no_text"


class ReviewSeverity(StrEnum):
    """User-facing urgency used to order independent review units."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        return {
            ReviewSeverity.CRITICAL: 0,
            ReviewSeverity.HIGH: 1,
            ReviewSeverity.MEDIUM: 2,
            ReviewSeverity.LOW: 3,
        }[self]


@dataclass(frozen=True, slots=True)
class ReviewUnit:
    id: str
    original_artifact_id: str
    proposed_artifact_id: str | None = None
    edited_artifact_id: str | None = None
    choice: ReviewChoice | None = None
    required: bool = True
    label: str | None = None
    original_selectable: bool = True
    target: str | None = None
    recommended_choice: ReviewChoice | None = None
    warning: str | None = None
    severity: ReviewSeverity = ReviewSeverity.MEDIUM
    proposed_selectable: bool = True

    @property
    def resolved(self) -> bool:
        return self.choice is not None or not self.required


@dataclass(frozen=True, slots=True)
class ReviewSession:
    id: str
    job_id: str
    stage: StageKind
    kind: ReviewKind
    input_artifact_id: str
    input_version: int
    units: tuple[ReviewUnit, ...]
    status: ReviewStatus = ReviewStatus.PENDING
    created_at: datetime = datetime.min.replace(tzinfo=UTC)
    updated_at: datetime = datetime.min.replace(tzinfo=UTC)

    @classmethod
    def create(
        cls,
        *,
        job_id: str,
        stage: StageKind,
        kind: ReviewKind,
        input_artifact_id: str,
        input_version: int,
        units: tuple[ReviewUnit, ...],
        now: datetime | None = None,
    ) -> ReviewSession:
        timestamp = now if now is not None else datetime.now(UTC)
        return cls(
            id=uuid4().hex,
            job_id=job_id,
            stage=stage,
            kind=kind,
            input_artifact_id=input_artifact_id,
            input_version=input_version,
            units=tuple(
                unit
                for _index, unit in sorted(
                    enumerate(units),
                    key=lambda item: (item[1].severity.rank, item[0]),
                )
            ),
            created_at=timestamp,
            updated_at=timestamp,
        )

    @property
    def complete(self) -> bool:
        return all(unit.resolved for unit in self.units)

    @property
    def resolved_count(self) -> int:
        return sum(unit.resolved for unit in self.units)

    @property
    def remaining_count(self) -> int:
        return len(self.units) - self.resolved_count

    @property
    def priority_remaining_count(self) -> int:
        return sum(
            not unit.resolved and unit.severity in {ReviewSeverity.CRITICAL, ReviewSeverity.HIGH}
            for unit in self.units
        )

    def first_unresolved_index(self) -> int | None:
        return next(
            (index for index, unit in enumerate(self.units) if not unit.resolved),
            None,
        )

    def next_unresolved_index(self, after_index: int) -> int | None:
        """Find the next pending decision, wrapping over already resolved work."""

        if not 0 <= after_index < len(self.units):
            raise IndexError(after_index)
        following = range(after_index + 1, len(self.units))
        preceding = range(0, after_index)
        return next(
            (index for index in chain(following, preceding) if not self.units[index].resolved),
            None,
        )

    def decide(
        self,
        unit_id: str,
        choice: ReviewChoice,
        *,
        edited_artifact_id: str | None = None,
        now: datetime | None = None,
    ) -> ReviewSession:
        if self.status is not ReviewStatus.PENDING:
            raise ValueError("Only a pending review can be edited.")
        units = list(self.units)
        for index, unit in enumerate(units):
            if unit.id != unit_id:
                continue
            if choice is ReviewChoice.EDITED and not edited_artifact_id:
                raise ValueError("An edited choice must reference the edited artifact.")
            if choice is ReviewChoice.PROPOSED and not unit.proposed_selectable:
                raise ValueError("Esta propuesta está en cuarentena y no se puede seleccionar.")
            if choice is ReviewChoice.EDITED and not unit.proposed_selectable:
                raise ValueError("Esta propuesta está en cuarentena y no se puede editar.")
            if choice is ReviewChoice.NO_TEXT and self.kind is not ReviewKind.OCR:
                raise ValueError("La opción de texto vacío solo está disponible para OCR.")
            if choice is ReviewChoice.NO_TEXT and (
                unit.proposed_artifact_id is None or not unit.proposed_selectable
            ):
                raise ValueError("La unidad OCR no tiene una propuesta seleccionable.")
            units[index] = replace(
                unit,
                choice=choice,
                edited_artifact_id=edited_artifact_id,
            )
            timestamp = now if now is not None else datetime.now(UTC)
            return replace(self, units=tuple(units), updated_at=timestamp)
        raise KeyError(unit_id)

    def apply(self, *, now: datetime | None = None) -> ReviewSession:
        if self.status is not ReviewStatus.PENDING or not self.complete:
            raise ValueError("The review must be complete before it can be applied.")
        return replace(
            self,
            status=ReviewStatus.APPLIED,
            updated_at=now if now is not None else datetime.now(UTC),
        )

    def reopen(self, *, now: datetime | None = None) -> ReviewSession:
        """Reopen an applied decision set without discarding its choices."""

        if self.status is not ReviewStatus.APPLIED:
            raise ValueError("Only an applied review can be reopened.")
        return replace(
            self,
            status=ReviewStatus.PENDING,
            updated_at=now if now is not None else datetime.now(UTC),
        )

    def dismiss(self, *, now: datetime | None = None) -> ReviewSession:
        """Inactivate a downstream review after an earlier phase changes."""

        if self.status is ReviewStatus.DISMISSED:
            return self
        return replace(
            self,
            status=ReviewStatus.DISMISSED,
            updated_at=now if now is not None else datetime.now(UTC),
        )
