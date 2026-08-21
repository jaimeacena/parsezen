from pathlib import Path

import parsezen.application.early_check as early_check_module
from parsezen.application.early_check import (
    representative_pdf_pages,
    run_early_check,
)
from parsezen.cancellation import CancellationToken
from parsezen.pdf_conversion import PdfPageRange, PdfQualityReport, PdfReviewIssue
from parsezen.processing import OutputFormat, ProcessRequest, ProcessResult
from parsezen.settings import AppSettings


def test_representative_pages_cover_the_selected_interval(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"local pdf identity")
    monkeypatch.setattr(
        early_check_module,
        "resolve_pdf_page_range",
        lambda _source, _requested: PdfPageRange(11, 110),
    )

    pages = representative_pdf_pages(
        ProcessRequest(
            source,
            convert_to_markdown=True,
            pdf_page_range=PdfPageRange(11, 110),
        )
    )

    assert pages == (11, 60, 110)


def test_representative_pages_prioritize_dense_visual_and_tabular_candidates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"local pdf identity")
    monkeypatch.setattr(
        early_check_module,
        "resolve_pdf_page_range",
        lambda _source, _requested: PdfPageRange(1, 100),
    )
    feature = early_check_module._PageFeatures
    monkeypatch.setattr(
        early_check_module,
        "_inspect_candidate_pages",
        lambda _source, _selected: (
            feature(1, 10, 0.0, False),
            feature(25, 900, 0.0, False),
            feature(50, 200, 0.0, True),
            feature(75, 50, 0.8, False),
            feature(100, 20, 0.0, False),
        ),
    )

    pages = representative_pdf_pages(ProcessRequest(source, convert_to_markdown=True))

    assert pages == (1, 25, 50, 75, 100)


def test_representative_pages_fill_five_spread_candidates_when_extrema_are_endpoints(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"local pdf identity")
    monkeypatch.setattr(
        early_check_module,
        "resolve_pdf_page_range",
        lambda _source, _requested: PdfPageRange(1, 100),
    )
    feature = early_check_module._PageFeatures
    monkeypatch.setattr(
        early_check_module,
        "_inspect_candidate_pages",
        lambda _source, _selected: tuple(
            feature(page, 900 if page == 50 else 100, 1.0, False)
            for page in (1, 13, 25, 38, 50, 63, 75, 88, 100)
        ),
    )

    pages = representative_pdf_pages(ProcessRequest(source, convert_to_markdown=True))

    assert pages == (1, 25, 50, 75, 100)
    assert len(pages) == len(set(pages)) == 5


def test_early_check_uses_temporary_outputs_and_reports_safe_progress(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"local pdf identity")
    monkeypatch.setattr(
        early_check_module,
        "representative_pdf_pages",
        lambda _request: (1, 50, 100),
    )
    sampled_ranges: list[PdfPageRange] = []
    progress: list[tuple[int, int]] = []

    def processor(request, **_arguments):
        assert request.output_directory != tmp_path
        assert request.output_directory is not None
        assert request.pdf_page_range is not None
        sampled_ranges.append(request.pdf_page_range)
        page = request.pdf_page_range.first_page
        return ProcessResult(
            request.output_directory / f"sample-{page}.md",
            pdf_quality_report=PdfQualityReport((page,), (), ()),
        )

    report = run_early_check(
        ProcessRequest(
            source,
            convert_to_markdown=True,
            output_directory=tmp_path,
            output_format=OutputFormat.MARKDOWN,
        ),
        AppSettings(checkpoint_retention_days=0),
        cancellation=CancellationToken(),
        work_checkpoint_root=tmp_path / "checkpoints",
        on_progress=lambda current, total: progress.append((current, total)),
        processor=processor,
    )

    assert sampled_ranges == [
        PdfPageRange(1, 1),
        PdfPageRange(50, 50),
        PdfPageRange(100, 100),
    ]
    assert progress == [(1, 3), (2, 3), (3, 3)]
    assert not report.blocking
    assert report.warning_pages == 0


def test_early_check_skips_final_review_and_limits_translation_to_three_diverse_pages(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"local pdf identity")
    monkeypatch.setattr(
        early_check_module,
        "representative_pdf_pages",
        lambda _request: (1, 50, 100, 150, 200),
    )
    sampled: list[tuple[int, str | None, OutputFormat, bool, bool]] = []

    def processor(request, **_arguments):
        assert request.pdf_page_range is not None
        page = request.pdf_page_range.first_page
        sampled.append(
            (
                page,
                request.offline_translation_language,
                request.output_format,
                request.review_content,
                request.review_structure,
            )
        )
        return ProcessResult(request.output_directory / f"sample-{page}.md")

    run_early_check(
        ProcessRequest(
            source,
            convert_to_markdown=False,
            output_format=OutputFormat.EPUB,
            offline_translation_language="es",
            review_content=True,
            review_structure=True,
            epub_first_page_cover=True,
        ),
        AppSettings(),
        cancellation=CancellationToken(),
        work_checkpoint_root=tmp_path / "checkpoints",
        processor=processor,
    )

    assert [page for page, language, *_rest in sampled if language is not None] == [1, 100, 200]
    assert all(output_format is OutputFormat.MARKDOWN for _, _, output_format, _, _ in sampled)
    assert all(not content and not structure for _, _, _, content, structure in sampled)


def test_preserved_visual_endpoints_warn_without_counting_as_material_failures(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"local pdf identity")
    monkeypatch.setattr(
        early_check_module,
        "representative_pdf_pages",
        lambda _request: (1, 50, 100),
    )

    def processor(request, **_arguments):
        assert request.output_directory is not None
        assert request.pdf_page_range is not None
        page = request.pdf_page_range.first_page
        endpoint = page in {1, 100}
        report = PdfQualityReport(
            (page,),
            (),
            (PdfReviewIssue(page, "ignored", "", blocking=True),) if endpoint else (),
            low_confidence_pages=(page,) if endpoint else (),
        )
        return ProcessResult(
            request.output_directory / f"sample-{page}.md",
            pdf_quality_report=report,
            preserved_images=1 if endpoint else 0,
        )

    report = run_early_check(
        ProcessRequest(
            source,
            convert_to_markdown=True,
            include_images=True,
            output_directory=tmp_path,
            output_format=OutputFormat.MARKDOWN,
        ),
        AppSettings(),
        cancellation=CancellationToken(),
        work_checkpoint_root=tmp_path / "checkpoints",
        processor=processor,
    )

    assert not report.blocking
    assert report.warning_pages == 2
    assert report.conversion_issues == 2
    assert report.low_confidence_pages == 2


def test_repeated_failing_interior_pages_still_block(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"local pdf identity")
    monkeypatch.setattr(
        early_check_module,
        "representative_pdf_pages",
        lambda _request: (1, 50, 100, 150, 200),
    )

    def processor(request, **_arguments):
        assert request.output_directory is not None
        assert request.pdf_page_range is not None
        page = request.pdf_page_range.first_page
        failing = page in {50, 150}
        report = PdfQualityReport(
            (page,),
            (),
            (),
            low_confidence_pages=(page,) if failing else (),
        )
        return ProcessResult(
            request.output_directory / f"sample-{page}.md",
            pdf_quality_report=report,
        )

    report = run_early_check(
        ProcessRequest(source, convert_to_markdown=True),
        AppSettings(),
        cancellation=CancellationToken(),
        work_checkpoint_root=tmp_path / "checkpoints",
        processor=processor,
    )

    assert report.blocking
    assert report.warning_pages == 2


def test_visual_endpoints_without_preserved_images_still_block(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"local pdf identity")
    monkeypatch.setattr(
        early_check_module,
        "representative_pdf_pages",
        lambda _request: (1, 50, 100),
    )

    def run_case(include_images: bool, preserved_images: int):
        def processor(request, **_arguments):
            assert request.output_directory is not None
            assert request.pdf_page_range is not None
            page = request.pdf_page_range.first_page
            endpoint = page in {1, 100}
            report = PdfQualityReport(
                (page,),
                (),
                (PdfReviewIssue(page, "ignored", "", blocking=True),) if endpoint else (),
                low_confidence_pages=(page,) if endpoint else (),
            )
            return ProcessResult(
                request.output_directory / f"sample-{page}.md",
                pdf_quality_report=report,
                preserved_images=preserved_images if endpoint else 0,
            )

        return run_early_check(
            ProcessRequest(
                source,
                convert_to_markdown=True,
                include_images=include_images,
                output_directory=tmp_path,
                output_format=OutputFormat.MARKDOWN,
            ),
            AppSettings(),
            cancellation=CancellationToken(),
            work_checkpoint_root=tmp_path / "checkpoints",
            processor=processor,
        )

    for include_images, preserved_images in ((False, 1), (True, 0)):
        report = run_case(include_images, preserved_images)
        assert report.blocking
        assert report.warning_pages == 2


def test_early_check_stops_before_a_long_run_when_multiple_pages_are_doubtful(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"local pdf identity")
    monkeypatch.setattr(
        early_check_module,
        "representative_pdf_pages",
        lambda _request: (1, 50, 100),
    )

    def processor(request, **_arguments):
        assert request.output_directory is not None
        assert request.pdf_page_range is not None
        page = request.pdf_page_range.first_page
        return ProcessResult(
            request.output_directory / f"sample-{page}.md",
            pdf_quality_report=PdfQualityReport(
                (page,),
                (),
                (),
                low_confidence_pages=(page,),
            ),
        )

    report = run_early_check(
        ProcessRequest(source, convert_to_markdown=True),
        AppSettings(),
        cancellation=CancellationToken(),
        work_checkpoint_root=tmp_path / "checkpoints",
        processor=processor,
    )

    assert report.blocking
    assert report.warning_pages == 3
    assert "OCR" in report.blocking_reasons[0]
