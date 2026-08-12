from dataclasses import replace
from pathlib import Path

import pytest

from parsezen.application.review_recommendation import (
    rebind_review_recommendation,
    recommend_targeted_review,
    recommendation_summary,
    reconstruct_completed_result,
)
from parsezen.domain.jobs import (
    DocumentFormat,
    DocumentJob,
    DocumentSource,
    JobConfiguration,
    OutputConfiguration,
    ReviewRecommendation,
    ReviewSignal,
)
from parsezen.epub_builder import EpubBookMetadata, build_epub
from parsezen.pdf_conversion import PdfQualityReport, PdfReviewIssue
from parsezen.processing import (
    ProcessResult,
    review_block_fingerprint,
    review_scope_fingerprint,
)
from parsezen.translation_quality import (
    TranslationIssueKind,
    TranslationQualityIssue,
    TranslationQualityReport,
)


def test_source_text_signal_recommends_only_the_affected_translation_block() -> None:
    report = TranslationQualityReport(
        "en",
        "es",
        "es",
        3,
        100,
        110,
        1,
        (
            TranslationQualityIssue(
                2,
                TranslationIssueKind.SOURCE_TEXT,
                "Queda texto original.",
                "Source excerpt.",
                "Source excerpt.",
            ),
        ),
    )
    result = ProcessResult(
        Path("result.md"),
        review_markdown="Primer bloque.\n\nSource excerpt.\n\nTercer bloque.\n",
        translation_quality_report=report,
    )

    recommendation = recommend_targeted_review(result)

    assert recommendation is not None
    assert recommendation.signal_counts == ((ReviewSignal.SOURCE_TEXT_RESIDUE, 1),)
    assert recommendation.block_positions == (1,)
    assert recommendation.scope_fingerprint == review_scope_fingerprint(
        result.review_markdown or ""
    )
    assert recommendation.block_fingerprints == (review_block_fingerprint("Source excerpt.\n\n"),)
    assert "1 bloque" in recommendation_summary(recommendation)


def test_one_weak_length_warning_does_not_trigger_ai_review() -> None:
    report = TranslationQualityReport(
        "en",
        "es",
        "es",
        1,
        100,
        200,
        1,
        (
            TranslationQualityIssue(
                1,
                TranslationIssueKind.LENGTH,
                "Longitud dudosa.",
                "Source.",
                "Resultado más largo.",
            ),
        ),
    )
    result = ProcessResult(
        Path("result.md"),
        review_markdown="Resultado más largo.\n",
        translation_quality_report=report,
    )

    assert recommend_targeted_review(result) is None


def test_repeated_pdf_quality_evidence_selects_only_its_pages() -> None:
    markdown = (
        "<!-- PZDOC PDF PAGE 1 -->\n\nPágina correcta.\n\n"
        "<!-- PZDOC PDF PAGE 2 -->\n\nTexto dudoso dos.\n\n"
        "<!-- PZDOC PDF PAGE 3 -->\n\nTexto dudoso tres.\n"
    )
    result = ProcessResult(
        Path("result.md"),
        review_markdown=markdown,
        pdf_quality_report=PdfQualityReport(
            (1, 2, 3),
            (),
            (
                PdfReviewIssue(2, "Revisar", ""),
                PdfReviewIssue(3, "Revisar", ""),
            ),
        ),
    )

    recommendation = recommend_targeted_review(result)

    assert recommendation is not None
    assert recommendation.signal_counts == ((ReviewSignal.CONVERSION_DAMAGE, 2),)
    assert recommendation.block_positions == (1, 2)


def test_completed_markdown_result_can_be_reopened_without_private_snapshot(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("Source", encoding="utf-8")
    output = tmp_path / "result.md"
    output.write_text("Resultado.\n", encoding="utf-8")
    job = DocumentJob.create(
        DocumentSource.inspect(source),
        JobConfiguration(output=OutputConfiguration(format=DocumentFormat.MARKDOWN)),
        order=0,
    )
    job = replace(job, result_path=output)

    result = reconstruct_completed_result(job)

    assert result.final_path == output
    assert result.review_markdown == "Resultado.\n"
    assert result.review_original_path == source


def test_review_scope_fingerprint_ignores_only_private_pdf_page_anchors() -> None:
    private = "<!-- PZDOC PDF PAGE 1 -->\n\n# Capítulo 1\n\nContenido.\n"
    public = "\n\n# Capítulo 1\n\nContenido.\n"

    assert review_scope_fingerprint(private) == review_scope_fingerprint(public)
    assert review_scope_fingerprint(public) != review_scope_fingerprint(
        "\n\n# Capítulo 2\n\nContenido.\n"
    )


def test_review_recommendation_rejects_an_unbounded_scope() -> None:
    with pytest.raises(ValueError, match="64"):
        ReviewRecommendation(
            ((ReviewSignal.CONVERSION_DAMAGE, 65),),
            tuple(range(65)),
        )


def test_epub_result_rebinds_targets_after_local_package_metadata_is_added(
    tmp_path: Path,
) -> None:
    markdown = "# Chapter\n\nword word word.\n"
    source = tmp_path / "source.md"
    source.write_text(markdown, encoding="utf-8")
    output = tmp_path / "result.epub"
    output.write_bytes(build_epub(markdown, (), EpubBookMetadata("Book", "en")).content)
    job = replace(
        DocumentJob.create(
            DocumentSource.inspect(source),
            JobConfiguration(output=OutputConfiguration(format=DocumentFormat.EPUB)),
            order=0,
        ),
        result_path=output,
    )
    recommendation = recommend_targeted_review(ProcessResult(output, review_markdown=markdown))
    restored = reconstruct_completed_result(job)

    assert recommendation is not None
    assert restored.review_markdown is not None
    rebound = rebind_review_recommendation(restored.review_markdown, recommendation)

    assert rebound is not None
    assert rebound.block_positions != recommendation.block_positions
    assert rebound.scope_fingerprint == review_scope_fingerprint(restored.review_markdown)
