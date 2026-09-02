"""Qt-free facade for materializing, advancing and publishing reviews."""

from __future__ import annotations

from parsezen.application.artifact_repository import ArtifactRepository
from parsezen.application.phase_review_sequence import (
    PhaseReviewProgress,
    PhaseReviewSequenceCoordinator,
)
from parsezen.application.quality_review_adapter import ensure_quality_reviews_applied
from parsezen.application.review_materialization import (
    QualityReviewMaterialization,
    ReviewMaterializationService,
    RevisionReviewMaterialization,
)
from parsezen.application.review_publication import (
    PublishedReview,
    ReviewPublicationCoordinator,
)
from parsezen.application.revision_materializer import render_revision_reviews
from parsezen.domain.books import BookDocument
from parsezen.domain.jobs import DocumentJob
from parsezen.domain.reviews import ReviewKind, ReviewSession
from parsezen.pipeline.contracts import ProcessResult
from parsezen.revision import RevisionDraft, build_revision_draft


class ReviewFlowCoordinator:
    """Present one application boundary while dialogs remain in presentation."""

    def __init__(
        self,
        materialization: ReviewMaterializationService,
        phases: PhaseReviewSequenceCoordinator,
        publication: ReviewPublicationCoordinator,
        artifacts: ArtifactRepository,
    ) -> None:
        self._materialization = materialization
        self._phases = phases
        self._publication = publication
        self._artifacts = artifacts

    def materialize_quality(
        self,
        job: DocumentJob,
        result: ProcessResult,
        text: str,
    ) -> QualityReviewMaterialization:
        return self._materialization.materialize_quality(
            job,
            result,
            job.source.path,
            text,
        )

    def materialize_revisions(
        self,
        job: DocumentJob,
        result: ProcessResult,
        draft: RevisionDraft,
        *,
        preceding_reviews: tuple[ReviewSession, ...] = (),
    ) -> RevisionReviewMaterialization:
        return self._materialization.materialize_revisions(
            job,
            result,
            draft,
            preceding_reviews=preceding_reviews,
        )

    def ensure_revision_candidates(
        self,
        draft: RevisionDraft | None,
        job: DocumentJob,
        saved: tuple[ReviewSession, ...],
    ) -> None:
        self._materialization.ensure_revision_candidates(draft, job, saved)

    def reconcile(self, job_id: str, result: ProcessResult) -> PhaseReviewProgress:
        return self._phases.reconcile(job_id, result)

    def prepare(
        self,
        job_id: str,
        result: ProcessResult,
        review: ReviewSession,
    ) -> DocumentJob:
        return self._phases.prepare(job_id, result, review)

    def apply(
        self,
        job_id: str,
        result: ProcessResult,
        review: ReviewSession,
    ) -> PhaseReviewProgress:
        return self._phases.apply(job_id, result, review)

    def reopen_review(
        self,
        job_id: str,
        result: ProcessResult,
        *,
        kind: ReviewKind,
    ) -> DocumentJob:
        return self._phases.reopen_review(job_id, result, kind=kind)

    def reopen_last_review(self, job_id: str, result: ProcessResult) -> DocumentJob:
        return self._phases.reopen_last_review(job_id, result)

    @staticmethod
    def rebuild_revision_draft(
        result: ProcessResult,
        reviewed_text: str,
    ) -> RevisionDraft | None:
        draft = result.revision_draft
        if draft is None:
            return None
        return build_revision_draft(
            draft.original_markdown,
            reviewed_text,
            kinds=draft.kinds,
        )

    def render_revision(
        self,
        draft: RevisionDraft,
        reviews: tuple[ReviewSession, ...],
        preceding_reviews: tuple[ReviewSession, ...],
    ) -> str:
        reviewed_text = render_revision_reviews(draft, reviews, self._artifacts)
        return ensure_quality_reviews_applied(
            reviewed_text,
            preceding_reviews,
            self._artifacts,
        )

    def prepare_book(
        self,
        job_id: str,
        result: ProcessResult,
        reviewed_text: str,
    ) -> BookDocument:
        return self._publication.prepare_book(job_id, result, reviewed_text)

    def save_book(self, job_id: str, book: BookDocument) -> None:
        self._publication.save_book(job_id, book)

    def publish_book(
        self,
        job_id: str,
        result: ProcessResult,
        reviewed_text: str,
        book: BookDocument,
        reviews: tuple[ReviewSession, ...],
    ) -> PublishedReview:
        return self._publication.publish_book(job_id, result, reviewed_text, book, reviews)

    def publish_text(
        self,
        job_id: str,
        result: ProcessResult,
        reviewed_text: str,
        reviews: tuple[ReviewSession, ...],
    ) -> PublishedReview:
        return self._publication.publish_text(job_id, result, reviewed_text, reviews)


__all__ = ["ReviewFlowCoordinator"]
