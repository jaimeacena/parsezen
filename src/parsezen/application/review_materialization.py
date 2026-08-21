"""Materialize durable review candidates without depending on Qt."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from parsezen.application.artifact_repository import ArtifactRepository
from parsezen.application.quality_review_adapter import (
    apply_pdf_review,
    apply_translation_review,
    create_pdf_review,
    create_translation_review,
)
from parsezen.application.review_plan import review_workload_for_result
from parsezen.application.revision_materializer import create_revision_review
from parsezen.domain.jobs import DocumentJob, JobConfiguration
from parsezen.domain.reviews import ReviewChoice, ReviewKind, ReviewSession, ReviewStatus
from parsezen.pipeline.contracts import ProcessResult
from parsezen.revision import RevisionDraft, RevisionKind


class ReviewRepository(Protocol):
    def save_review(self, review: ReviewSession) -> None: ...

    def load_reviews(self, *, job_id: str | None = None) -> tuple[ReviewSession, ...]: ...


@dataclass(frozen=True, slots=True)
class QualityReviewMaterialization:
    saved_reviews: tuple[ReviewSession, ...]
    materialized: tuple[tuple[ReviewKind, ReviewSession | None], ...]
    reviewed_text: str
    phase_plan: tuple[tuple[ReviewKind, int], ...]

    @property
    def complete(self) -> bool:
        return all(
            review is None or review.status is ReviewStatus.APPLIED
            for _kind, review in self.materialized
        )


@dataclass(frozen=True, slots=True)
class RevisionReviewMaterialization:
    saved_reviews: tuple[ReviewSession, ...]
    materialized: tuple[tuple[RevisionKind, ReviewKind, ReviewSession | None], ...]
    phase_plan: tuple[tuple[ReviewKind, int], ...]


class ReviewMaterializationService:
    """Own candidate identity, persistence and reuse across review sessions."""

    def __init__(
        self,
        reviews: ReviewRepository,
        artifacts: ArtifactRepository,
    ) -> None:
        self._reviews = reviews
        self._artifacts = artifacts

    def materialize_quality(
        self,
        job: DocumentJob,
        result: ProcessResult,
        source_path: Path,
        text: str,
    ) -> QualityReviewMaterialization:
        saved = self._saved_reviews(job)
        input_record = self._artifacts.put_text(
            job_id=job.id,
            text=text,
            media_type="text/markdown; charset=utf-8",
        )
        candidate_factories: tuple[tuple[ReviewKind, Callable[[], ReviewSession | None]], ...] = (
            (
                ReviewKind.OCR,
                lambda: (
                    create_pdf_review(
                        result.pdf_quality_report,
                        source_path,
                        job_id=job.id,
                        configuration_revision=job.configuration_revision,
                        input_artifact_id=input_record.id,
                        artifacts=self._artifacts,
                    )
                    if result.pdf_quality_report is not None
                    else None
                ),
            ),
            (
                ReviewKind.TRANSLATION,
                lambda: (
                    create_translation_review(
                        result.translation_quality_for_review,
                        job_id=job.id,
                        configuration_revision=job.configuration_revision,
                        input_artifact_id=input_record.id,
                        artifacts=self._artifacts,
                    )
                    if result.translation_quality_for_review is not None
                    else None
                ),
            ),
        )
        materialized: list[tuple[ReviewKind, ReviewSession | None]] = []
        for kind, create_candidate in candidate_factories:
            candidate = create_candidate()
            saved_review = next((item for item in saved if item.kind is kind), None)
            if candidate is None:
                if saved_review is not None:
                    raise ValueError(
                        "La revisión guardada ya no coincide con un fragmento "
                        "verificable del documento. Se ha conservado sin sobrescribirla."
                    )
                materialized.append((kind, None))
                continue
            review = review_for_current_candidate(
                saved_review,
                candidate,
                artifacts=self._artifacts,
            )
            if (
                saved_review is None
                or not review_matches_candidate(saved_review, candidate)
                or review != saved_review
            ):
                self._reviews.save_review(review)
            materialized.append((kind, review))

        materialized_tuple = tuple(materialized)
        reviewed_text = _apply_saved_quality_reviews(
            text,
            materialized_tuple,
            self._artifacts,
        )
        return QualityReviewMaterialization(
            saved,
            materialized_tuple,
            reviewed_text,
            phase_plan_with_materialized_reviews(
                result,
                job.configuration,
                materialized_tuple,
            ),
        )

    def ensure_revision_candidates(
        self,
        draft: RevisionDraft | None,
        job: DocumentJob,
        saved: tuple[ReviewSession, ...],
    ) -> None:
        """Validate later review identities before reconciliation advances them."""

        if draft is None:
            return
        for revision_kind, review_kind in _REVISION_KINDS:
            candidate = create_revision_review(
                draft,
                revision_kind=revision_kind,
                job_id=job.id,
                configuration_revision=job.configuration_revision,
                artifacts=self._artifacts,
            )
            if candidate is None:
                continue
            saved_review = next((item for item in saved if item.kind is review_kind), None)
            review = review_for_current_candidate(
                saved_review,
                candidate,
                artifacts=self._artifacts,
            )
            if saved_review is None or not review_matches_candidate(saved_review, candidate):
                self._reviews.save_review(review)

    def materialize_revisions(
        self,
        job: DocumentJob,
        result: ProcessResult,
        draft: RevisionDraft,
        *,
        preceding_reviews: tuple[ReviewSession, ...] = (),
    ) -> RevisionReviewMaterialization:
        saved = self._saved_reviews(job)
        materialized: list[tuple[RevisionKind, ReviewKind, ReviewSession | None]] = []
        for revision_kind, review_kind in _REVISION_KINDS:
            saved_review = next((item for item in saved if item.kind is review_kind), None)
            candidate = create_revision_review(
                draft,
                revision_kind=revision_kind,
                job_id=job.id,
                configuration_revision=job.configuration_revision,
                artifacts=self._artifacts,
            )
            if candidate is None:
                materialized.append((revision_kind, review_kind, None))
                continue
            review = review_for_current_candidate(
                saved_review,
                candidate,
                artifacts=self._artifacts,
            )
            if (
                saved_review is None
                or not review_matches_candidate(saved_review, candidate)
                or review != saved_review
            ):
                self._reviews.save_review(review)
            materialized.append((revision_kind, review_kind, review))

        materialized_tuple = tuple(materialized)
        return RevisionReviewMaterialization(
            saved,
            materialized_tuple,
            phase_plan_with_materialized_reviews(
                result,
                job.configuration,
                tuple(
                    [(review.kind, review) for review in preceding_reviews]
                    + [
                        (review_kind, review)
                        for _revision_kind, review_kind, review in materialized_tuple
                    ]
                ),
            ),
        )

    def _saved_reviews(self, job: DocumentJob) -> tuple[ReviewSession, ...]:
        return tuple(
            review
            for review in self._reviews.load_reviews(job_id=job.id)
            if review.input_version == job.configuration_revision
            and review.status in {ReviewStatus.PENDING, ReviewStatus.APPLIED}
        )


_REVISION_KINDS = (
    (RevisionKind.CONTENT, ReviewKind.REFINEMENT),
    (RevisionKind.STRUCTURE, ReviewKind.STRUCTURE),
)


def review_matches_candidate(saved: ReviewSession, candidate: ReviewSession) -> bool:
    return (
        saved.kind is candidate.kind
        and saved.stage is candidate.stage
        and saved.input_version == candidate.input_version
        and tuple(unit.id for unit in saved.units) == tuple(unit.id for unit in candidate.units)
    )


def review_for_current_candidate(
    saved: ReviewSession | None,
    candidate: ReviewSession,
    *,
    artifacts: ArtifactRepository | None = None,
) -> ReviewSession:
    if saved is None:
        return candidate
    if not review_matches_candidate(saved, candidate):
        return replace(candidate, id=saved.id)
    current_units = {unit.id: unit for unit in candidate.units}
    merged_units = []
    invalidated_choice = False
    for saved_unit in saved.units:
        current = current_units[saved_unit.id]
        merged = replace(
            saved_unit,
            original_artifact_id=current.original_artifact_id,
            proposed_artifact_id=current.proposed_artifact_id,
            required=current.required,
            label=current.label,
            original_selectable=(
                False if candidate.kind is ReviewKind.TRANSLATION else current.original_selectable
            ),
            target=current.target,
            recommended_choice=current.recommended_choice,
            warning=current.warning,
            severity=current.severity,
            proposed_selectable=current.proposed_selectable,
        )
        edited_is_missing = bool(
            merged.choice is ReviewChoice.EDITED
            and merged.edited_artifact_id is not None
            and artifacts is not None
            and not _artifact_is_readable(
                artifacts,
                saved.job_id,
                merged.edited_artifact_id,
            )
        )
        if (
            candidate.kind is ReviewKind.TRANSLATION and merged.choice is ReviewChoice.ORIGINAL
        ) or edited_is_missing:
            merged = replace(merged, choice=None, edited_artifact_id=None)
            invalidated_choice = True
        merged_units.append(merged)
    return replace(
        saved,
        input_artifact_id=candidate.input_artifact_id,
        units=tuple(merged_units),
        status=ReviewStatus.PENDING if invalidated_choice else saved.status,
    )


def _artifact_is_readable(
    artifacts: ArtifactRepository,
    job_id: str,
    artifact_id: str,
) -> bool:
    try:
        artifacts.read(job_id, artifact_id)
    except (KeyError, OSError, UnicodeError, ValueError):
        return False
    return True


def phase_plan_with_materialized_reviews(
    result: ProcessResult,
    configuration: JobConfiguration,
    materialized: tuple[tuple[ReviewKind, ReviewSession | None], ...],
) -> tuple[tuple[ReviewKind, int], ...]:
    dialog_kinds = {
        ReviewKind.OCR,
        ReviewKind.TRANSLATION,
        ReviewKind.REFINEMENT,
        ReviewKind.CONVERSION_WARNING,
    }
    base = {
        item.kind: item.unit_count
        for item in review_workload_for_result(result, configuration)
        if item.kind in dialog_kinds
    }
    for kind, review in materialized:
        if kind in dialog_kinds:
            base[kind] = len(review.units) if review is not None else 0
    return tuple((kind, count) for kind, count in base.items() if count > 0)


def previous_review_kind(
    phase_plan: tuple[tuple[ReviewKind, int], ...],
    current: ReviewKind,
) -> ReviewKind | None:
    kinds = tuple(kind for kind, count in phase_plan if count > 0)
    try:
        index = kinds.index(current)
    except ValueError:
        return None
    return kinds[index - 1] if index > 0 else None


def _apply_saved_quality_reviews(
    text: str,
    materialized: tuple[tuple[ReviewKind, ReviewSession | None], ...],
    artifacts: ArtifactRepository,
) -> str:
    reviewed_text = text
    for kind, review in materialized:
        if review is None or review.status is not ReviewStatus.APPLIED:
            continue
        reviewed_text = (
            apply_pdf_review(reviewed_text, review, artifacts)
            if kind is ReviewKind.OCR
            else apply_translation_review(reviewed_text, review, artifacts)
        )
    return reviewed_text
