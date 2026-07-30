from pathlib import Path

from parsezen.application.review_plan import (
    ReviewStep,
    review_steps_for_result,
    review_workload_for_result,
)
from parsezen.domain.jobs import (
    DocumentFormat,
    JobConfiguration,
    OutputConfiguration,
    RefinementConfiguration,
    StructureConfiguration,
    TranslationConfiguration,
)
from parsezen.domain.reviews import ReviewKind
from parsezen.domain.stages import StageKind
from parsezen.epub_builder import EpubBookMetadata
from parsezen.pdf_conversion import PdfQualityReport, PdfReviewIssue
from parsezen.processing import ProcessResult
from parsezen.revision import RevisionChange, RevisionDraft, RevisionKind
from parsezen.translation_quality import (
    TranslationIssueKind,
    TranslationQualityIssue,
    TranslationQualityReport,
)


def full_configuration() -> JobConfiguration:
    return JobConfiguration(
        output=OutputConfiguration(format=DocumentFormat.EPUB),
        translation=TranslationConfiguration(enabled=True, target_language="es"),
        refinement=RefinementConfiguration(enabled=True),
        structure=StructureConfiguration(enabled=True),
    )


def full_review_result() -> ProcessResult:
    return ProcessResult(
        Path("book.epub"),
        pdf_quality_report=PdfQualityReport(
            processed_pages=(1,),
            ocr_pages=(1,),
            issues=(PdfReviewIssue(1, "Revisar", "Texto"),),
        ),
        translation_quality_report=TranslationQualityReport(
            source_language="en",
            target_language="es",
            detected_language="es",
            checked_segments=1,
            source_characters=4,
            translated_characters=5,
            total_issues=1,
            issues=(
                TranslationQualityIssue(
                    1,
                    TranslationIssueKind.SOURCE_TEXT,
                    "Sin traducir",
                    "Text",
                    "Text",
                ),
            ),
        ),
        revision_draft=RevisionDraft(
            "Text\n",
            "# Texto\n",
            (
                RevisionChange(
                    "content",
                    RevisionKind.CONTENT,
                    0,
                    1,
                    "Text\n",
                    "Texto\n",
                    "Corrección",
                ),
                RevisionChange(
                    "structure",
                    RevisionKind.STRUCTURE,
                    0,
                    1,
                    "Texto\n",
                    "# Texto\n",
                    "Estructura",
                ),
            ),
            frozenset({RevisionKind.CONTENT, RevisionKind.STRUCTURE}),
        ),
        review_required=True,
    )


def test_review_plan_orders_only_real_work_chronologically() -> None:
    assert review_steps_for_result(full_review_result(), full_configuration()) == (
        ReviewStep(ReviewKind.OCR, StageKind.PREPARE),
        ReviewStep(ReviewKind.TRANSLATION, StageKind.TRANSLATE),
        ReviewStep(ReviewKind.REFINEMENT, StageKind.REFINE),
        ReviewStep(ReviewKind.STRUCTURE, StageKind.STRUCTURE),
    )


def test_empty_revision_kinds_do_not_create_phantom_reviews() -> None:
    result = ProcessResult(
        Path("book.md"),
        revision_draft=RevisionDraft(
            "Text\n",
            "Text\n",
            (),
            frozenset({RevisionKind.CONTENT}),
        ),
    )

    assert review_steps_for_result(result, full_configuration()) == ()


def test_generic_manual_review_keeps_one_conservative_fallback_gate() -> None:
    configuration = JobConfiguration()
    result = ProcessResult(
        Path("book.md"),
        review_markdown="Text",
        review_required=True,
    )

    assert review_steps_for_result(result, configuration) == (
        ReviewStep(ReviewKind.CONVERSION_WARNING, StageKind.PREPARE),
    )


def test_epub_without_other_reviews_uses_the_final_personalization_gate() -> None:
    result = ProcessResult(
        Path("book.epub"),
        review_markdown="# Book\n",
        review_required=True,
        revision_epub_metadata=EpubBookMetadata("Book", "en"),
    )

    assert review_steps_for_result(result, JobConfiguration()) == (
        ReviewStep(ReviewKind.STRUCTURE, StageKind.PUBLISH),
    )


def test_review_workload_uses_real_unit_counts() -> None:
    translation_issues = tuple(
        TranslationQualityIssue(
            index,
            TranslationIssueKind.ALIGNMENT,
            "Revisar",
            f"Original {index}",
            f"Traducción {index}",
        )
        for index in range(10)
    )
    content_changes = tuple(
        RevisionChange(
            f"content-{index}",
            RevisionKind.CONTENT,
            0,
            1,
            "Original\n",
            "Propuesta\n",
            "Corrección",
        )
        for index in range(90)
    )
    result = ProcessResult(
        Path("book.epub"),
        translation_quality_report=TranslationQualityReport(
            source_language="en",
            target_language="es",
            detected_language="es",
            checked_segments=100,
            source_characters=1_000,
            translated_characters=1_100,
            total_issues=10,
            issues=translation_issues,
        ),
        revision_draft=RevisionDraft(
            "Original\n",
            "Propuesta\n",
            content_changes,
            frozenset({RevisionKind.CONTENT}),
        ),
        review_required=True,
    )

    workload = review_workload_for_result(result, full_configuration())

    assert tuple((item.kind, item.unit_count) for item in workload) == (
        (ReviewKind.TRANSLATION, 10),
        (ReviewKind.REFINEMENT, 90),
    )
