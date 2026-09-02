"""Prepare, persist and atomically publish reviewed document results."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from parsezen.application.artifact_repository import ArtifactRepository
from parsezen.application.book_editor import (
    book_package_structure_fingerprint,
    book_source_fingerprint,
    create_book_from_epub_package,
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
from parsezen.epub_conversion import patch_epub_xhtml_package
from parsezen.errors import RequestValidationError
from parsezen.final_integrity import (
    IntegrityLedger,
    binary_integrity_capture,
    merge_integrity_reports,
)
from parsezen.output import replace_binary_output
from parsezen.pdf_conversion import strip_pdf_page_markers
from parsezen.pipeline.contracts import ProcessResult
from parsezen.processing import apply_reviewed_revision


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
        artifacts: ArtifactRepository,
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
        source_text = strip_pdf_page_markers(reviewed_text)
        planning_text = reviewed_text
        existing = self._books.load_book(job_id)
        preserve_source_package = bool(
            result.preserve_epub_package_on_unchanged_review
            and result.revision_draft is None
            and result.review_markdown is not None
            and source_text == strip_pdf_page_markers(result.review_markdown)
        )
        if (
            existing is not None
            and existing.source_fingerprint
            in {
                book_source_fingerprint(planning_text),
                book_source_fingerprint(source_text),
            }
            and (not preserve_source_package or existing.source_package_artifact_id is not None)
        ):
            return existing
        if result.final_path.suffix.casefold() != ".epub":
            raise RequestValidationError("Este resultado no necesita un editor EPUB.")
        metadata = result.revision_epub_metadata
        if metadata is None:
            raise RequestValidationError("Faltan los metadatos para editar este EPUB.")
        book = (
            create_book_from_epub_package(
                result.final_path,
                source_text,
                result.revision_resources,
                metadata,
                self._artifacts,
                job_id=job_id,
            )
            if preserve_source_package
            else create_book_from_markdown(
                planning_text,
                result.revision_resources,
                metadata,
                self._artifacts,
                job_id=job_id,
            )
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
        source_text = strip_pdf_page_markers(reviewed_text)
        package_edit_allowed = bool(
            result.preserve_epub_package_on_unchanged_review
            and result.revision_draft is None
            and result.review_markdown is not None
            and source_text == strip_pdf_page_markers(result.review_markdown)
            and book.source_package_artifact_id is not None
            and book.package_structure_fingerprint is not None
            and book.package_structure_fingerprint
            == book_package_structure_fingerprint(
                book,
                self._artifacts,
                job_id=job_id,
            )
        )
        preserve_existing_package = False
        if package_edit_allowed:
            if book.source_package_artifact_id is None:
                raise AssertionError("A package-bound draft must retain its source artifact.")
            replacements = {
                section.source_archive_path: self._artifacts.read(
                    job_id,
                    section.xhtml_artifact_id,
                )
                for root in book.sections
                for section in root.walk()
                if section.source_archive_path is not None
                and section.source_xhtml_artifact_id is not None
                and section.xhtml_artifact_id != section.source_xhtml_artifact_id
            }
            content = patch_epub_xhtml_package(
                self._artifacts.read(job_id, book.source_package_artifact_id),
                replacements,
            )
            preserve_existing_package = not replacements
        else:
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
        if preserve_existing_package and result.final_path.read_bytes() == content:
            integrity(result.final_path)
        else:
            replace_binary_output(
                result.final_path,
                content,
                validate_staged=integrity,
            )
        updated = replace(
            result,
            translation_quality_report=result.translation_quality_for_review,
            review_translation_quality_report=result.translation_quality_for_review,
            review_markdown=source_text,
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
            final_integrity_report=merge_integrity_reports(
                result.final_integrity_report,
                integrity.report,
            ),
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
