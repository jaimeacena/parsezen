from datetime import UTC, datetime
from pathlib import Path

from parsezen.application.outcome_summary import build_outcome_summary
from parsezen.domain.estimates import DurationEstimate
from parsezen.domain.jobs import (
    DocumentFormat,
    DocumentJob,
    DocumentSource,
    JobConfiguration,
    OutputConfiguration,
    TranslationConfiguration,
)
from parsezen.domain.outcomes import EarlyCheckReport
from parsezen.domain.reviews import (
    ReviewChoice,
    ReviewKind,
    ReviewSession,
    ReviewUnit,
)
from parsezen.domain.stages import StageKind
from parsezen.final_integrity import FinalIntegrityReport, IntegrityLedger
from parsezen.pdf_conversion import PdfQualityReport, PdfReviewIssue
from parsezen.processing import ProcessResult


def test_outcome_summary_keeps_integrity_incidents_and_review_separate() -> None:
    source = DocumentSource(Path("book.pdf"), DocumentFormat.PDF, 100, 1)
    job = DocumentJob.create(
        source,
        JobConfiguration(
            output=OutputConfiguration(format=DocumentFormat.EPUB),
            translation=TranslationConfiguration(
                enabled=True,
                target_language="es",
                manual_review=True,
            ),
        ),
        order=0,
        job_id="book",
    )
    review = ReviewSession.create(
        job_id=job.id,
        stage=StageKind.TRANSLATE,
        kind=ReviewKind.TRANSLATION,
        input_artifact_id="input",
        input_version=job.configuration_revision,
        units=(
            ReviewUnit(
                id="one",
                original_artifact_id="original-one",
                proposed_artifact_id="proposed-one",
                choice=ReviewChoice.PROPOSED,
            ),
            ReviewUnit(
                id="two",
                original_artifact_id="original-two",
                proposed_artifact_id="proposed-two",
                edited_artifact_id="edited-two",
                choice=ReviewChoice.EDITED,
            ),
            ReviewUnit(
                id="three",
                original_artifact_id="original-three",
                proposed_artifact_id="proposed-three",
                choice=ReviewChoice.ORIGINAL,
            ),
        ),
        now=datetime.now(UTC),
    )
    result = ProcessResult(
        Path("book.epub"),
        pdf_quality_report=PdfQualityReport(
            (1, 2, 3),
            (2,),
            (PdfReviewIssue(2, "Revisar", "texto"),),
        ),
        preserved_images=4,
        epub_chapters=7,
        revision_approved=True,
        final_integrity_report=FinalIntegrityReport(
            "EPUB",
            ("Contenedor válido", "Recursos comprobados"),
            IntegrityLedger(images=4, resources=4),
        ),
    )

    summary = build_outcome_summary(
        job,
        result,
        reviews=(review,),
        duration_seconds=600,
        estimate=DurationEstimate(480, 600, 900, 4),
        early_check=EarlyCheckReport((1, 50, 100), warning_pages=1),
    )

    assert summary.integrity_verified
    assert summary.integrity_checks == 2
    assert summary.conversion_issues == 1
    assert summary.review_units == 3
    assert summary.review_changes == 2
    assert summary.review_edits == 1
    assert summary.review_originals == 1
    assert summary.early_check_pages == 3
    assert summary.early_check_warnings == 1
