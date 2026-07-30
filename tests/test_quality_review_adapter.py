from dataclasses import replace
from pathlib import Path

import pytest

from parsezen.application.quality_review_adapter import (
    apply_pdf_review,
    apply_translation_review,
    create_pdf_review,
    create_translation_review,
)
from parsezen.domain.reviews import ReviewChoice, ReviewKind, ReviewSession, ReviewUnit
from parsezen.domain.stages import StageKind
from parsezen.infrastructure.artifact_store import ArtifactStore
from parsezen.pdf_conversion import PdfQualityReport, PdfReviewIssue
from parsezen.translation_quality import (
    TranslationIssueKind,
    TranslationQualityIssue,
    TranslationQualityReport,
)


def reversible(payload: bytes) -> bytes:
    return bytes(value ^ 0x17 for value in payload)


def test_translation_review_applies_manual_excerpt_without_touching_other_text(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    input_record = store.put_text(job_id="job", text="Hello. Hola. End.")
    report = TranslationQualityReport(
        "en",
        "Español",
        "es",
        1,
        6,
        5,
        1,
        (
            TranslationQualityIssue(
                1,
                TranslationIssueKind.ALIGNMENT,
                "Revisar este segmento",
                "Hello.",
                "Hola.",
                "segment",
            ),
        ),
    )
    review = create_translation_review(
        report,
        job_id="job",
        configuration_revision=1,
        input_artifact_id=input_record.id,
        artifacts=store,
    )
    assert review is not None
    edited = store.put_text(job_id="job", text="Buenas.")
    review = review.decide(
        "segment",
        ReviewChoice.EDITED,
        edited_artifact_id=edited.id,
    )

    assert apply_translation_review("Start. Hola. End.", review, store) == ("Start. Buenas. End.")


def test_translation_review_anchors_collapsed_whitespace_to_the_real_document(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    document = "Start.\n\nHola\nmundo.\n\nEnd."
    input_record = store.put_text(job_id="job", text=document)
    report = TranslationQualityReport(
        "en",
        "Español",
        "es",
        1,
        6,
        5,
        1,
        (
            TranslationQualityIssue(
                1,
                TranslationIssueKind.ALIGNMENT,
                "Revisar este segmento",
                "Hello world.",
                "Hola mundo.",
                "segment",
            ),
        ),
    )
    review = create_translation_review(
        report,
        job_id="job",
        configuration_revision=1,
        input_artifact_id=input_record.id,
        artifacts=store,
    )

    assert review is not None
    assert store.read_text("job", review.units[0].proposed_artifact_id or "") == "Hola\nmundo."
    edited = store.put_text(job_id="job", text="Buenas, mundo.")
    review = review.decide(
        "segment",
        ReviewChoice.EDITED,
        edited_artifact_id=edited.id,
    )
    assert apply_translation_review(document, review, store) == ("Start.\n\nBuenas, mundo.\n\nEnd.")


def test_saved_review_tolerates_line_ending_normalization_when_applied(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    original = store.put_text(job_id="job", text="Hello world.")
    proposal = store.put_text(job_id="job", text="Hola\r\nmundo.")
    edited = store.put_text(job_id="job", text="Buenas, mundo.")
    review = ReviewSession.create(
        job_id="job",
        stage=StageKind.TRANSLATE,
        kind=ReviewKind.TRANSLATION,
        input_artifact_id="input",
        input_version=1,
        units=(ReviewUnit("segment", original.id, proposal.id),),
    ).decide(
        "segment",
        ReviewChoice.EDITED,
        edited_artifact_id=edited.id,
    )

    assert apply_translation_review("Inicio.\nHola\nmundo.\nFin.", review, store) == (
        "Inicio.\nBuenas, mundo.\nFin."
    )


def test_pdf_review_uses_page_image_and_applies_edited_text(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF")
    input_record = store.put_text(job_id="job", text="<!-- page -->\nOld OCR")
    report = PdfQualityReport(
        (1,),
        (1,),
        (
            PdfReviewIssue(
                1,
                "OCR dudoso",
                "Old OCR",
                "page-one",
                True,
                "<!-- page -->",
            ),
        ),
    )
    monkeypatch.setattr(
        "parsezen.application.quality_review_adapter.render_pdf_page_cover",
        lambda *_args: b"jpeg-page",
    )

    review = create_pdf_review(
        report,
        source,
        job_id="job",
        configuration_revision=2,
        input_artifact_id=input_record.id,
        artifacts=store,
    )
    assert review is not None
    assert review.kind is ReviewKind.OCR
    assert not review.units[0].original_selectable
    edited = store.put_text(job_id="job", text="Corrected OCR")
    decided = review.decide(
        review.units[0].id,
        ReviewChoice.EDITED,
        edited_artifact_id=edited.id,
    )

    assert apply_pdf_review("<!-- page -->\nOld OCR", decided, store) == (
        "<!-- page -->\nCorrected OCR"
    )


def test_quality_review_handles_empty_reports_targets_and_invalid_decisions(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    empty_translation = TranslationQualityReport("en", "Español", "es", 1, 1, 1, 0, ())
    assert (
        create_translation_review(
            empty_translation,
            job_id="job",
            configuration_revision=1,
            input_artifact_id="input",
            artifacts=store,
        )
        is None
    )
    assert (
        create_pdf_review(
            PdfQualityReport((), (), ()),
            tmp_path / "missing.pdf",
            job_id="job",
            configuration_revision=1,
            input_artifact_id="input",
            artifacts=store,
        )
        is None
    )

    original = store.put_text(job_id="job", text="image")
    proposal = store.put_text(job_id="job", text="")
    unit = ReviewUnit(
        "unit",
        original.id,
        proposal.id,
        original_selectable=False,
        target="<!-- marker -->",
    )
    review = ReviewSession.create(
        job_id="job",
        stage=StageKind.PREPARE,
        kind=ReviewKind.OCR,
        input_artifact_id="input",
        input_version=1,
        units=(unit,),
    )
    edited = store.put_text(job_id="job", text="Inserted")
    decided = review.decide("unit", ReviewChoice.EDITED, edited_artifact_id=edited.id)
    assert apply_pdf_review("<!-- marker -->", decided, store) == ("<!-- marker -->\n\nInserted")

    with pytest.raises(ValueError, match="Decide todos"):
        apply_pdf_review("<!-- marker -->", review, store)
    with pytest.raises(ValueError, match="ya no coincide"):
        apply_pdf_review("different", decided, store)


def test_quality_review_supports_original_proposed_and_defensive_failures(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    original = store.put_text(job_id="job", text="Original")
    proposal = store.put_text(job_id="job", text="Proposal")
    base_unit = ReviewUnit("unit", original.id, proposal.id)
    review = ReviewSession.create(
        job_id="job",
        stage=StageKind.TRANSLATE,
        kind=ReviewKind.TRANSLATION,
        input_artifact_id="input",
        input_version=1,
        units=(base_unit,),
    )
    original_review = review.decide("unit", ReviewChoice.ORIGINAL)
    proposed_review = review.decide("unit", ReviewChoice.PROPOSED)

    assert apply_translation_review("Proposal", original_review, store) == "Original"
    assert apply_translation_review("Proposal", proposed_review, store) == "Proposal"

    image_only = replace(base_unit, original_selectable=False)
    invalid_original = replace(review, units=(replace(image_only, choice=ReviewChoice.ORIGINAL),))
    with pytest.raises(ValueError, match="imagen original"):
        apply_translation_review("Proposal", invalid_original, store)

    missing_edit = replace(review, units=(replace(base_unit, choice=ReviewChoice.EDITED),))
    with pytest.raises(ValueError, match="edición guardada"):
        apply_translation_review("Proposal", missing_edit, store)

    empty_proposal = store.put_text(job_id="job", text="")
    empty_review = replace(
        review,
        units=(
            replace(
                base_unit,
                proposed_artifact_id=empty_proposal.id,
                choice=ReviewChoice.PROPOSED,
            ),
        ),
    )
    with pytest.raises(ValueError, match="está vacío"):
        apply_translation_review("Proposal", empty_review, store)
