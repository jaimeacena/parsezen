from pathlib import Path

from parsezen.application.revision_materializer import (
    create_revision_review,
    render_revision_reviews,
)
from parsezen.domain.reviews import ReviewChoice
from parsezen.infrastructure.artifact_store import ArtifactStore
from parsezen.revision import RevisionKind, build_revision_draft


def reversible(payload: bytes) -> bytes:
    return bytes(value ^ 0x6B for value in payload)


def test_revision_materializer_combines_original_proposed_and_manual_choices(
    tmp_path: Path,
) -> None:
    draft = build_revision_draft(
        "One.\n\nMiddle.\n\nTwo.\n",
        "One corrected.\n\nMiddle.\n\nTwo corrected.\n",
        kinds=frozenset({RevisionKind.CONTENT}),
    )
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    review = create_revision_review(
        draft,
        revision_kind=RevisionKind.CONTENT,
        job_id="job",
        configuration_revision=1,
        artifacts=store,
    )
    assert review is not None
    first, second = review.units
    edited = store.put_text(job_id="job", text="One manual.\n\n")
    review = review.decide(
        first.id,
        ReviewChoice.EDITED,
        edited_artifact_id=edited.id,
    ).decide(second.id, ReviewChoice.ORIGINAL)

    rendered = render_revision_reviews(draft, (review,), store)

    assert rendered == "One manual.\n\nMiddle.\n\nTwo.\n"


def test_revision_materializer_marks_high_risk_changes_as_original_by_default(
    tmp_path: Path,
) -> None:
    draft = build_revision_draft(
        "La edición contiene 36 capítulos completos.\n\n",
        "La edición contiene 35 capítulos completos.\n\n",
        kinds=frozenset({RevisionKind.CONTENT}),
    )
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)

    review = create_revision_review(
        draft,
        revision_kind=RevisionKind.CONTENT,
        job_id="job",
        configuration_revision=1,
        artifacts=store,
    )

    assert review is not None
    assert review.units[0].recommended_choice is ReviewChoice.ORIGINAL
    assert review.units[0].warning is not None
