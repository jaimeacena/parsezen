"""Prepare, persist and atomically publish reviewed document results."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from parsezen.application.book_editor import (
    create_book_from_markdown,
    publish_book,
)
from parsezen.application.review_finalization import (
    FinalizedReview,
    ReviewFinalizationCoordinator,
)
from parsezen.domain.books import BookDocument
from parsezen.domain.reviews import ReviewSession
from parsezen.epub_builder import validate_epub_file
from parsezen.errors import RequestValidationError
from parsezen.final_integrity import IntegrityLedger, binary_integrity_capture
from parsezen.infrastructure.artifact_store import ArtifactStore
from parsezen.output import replace_binary_output
from parsezen.pdf_conversion import strip_pdf_page_markers
from parsezen.processing import ProcessResult, apply_reviewed_revision


class BookDraftRepository(Protocol):
    def load_book(self, job_id: str) -> BookDocument | None: ...

    def save_book(self, job_id: str, book: BookDocument) -> None: ...


@dataclass(frozen=True, slots=True)
class PublishedReview:
    result: ProcessResult
    finalization: FinalizedReview


class ReviewPublicationCoordinator:
    """Keep draft durability, file publication and job completion in one order."""

    def __init__(
        self,
        books: BookDraftRepository,
        artifacts: ArtifactStore,
        finalization: ReviewFinalizationCoordinator,
    ) -> None:
        self._books = books
        self._artifacts = artifacts
        self._finalization = finalization

    def prepare_book(
        self,
        job_id: str,
        result: ProcessResult,
        reviewed_text: str,
    ) -> BookDocument:
        existing = self._books.load_book(job_id)
        if existing is not None:
            return existing
        if result.final_path.suffix.casefold() != ".epub":
            raise RequestValidationError("Este resultado no necesita un editor EPUB.")
        metadata = result.revision_epub_metadata
        if metadata is None:
            raise RequestValidationError("Faltan los metadatos para editar este EPUB.")
        book = create_book_from_markdown(
            strip_pdf_page_markers(reviewed_text),
            result.revision_resources,
            metadata,
            self._artifacts,
            job_id=job_id,
        )
        self._books.save_book(job_id, book)
        return book

    def save_book(self, job_id: str, book: BookDocument) -> None:
        """Persist a recoverable editor draft without touching the final output."""

        self._books.save_book(job_id, book)

    def publish_book(
        self,
        job_id: str,
        result: ProcessResult,
        reviewed_text: str,
        book: BookDocument,
        reviews: tuple[ReviewSession, ...],
    ) -> PublishedReview:
        """Persist the draft, replace the EPUB, then complete and clean the job."""

        self._books.save_book(job_id, book)
        content = publish_book(book, self._artifacts, job_id=job_id)
        integrity = binary_integrity_capture(
            content,
            format_label="EPUB",
            validate_container=validate_epub_file,
            ledger=IntegrityLedger(
                blocks=len(book.spine),
                headings=len(book.spine),
                images=sum(resource.media_type.startswith("image/") for resource in book.resources),
                resources=len(book.resources),
            ),
        )
        replace_binary_output(
            result.final_path,
            content,
            validate_staged=integrity,
        )
        updated = replace(
            result,
            review_markdown=strip_pdf_page_markers(reviewed_text),
            review_required=False,
            revision_approved=True,
            epub_chapters=len(book.spine),
            preserved_images=len(
                tuple(
                    resource
                    for resource in book.resources
                    if resource.media_type.startswith("image/")
                )
            ),
            final_integrity_report=integrity.report,
        )
        finalized = self._finalization.finalize(
            job_id,
            updated.final_path,
            reviews,
        )
        return PublishedReview(updated, finalized)

    def publish_text(
        self,
        job_id: str,
        result: ProcessResult,
        reviewed_text: str,
        reviews: tuple[ReviewSession, ...],
    ) -> PublishedReview:
        updated = apply_reviewed_revision(result, reviewed_text)
        finalized = self._finalization.finalize(
            job_id,
            updated.final_path,
            reviews,
        )
        return PublishedReview(updated, finalized)
