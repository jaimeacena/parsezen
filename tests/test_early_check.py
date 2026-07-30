from pathlib import Path

import parsezen.application.early_check as early_check_module
from parsezen.application.early_check import (
    representative_pdf_pages,
    run_early_check,
)
from parsezen.cancellation import CancellationToken
from parsezen.pdf_conversion import PdfPageRange, PdfQualityReport
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
