from __future__ import annotations

from pathlib import Path
from zipfile import ZipInfo

import pytest
import scripts.validate_real_workflows as workflow_module
from scripts.validate_real_workflows import (
    SYNTHETIC_PAGE_COUNT,
    LiveWorkflowResult,
    _parse_glossary_arguments,
    _validate_generated_output,
    full_matrix_cases,
    run_live_workflows,
    select_live_settings,
    write_report,
    write_synthetic_pdf,
)

from parsezen.glossary import GlossaryEntry
from parsezen.local_models import OllamaConnection, OllamaModel, OllamaStatus
from parsezen.pdf_conversion import PdfPageRange, PdfQualityReport, convert_pdf
from parsezen.processing import (
    OutputFormat,
    ProcessResult,
    ProcessStage,
    ProcessTelemetry,
    StageTelemetry,
)
from parsezen.revision import RevisionKind, build_revision_draft
from parsezen.settings import AppSettings


def test_synthetic_live_sample_is_a_real_convertible_twenty_page_pdf(tmp_path: Path) -> None:
    source = tmp_path / "sample.pdf"
    reports: list[PdfQualityReport] = []

    write_synthetic_pdf(source)
    markdown = convert_pdf(source, on_quality_report=reports.append)

    assert reports[0].processed_pages == tuple(range(1, SYNTHETIC_PAGE_COUNT + 1))
    assert "# PRACTICAL GUIDE" in markdown
    assert "# FINAL CHECKS" in markdown
    assert "# PUBLICATION REVIEW" in markdown
    assert "original fragment must remain complete" in markdown


def test_live_settings_reject_reasoning_model_even_when_it_is_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = (
        OllamaModel("qwen3:4b", "Qwen3 4B"),
        OllamaModel("qwen3:4b-instruct", "Qwen3 4B Instruct"),
    )
    monkeypatch.setattr(workflow_module, "load_settings", lambda: AppSettings())
    monkeypatch.setattr(
        workflow_module,
        "discover_ollama",
        lambda _preferred: OllamaConnection(OllamaStatus.READY, models),
    )

    with pytest.raises(RuntimeError, match="Instruct"):
        select_live_settings("qwen3:4b")

    assert select_live_settings("qwen3:4b-instruct").model == "qwen3:4b-instruct"


def test_full_live_matrix_contains_each_combination_for_both_outputs() -> None:
    cases = full_matrix_cases()

    assert len(cases) == 16
    assert len({case.name for case in cases}) == 16
    assert {
        (case.output_format.value, case.translate, case.review_content, case.review_structure)
        for case in cases
    } == {
        (output, translate, content, structure)
        for output in ("markdown", "epub")
        for translate in (False, True)
        for content in (False, True)
        for structure in (False, True)
    }


def test_live_glossary_arguments_are_validated_without_entering_the_report() -> None:
    assert _parse_glossary_arguments(["Ancient Astrology=Astrología Antigua"]) == (
        GlossaryEntry("Ancient Astrology", "Astrología Antigua"),
    )
    with pytest.raises(ValueError, match="ORIGEN=DESTINO"):
        _parse_glossary_arguments(["incomplete"])


def test_live_report_contains_no_document_identity_or_text(tmp_path: Path) -> None:
    private_name = "secret-book.pdf"
    private_text = "A private paragraph that must never reach diagnostics."
    private_path = tmp_path / private_name
    result = LiveWorkflowResult(
        source_index=1,
        source_extension=".pdf",
        case="epub-completo-con-tres-mejoras",
        passed=False,
        elapsed_seconds=12.5,
        stages=("validating", "organizing_structure"),
        error_type="ImprovementError",
        failed_stage="organizing_structure",
    )
    report_path = tmp_path / "report.json"

    write_report(report_path, (result,), model="qwen3:4b")

    report = report_path.read_text(encoding="utf-8")
    assert "organizing_structure" in report
    assert "ImprovementError" in report
    assert private_name not in report
    assert str(private_path) not in report
    assert private_text not in report
    assert "prompts" in report


def test_live_epub_validation_reports_oversized_body_promoted_to_heading(
    tmp_path: Path,
) -> None:
    epub = tmp_path / "sample.epub"
    oversized = " ".join(f"word{index}" for index in range(50))
    with workflow_module.ZipFile(epub, "w") as archive:
        archive.writestr(
            ZipInfo("mimetype"),
            b"application/epub+zip",
            compress_type=workflow_module.ZIP_STORED,
        )
        archive.writestr("EPUB/package.opf", "<package/>")
        archive.writestr("EPUB/nav.xhtml", "<html/>")
        archive.writestr(
            "EPUB/chapter.xhtml",
            f'<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>{oversized}</h1></body></html>',
        )

    assert _validate_generated_output(epub, OutputFormat.EPUB) == 1


def test_live_epub_validation_reports_an_outline_that_starts_too_deep(
    tmp_path: Path,
) -> None:
    epub = tmp_path / "sample.epub"
    with workflow_module.ZipFile(epub, "w") as archive:
        archive.writestr(
            ZipInfo("mimetype"),
            b"application/epub+zip",
            compress_type=workflow_module.ZIP_STORED,
        )
        archive.writestr("EPUB/package.opf", "<package/>")
        archive.writestr("EPUB/nav.xhtml", "<html/>")
        archive.writestr(
            "EPUB/chapter.xhtml",
            '<html xmlns="http://www.w3.org/1999/xhtml"><body><h6>Chapter</h6></body></html>',
        )

    assert _validate_generated_output(epub, OutputFormat.EPUB) == 1


def test_live_workflow_forwards_pdf_range_and_records_diagnostic_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    captured_ranges: list[PdfPageRange | None] = []

    def process(request, *, on_stage, **_kwargs) -> ProcessResult:
        captured_ranges.append(request.pdf_page_range)
        on_stage(ProcessStage.COMPLETED)
        return ProcessResult(
            final_path=tmp_path / "sample.epub",
            pdf_quality_report=PdfQualityReport(tuple(range(1, 101)), (4, 9), ()),
            preserved_translation_chunks=(2,),
            preserved_images=7,
            epub_chapters=12,
            telemetry=ProcessTelemetry(
                1_500,
                (StageTelemetry(ProcessStage.TRANSLATING, 1_200, 1),),
            ),
            front_matter_blocks=3,
            toc_blocks=4,
            terminology_terms=5,
        )

    monkeypatch.setattr(workflow_module, "process_document", process)
    monkeypatch.setattr(workflow_module, "_validate_generated_output", lambda *_args: None)

    results = run_live_workflows(
        (source,),
        tmp_path / "outputs",
        AppSettings(model="qwen3:4b"),
        page_range=PdfPageRange(1, 100),
    )

    assert captured_ranges == [PdfPageRange(1, 100)]
    assert results[0].processed_pages == 100
    assert results[0].ocr_pages == 2
    assert results[0].revision_changes == 0
    assert results[0].preserved_translation_chunks == 1
    assert results[0].preserved_images == 7
    assert results[0].epub_chapters == 12
    assert results[0].stage_duration_ms == {"translating": 1_200}
    assert results[0].translation_issue_kinds == {}
    assert results[0].front_matter_blocks == 3
    assert results[0].toc_blocks == 4
    assert results[0].terminology_terms == 5
    assert results[0].passed
    assert not results[0].quality_gate_passed


def test_live_workflow_applies_only_recommended_revision_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    original = "Total 10.\n\nSin cambios.\n\nContiene un eror.\n"
    proposed = "Total 11.\n\nSin cambios.\n\nContiene un error.\n"
    draft = build_revision_draft(
        original,
        proposed,
        kinds=frozenset({RevisionKind.CONTENT}),
    )
    applied: list[str] = []

    def process(_request, *, on_stage, **_kwargs) -> ProcessResult:
        on_stage(ProcessStage.COMPLETED)
        return ProcessResult(
            final_path=tmp_path / "sample.epub",
            revision_draft=draft,
        )

    def apply_revision(result: ProcessResult, reviewed_text: str) -> ProcessResult:
        applied.append(reviewed_text)
        return result

    monkeypatch.setattr(workflow_module, "process_document", process)
    monkeypatch.setattr(workflow_module, "apply_reviewed_revision", apply_revision)
    monkeypatch.setattr(workflow_module, "_validate_generated_output", lambda *_args: None)

    results = run_live_workflows(
        (source,),
        tmp_path / "outputs",
        AppSettings(model="qwen3:4b"),
    )

    assert results[0].passed
    assert results[0].quality_gate_passed
    assert applied == ["Total 10.\n\nSin cambios.\n\nContiene un error.\n"]
    assert results[0].revision_changes == 2
    assert results[0].recommended_revision_changes == 1
    assert results[0].rejected_revision_changes == 1
