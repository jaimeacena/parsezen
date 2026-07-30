from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from parsezen.application.book_editor import BookEditor
from parsezen.application.job_execution import JobExecutionController
from parsezen.application.job_queue import JobQueue
from parsezen.application.review_finalization import (
    FinalizedReview,
    ReviewFinalizationCoordinator,
)
from parsezen.application.review_publication import ReviewPublicationCoordinator
from parsezen.domain.books import BookDocument
from parsezen.domain.jobs import (
    DocumentFormat,
    DocumentSource,
    JobConfiguration,
    JobStatus,
    OutputConfiguration,
    StructureConfiguration,
)
from parsezen.domain.reviews import ReviewSession
from parsezen.domain.stages import StageKind
from parsezen.epub_builder import EpubBookMetadata
from parsezen.infrastructure.artifact_store import ArtifactStore
from parsezen.processing import ProcessResult


def reversible(payload: bytes) -> bytes:
    return bytes(value ^ 0x57 for value in payload)


class DraftRepository:
    def __init__(self) -> None:
        self.book: BookDocument | None = None
        self.saved_books: list[BookDocument] = []
        self.deleted: list[str] = []
        self.reviews: list[ReviewSession] = []

    def load_book(self, _job_id: str) -> BookDocument | None:
        return self.book

    def save_book(self, _job_id: str, book: BookDocument) -> None:
        self.book = book
        self.saved_books.append(book)

    def save_review(self, review: ReviewSession) -> None:
        self.reviews.append(review)

    def delete_review_material(self, job_id: str) -> None:
        self.deleted.append(job_id)
        self.book = None


def _blocked_job(
    source: Path,
) -> tuple[JobQueue, JobExecutionController]:
    queue = JobQueue()
    job = queue.add(
        DocumentSource.inspect(source),
        JobConfiguration(
            output=OutputConfiguration(format=DocumentFormat.EPUB),
            structure=StructureConfiguration(enabled=True),
        ),
        job_id="job",
    )
    execution = JobExecutionController(queue)
    execution.start_next(job.id)
    execution.advance(job.id, StageKind.PUBLISH)
    execution.block_completed_result_for_review(
        job.id,
        StageKind.STRUCTURE,
        review_id="structure-gate",
    )
    return queue, execution


def _result(destination: Path) -> ProcessResult:
    return ProcessResult(
        destination,
        review_markdown="# Chapter\n\nBody.\n",
        review_required=True,
        revision_epub_metadata=EpubBookMetadata("Book", "en", "Author"),
    )


def test_epub_draft_is_recoverable_and_reused_before_publication(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("# Chapter\n\nBody.", encoding="utf-8")
    destination = tmp_path / "book.epub"
    repository = DraftRepository()
    artifacts = ArtifactStore(
        tmp_path / "artifacts",
        protect=reversible,
        unprotect=reversible,
    )
    queue, execution = _blocked_job(source)
    finalization = ReviewFinalizationCoordinator(
        queue,
        execution,
        repository,
        artifacts,
    )
    coordinator = ReviewPublicationCoordinator(repository, artifacts, finalization)

    prepared = coordinator.prepare_book(
        "job",
        _result(destination),
        "<!-- PZDOC PDF PAGE 1 -->\n\n# Chapter\n\nBody.\n",
    )
    edited = BookEditor(prepared, artifacts, job_id="job").update_metadata(
        title="Edited title",
        author="Author",
        language="es",
    )
    coordinator.save_book("job", edited)

    assert coordinator.prepare_book("job", _result(destination), "ignored") is edited
    assert repository.saved_books[0].metadata.title == "Book"
    assert repository.saved_books[0].metadata.identifier is not None
    assert "PZDOC" not in BookEditor(
        repository.saved_books[0],
        artifacts,
        job_id="job",
    ).editable_html(prepared.spine[0])


def test_epub_publication_replaces_output_before_completing_and_cleans_draft(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("# Chapter\n\nBody.", encoding="utf-8")
    destination = tmp_path / "book.epub"
    destination.write_bytes(b"preliminary")
    repository = DraftRepository()
    artifacts = ArtifactStore(
        tmp_path / "artifacts",
        protect=reversible,
        unprotect=reversible,
    )
    queue, execution = _blocked_job(source)
    coordinator = ReviewPublicationCoordinator(
        repository,
        artifacts,
        ReviewFinalizationCoordinator(queue, execution, repository, artifacts),
    )
    result = _result(destination)
    book = coordinator.prepare_book("job", result, result.review_markdown or "")

    published = coordinator.publish_book("job", result, result.review_markdown or "", book, ())

    assert destination.read_bytes().startswith(b"PK")
    assert published.result.review_required is False
    assert published.result.revision_approved is True
    assert published.finalization.job.status is JobStatus.COMPLETED
    assert repository.deleted == ["job"]
    assert repository.book is None
    assert not (artifacts.root / "job").exists()


def test_failed_completion_keeps_the_published_book_draft_recoverable(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("# Chapter\n\nBody.", encoding="utf-8")
    destination = tmp_path / "book.epub"
    repository = DraftRepository()
    artifacts = ArtifactStore(
        tmp_path / "artifacts",
        protect=reversible,
        unprotect=reversible,
    )
    _queue, _execution = _blocked_job(source)

    class FailingFinalization:
        def finalize(
            self,
            _job_id: str,
            result_path: Path,
            _reviews: tuple[ReviewSession, ...],
        ) -> FinalizedReview:
            assert result_path.read_bytes().startswith(b"PK")
            raise OSError("state is locked")

    coordinator = ReviewPublicationCoordinator(
        repository,
        artifacts,
        cast(ReviewFinalizationCoordinator, FailingFinalization()),
    )
    result = _result(destination)
    book = coordinator.prepare_book("job", result, result.review_markdown or "")

    with pytest.raises(OSError, match="locked"):
        coordinator.publish_book("job", result, result.review_markdown or "", book, ())

    assert repository.book == book
    assert destination.read_bytes().startswith(b"PK")
    assert (artifacts.root / "job").is_dir()
