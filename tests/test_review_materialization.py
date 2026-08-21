from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from parsezen.application.review_materialization import (
    ReviewMaterializationService,
    review_for_current_candidate,
)
from parsezen.domain.jobs import (
    DocumentJob,
    DocumentSource,
    JobConfiguration,
    TranslationConfiguration,
)
from parsezen.domain.reviews import (
    ReviewChoice,
    ReviewKind,
    ReviewSession,
    ReviewStatus,
    ReviewUnit,
)
from parsezen.domain.stages import StageKind
from parsezen.processing import ProcessResult
from parsezen.revision import RevisionKind, build_revision_draft
from parsezen.translation_quality import (
    TranslationIssueKind,
    TranslationQualityIssue,
    TranslationQualityReport,
)


@dataclass(frozen=True)
class ArtifactReferenceStub:
    id: str


@dataclass
class ArtifactRepositoryStub:
    values: dict[tuple[str, str], bytes] = field(default_factory=dict)
    sequence: int = 0

    def put(
        self,
        *,
        job_id: str,
        payload: bytes,
        media_type: str,
        artifact_id: str | None = None,
    ) -> ArtifactReferenceStub:
        del media_type
        self.sequence += 1
        identifier = artifact_id or f"artifact-{self.sequence}"
        self.values[job_id, identifier] = payload
        return ArtifactReferenceStub(identifier)

    def put_text(
        self,
        *,
        job_id: str,
        text: str,
        media_type: str = "text/plain; charset=utf-8",
        artifact_id: str | None = None,
    ) -> ArtifactReferenceStub:
        return self.put(
            job_id=job_id,
            payload=text.encode("utf-8"),
            media_type=media_type,
            artifact_id=artifact_id,
        )

    def read(self, job_id: str, artifact_id: str) -> bytes:
        return self.values[job_id, artifact_id]

    def read_text(self, job_id: str, artifact_id: str) -> str:
        return self.read(job_id, artifact_id).decode("utf-8")


@dataclass
class ReviewRepositoryStub:
    reviews: dict[str, ReviewSession] = field(default_factory=dict)

    def save_review(self, review: ReviewSession) -> None:
        self.reviews[review.id] = review

    def load_reviews(self, *, job_id: str | None = None) -> tuple[ReviewSession, ...]:
        return tuple(
            review for review in self.reviews.values() if job_id is None or review.job_id == job_id
        )


def _job(tmp_path: Path) -> DocumentJob:
    source = tmp_path / "source.md"
    source.write_text("Source", encoding="utf-8")
    return DocumentJob.create(
        DocumentSource.inspect(source),
        JobConfiguration(translation=TranslationConfiguration(enabled=True, target_language="es")),
        order=0,
        job_id="job",
    )


def _translation_result(tmp_path: Path) -> ProcessResult:
    return ProcessResult(
        tmp_path / "result.md",
        review_markdown="Hola mundo.",
        translation_quality_report=TranslationQualityReport(
            "en",
            "Español",
            "es",
            1,
            12,
            11,
            1,
            (
                TranslationQualityIssue(
                    1,
                    TranslationIssueKind.ALIGNMENT,
                    "Revisar",
                    "Hello world.",
                    "Hola mundo.",
                    "translation-one",
                ),
            ),
        ),
        review_required=True,
    )


def test_quality_materialization_is_reusable_without_qt_or_concrete_storage(
    tmp_path: Path,
) -> None:
    job = _job(tmp_path)
    result = _translation_result(tmp_path)
    reviews = ReviewRepositoryStub()
    artifacts = ArtifactRepositoryStub()
    service = ReviewMaterializationService(reviews, artifacts)

    first = service.materialize_quality(job, result, job.source.path, "Hola mundo.")
    translation = dict(first.materialized)[ReviewKind.TRANSLATION]
    assert translation is not None
    assert not first.complete

    applied = translation.decide(
        translation.units[0].id,
        ReviewChoice.PROPOSED,
    ).apply()
    reviews.save_review(applied)
    resumed = service.materialize_quality(job, result, job.source.path, "Hola mundo.")

    assert resumed.complete
    assert resumed.reviewed_text == "Hola mundo."
    assert dict(resumed.materialized)[ReviewKind.TRANSLATION] is not None


def test_quality_materialization_uses_report_aligned_with_review_candidate(
    tmp_path: Path,
) -> None:
    job = _job(tmp_path)
    base_result = _translation_result(tmp_path)
    candidate_report = TranslationQualityReport(
        "en",
        "Español",
        "es",
        1,
        12,
        11,
        1,
        (
            TranslationQualityIssue(
                1,
                TranslationIssueKind.FIDELITY,
                "Revisar el candidato",
                "Hello candidate.",
                "Hola candidato.",
                "candidate-one",
            ),
        ),
    )
    result = replace(
        base_result,
        review_markdown="Hola candidato.",
        review_translation_quality_report=candidate_report,
    )
    service = ReviewMaterializationService(ReviewRepositoryStub(), ArtifactRepositoryStub())

    prepared = service.materialize_quality(
        job,
        result,
        job.source.path,
        "Hola candidato.",
    )

    translation = dict(prepared.materialized)[ReviewKind.TRANSLATION]
    assert translation is not None
    assert translation.units[0].label == "Revisar el candidato"


def test_quality_materialization_preserves_a_saved_review_when_anchor_disappears(
    tmp_path: Path,
) -> None:
    job = _job(tmp_path)
    reviews = ReviewRepositoryStub()
    artifacts = ArtifactRepositoryStub()
    service = ReviewMaterializationService(reviews, artifacts)
    prepared = service.materialize_quality(
        job,
        _translation_result(tmp_path),
        job.source.path,
        "Hola mundo.",
    )
    saved = dict(prepared.materialized)[ReviewKind.TRANSLATION]
    assert saved is not None

    with pytest.raises(ValueError, match="[Ss]e ha conservado"):
        service.materialize_quality(
            job,
            ProcessResult(tmp_path / "result.md", review_markdown="Texto distinto"),
            job.source.path,
            "Texto distinto",
        )

    assert reviews.reviews[saved.id] == saved


def test_matching_ocr_review_refreshes_cleaned_candidate_artifacts() -> None:
    saved = ReviewSession.create(
        job_id="job",
        stage=StageKind.PREPARE,
        kind=ReviewKind.OCR,
        input_artifact_id="old-input",
        input_version=1,
        units=(
            ReviewUnit(
                "page-1",
                "old-image",
                "old-text",
                choice=ReviewChoice.PROPOSED,
                original_selectable=False,
            ),
        ),
    ).apply()
    candidate = ReviewSession.create(
        job_id="job",
        stage=StageKind.PREPARE,
        kind=ReviewKind.OCR,
        input_artifact_id="new-input",
        input_version=1,
        units=(
            ReviewUnit(
                "page-1",
                "new-image",
                "new-text",
                original_selectable=False,
            ),
        ),
    )

    refreshed = review_for_current_candidate(saved, candidate)

    assert refreshed.status is ReviewStatus.APPLIED
    assert refreshed.input_artifact_id == "new-input"
    assert refreshed.units[0].choice is ReviewChoice.PROPOSED
    assert refreshed.units[0].original_artifact_id == "new-image"
    assert refreshed.units[0].proposed_artifact_id == "new-text"


def test_missing_human_edit_reopens_review_with_fresh_candidate() -> None:
    artifacts = ArtifactRepositoryStub()
    artifacts.put(job_id="job", payload=b"image", media_type="image/jpeg", artifact_id="new-image")
    artifacts.put_text(job_id="job", text="Fresh OCR", artifact_id="new-text")
    saved = ReviewSession.create(
        job_id="job",
        stage=StageKind.PREPARE,
        kind=ReviewKind.OCR,
        input_artifact_id="old-input",
        input_version=1,
        units=(
            ReviewUnit(
                "page-1",
                "old-image",
                "old-text",
                edited_artifact_id="cleaned-edit",
                choice=ReviewChoice.EDITED,
                original_selectable=False,
            ),
        ),
    ).apply()
    candidate = ReviewSession.create(
        job_id="job",
        stage=StageKind.PREPARE,
        kind=ReviewKind.OCR,
        input_artifact_id="new-input",
        input_version=1,
        units=(
            ReviewUnit(
                "page-1",
                "new-image",
                "new-text",
                original_selectable=False,
            ),
        ),
    )

    refreshed = review_for_current_candidate(saved, candidate, artifacts=artifacts)

    assert refreshed.status is ReviewStatus.PENDING
    assert refreshed.units[0].choice is None
    assert refreshed.units[0].edited_artifact_id is None
    assert refreshed.units[0].original_artifact_id == "new-image"
    assert refreshed.units[0].proposed_artifact_id == "new-text"


def test_revision_materialization_exposes_only_real_revision_capabilities(
    tmp_path: Path,
) -> None:
    job = _job(tmp_path)
    draft = build_revision_draft(
        "# Title\n\nOriginal.\n",
        "# Title\n\nImproved.\n",
        kinds=frozenset({RevisionKind.CONTENT}),
    )
    result = ProcessResult(
        tmp_path / "result.md",
        revision_draft=draft,
        review_required=True,
    )
    service = ReviewMaterializationService(ReviewRepositoryStub(), ArtifactRepositoryStub())

    materialized = service.materialize_revisions(job, result, draft)

    by_kind = {kind: review for _revision, kind, review in materialized.materialized}
    assert by_kind[ReviewKind.REFINEMENT] is not None
    assert by_kind[ReviewKind.STRUCTURE] is None
    assert materialized.phase_plan == ((ReviewKind.REFINEMENT, 1),)
