from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipInfo

import pytest
import scripts.validate_real_workflows as workflow_module
from scripts.validate_real_workflows import (
    SYNTHETIC_PAGE_COUNT,
    LiveWorkflowResult,
    LocalEvaluationRun,
    _parse_glossary_arguments,
    _validate_generated_output,
    full_matrix_cases,
    run_live_workflows,
    run_local_model_evaluation,
    select_live_settings,
    write_evaluation_report,
    write_report,
    write_synthetic_pdf,
)

from parsezen.glossary import GlossaryEntry
from parsezen.improvement import ImprovementMode
from parsezen.local_models import OllamaConnection, OllamaModel, OllamaStatus
from parsezen.pdf_conversion import PdfPageRange, PdfQualityReport, PdfReviewIssue, convert_pdf
from parsezen.processing import (
    OutputFormat,
    ProcessResult,
    ProcessStage,
    ProcessTelemetry,
    StageTelemetry,
)
from parsezen.processing_metrics import AiOperationTelemetry, BatchTelemetry
from parsezen.revision import RevisionKind, build_revision_draft
from parsezen.settings import AppSettings
from parsezen.translation_quality import (
    TranslationIssueKind,
    TranslationQualityIssue,
    TranslationQualityReport,
)


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


def test_review_profiles_compare_direct_and_reviewed_routes_with_explicit_pass_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-1.4\n")

    def process(request, *, on_stage, **_kwargs) -> ProcessResult:
        on_stage(ProcessStage.COMPLETED)
        return ProcessResult(final_path=tmp_path / f"result-{request.output_format.value}.md")

    monkeypatch.setattr(workflow_module, "process_document", process)
    monkeypatch.setattr(workflow_module, "_validate_generated_output", lambda *_args: 0)

    review = run_live_workflows(
        (source,),
        tmp_path / "review",
        AppSettings(model="qwen3:4b-instruct"),
        workflow_profile="review",
    )
    translated = run_live_workflows(
        (source,),
        tmp_path / "translation-review",
        AppSettings(model="qwen3:4b-instruct"),
        translation_engine="argos",
        workflow_profile="translation-review",
    )

    assert [(item.planned_text_passes, item.planned_ai_passes) for item in review] == [
        (0, 0),
        (1, 1),
    ]
    assert [(item.planned_text_passes, item.planned_ai_passes) for item in translated] == [
        (1, 0),
        (3, 2),
    ]


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
    assert not (tmp_path / ".report.json.tmp").exists()


def test_live_epub_validation_reports_oversized_body_promoted_to_heading(
    tmp_path: Path,
) -> None:
    epub = tmp_path / "sample.epub"
    oversized = " ".join(f"word{index}" for index in range(50))
    _write_validation_epub(
        epub,
        f'<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>{oversized}</h1></body></html>',
    )

    assert _validate_generated_output(epub, OutputFormat.EPUB) == 1


def test_live_epub_validation_reports_an_outline_that_starts_too_deep(
    tmp_path: Path,
) -> None:
    epub = tmp_path / "sample.epub"
    _write_validation_epub(
        epub,
        '<html xmlns="http://www.w3.org/1999/xhtml"><body><h6>Chapter</h6></body></html>',
    )

    assert _validate_generated_output(epub, OutputFormat.EPUB) == 1


def test_live_quality_gate_allows_a_reviewed_nonblocking_pdf_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-1.4\n")

    def process(_request, *, on_stage, **_kwargs) -> ProcessResult:
        on_stage(ProcessStage.COMPLETED)
        return ProcessResult(
            final_path=tmp_path / "sample.epub",
            pdf_quality_report=PdfQualityReport(
                (1,),
                (),
                (PdfReviewIssue(1, "Review the illustration.", "", blocking=False),),
            ),
        )

    monkeypatch.setattr(workflow_module, "process_document", process)
    monkeypatch.setattr(workflow_module, "_validate_generated_output", lambda *_args: 0)

    result = run_live_workflows(
        (source,),
        tmp_path / "outputs",
        AppSettings(model="qwen3:4b"),
    )[0]

    assert result.pdf_issues == 1
    assert result.blocking_pdf_issues == 0
    assert result.quality_gate_passed


def _write_validation_epub(destination: Path, chapter: str) -> None:
    container = """<?xml version="1.0" encoding="UTF-8"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles><rootfile full-path="OPS/content.opf"/></rootfiles>
</container>
"""
    package = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Sample</dc:title><dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="chapter"/></spine>
</package>
"""
    navigation = """<html xmlns="http://www.w3.org/1999/xhtml"
 xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>
 <li><a href="chapter.xhtml">Chapter</a></li></ol></nav></body></html>"""
    with workflow_module.ZipFile(destination, "w") as archive:
        archive.writestr(
            ZipInfo("mimetype"),
            b"application/epub+zip",
            compress_type=workflow_module.ZIP_STORED,
        )
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OPS/content.opf", package)
        archive.writestr("OPS/nav.xhtml", navigation)
        archive.writestr("OPS/chapter.xhtml", chapter)


def test_live_workflow_forwards_pdf_range_and_records_diagnostic_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    captured_ranges: list[PdfPageRange | None] = []
    captured_translation_modes: list[tuple[object, str | None]] = []

    def process(request, *, on_stage, **_kwargs) -> ProcessResult:
        captured_ranges.append(request.pdf_page_range)
        captured_translation_modes.append(
            (request.improvement_mode, request.offline_translation_language)
        )
        on_stage(ProcessStage.COMPLETED)
        return ProcessResult(
            final_path=tmp_path / "sample.epub",
            pdf_quality_report=PdfQualityReport(tuple(range(1, 101)), (4, 9), ()),
            translation_quality_report=TranslationQualityReport(
                "en",
                "es",
                "es",
                100,
                1_000,
                1_100,
                25,
                (
                    TranslationQualityIssue(
                        1,
                        TranslationIssueKind.LENGTH,
                        "Revisar longitud.",
                        "Original.",
                        "Traducción.",
                    ),
                ),
                (
                    (TranslationIssueKind.LENGTH, 23),
                    (TranslationIssueKind.SOURCE_TEXT, 2),
                ),
            ),
            preserved_translation_chunks=(2,),
            preserved_images=7,
            epub_chapters=12,
            telemetry=ProcessTelemetry(
                1_500,
                (StageTelemetry(ProcessStage.TRANSLATING, 1_200, 1),),
                BatchTelemetry(
                    local_ai_requests=2,
                    prompt_tokens=500,
                    output_tokens=100,
                    wall_duration_ms=2_000,
                    operations=(AiOperationTelemetry("translation_batch", 2, 500, 100, 2_000),),
                ),
            ),
            front_matter_blocks=3,
            toc_blocks=4,
            terminology_terms=5,
        )

    monkeypatch.setattr(workflow_module, "process_document", process)
    monkeypatch.setattr(workflow_module, "_validate_generated_output", lambda *_args: None)

    snapshots: list[tuple[LiveWorkflowResult, ...]] = []
    results = run_live_workflows(
        (source,),
        tmp_path / "outputs",
        AppSettings(model="qwen3:4b"),
        page_range=PdfPageRange(1, 100),
        on_results=snapshots.append,
    )

    assert captured_ranges == [PdfPageRange(1, 100)]
    assert captured_translation_modes == [(ImprovementMode.TRANSLATE, None)]
    assert results[0].translation_engine == "local_ai"
    assert results[0].processed_pages == 100
    assert results[0].ocr_pages == 2
    assert results[0].revision_changes == 0
    assert results[0].preserved_translation_chunks == 1
    assert results[0].preserved_translation_chunk_numbers == (2,)
    assert results[0].preserved_images == 7
    assert results[0].epub_chapters == 12
    assert results[0].stage_duration_ms == {"translating": 1_200}
    assert results[0].translation_issues == 25
    assert results[0].translation_issue_kinds == {"length": 23, "source_text": 2}
    assert results[0].front_matter_blocks == 3
    assert results[0].toc_blocks == 4
    assert results[0].terminology_terms == 5
    assert results[0].ai_operations == {
        "translation_batch": {
            "requests": 2,
            "prompt_tokens": 500,
            "output_tokens": 100,
            "wall_duration_ms": 2_000,
        }
    }
    assert results[0].passed
    assert not results[0].quality_gate_passed
    assert snapshots == [results]


def test_live_workflow_can_use_local_ai_for_translation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    captured_modes: list[tuple[object, str | None, str | None, bool, bool]] = []
    captured_settings: list[AppSettings | None] = []

    def process(request, *, on_stage, settings, **_kwargs) -> ProcessResult:
        captured_modes.append(
            (
                request.improvement_mode,
                request.target_language,
                request.offline_translation_language,
                request.review_content,
                request.review_structure,
            )
        )
        captured_settings.append(settings)
        on_stage(ProcessStage.COMPLETED)
        return ProcessResult(final_path=tmp_path / "sample.epub")

    monkeypatch.setattr(workflow_module, "process_document", process)
    monkeypatch.setattr(workflow_module, "_validate_generated_output", lambda *_args: 0)

    results = run_live_workflows(
        (source,),
        tmp_path / "outputs",
        AppSettings(model="qwen3:4b"),
        translation_engine="local_ai",
        workflow_profile="translation",
    )

    assert captured_modes == [(ImprovementMode.TRANSLATE, "Español", None, False, False)]
    assert captured_settings == [AppSettings(model="qwen3:4b")]
    assert results[0].case == "epub-solo-traduccion"
    assert results[0].translation_engine == "local_ai"
    assert results[0].passed


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
    assert results[0].revision_changes_by_kind == {"content": 2}
    assert results[0].recommended_revision_changes_by_kind == {"content": 1}


def test_local_evaluation_repeats_explicit_installed_tags_with_shared_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    models = (
        OllamaModel("alpha:4b", "Alpha", recommended_context=4096),
        OllamaModel("beta:4b", "Beta", recommended_context=8192),
    )
    monkeypatch.setattr(
        workflow_module,
        "discover_ollama",
        lambda _preferred: OllamaConnection(OllamaStatus.READY, models),
    )
    selected: list[tuple[str, int | None]] = []

    def select(model: str, *, context_window: int | None = None) -> AppSettings:
        selected.append((model, context_window))
        return AppSettings(model=model, context_window=context_window or 4096)

    monkeypatch.setattr(workflow_module, "select_live_settings", select)
    calls: list[tuple[str, int, Path, int]] = []

    def run(
        _sources: tuple[Path, ...],
        output_root: Path,
        settings: AppSettings,
        **_kwargs: object,
    ) -> tuple[LiveWorkflowResult, ...]:
        calls.append((settings.model or "", settings.context_window, output_root, len(calls)))
        return (
            LiveWorkflowResult(
                source_index=1,
                source_extension=".pdf",
                case="case",
                passed=True,
                elapsed_seconds=0.1,
                stages=("completed",),
                quality_gate_passed=True,
                output_sha256=("a" if settings.model == "alpha:4b" else "b") * 64,
            ),
        )

    monkeypatch.setattr(workflow_module, "run_live_workflows", run)

    runs = run_local_model_evaluation(
        (source,),
        tmp_path / "outputs",
        ("alpha:4b", "beta:4b"),
        repetitions=2,
        context_window=8192,
    )

    assert len(runs) == 4
    assert [(run.model, run.context_window, run.repetition) for run in runs] == [
        ("alpha:4b", 8192, 1),
        ("alpha:4b", 8192, 2),
        ("beta:4b", 8192, 1),
        ("beta:4b", 8192, 2),
    ]
    assert selected == [("alpha:4b", 8192), ("beta:4b", 8192)]
    assert len({call[2] for call in calls}) == 4


def test_local_evaluation_report_is_paired_atomic_and_content_free(tmp_path: Path) -> None:
    private_path = tmp_path / "private-title.pdf"
    private_text = "private document text"
    result_a = LiveWorkflowResult(
        source_index=1,
        source_extension=".pdf",
        case="epub-solo-traduccion",
        passed=True,
        elapsed_seconds=0.1,
        stages=("completed",),
        quality_gate_passed=True,
        output_sha256="a" * 64,
    )
    result_b = LiveWorkflowResult(
        source_index=1,
        source_extension=".pdf",
        case="epub-solo-traduccion",
        passed=True,
        elapsed_seconds=0.1,
        stages=("completed",),
        quality_gate_passed=True,
        output_sha256="b" * 64,
    )
    runs = (
        LocalEvaluationRun("opaque-case", "alpha:4b", 8192, 1, result_a),
        LocalEvaluationRun("opaque-case", "beta:4b", 8192, 1, result_b),
    )
    report_path = tmp_path / "evaluation.json"

    write_evaluation_report(
        report_path,
        runs,
        evaluation_id="opaque-evaluation",
        options={"context_window": 8192, "repetitions": 1, "prompt": private_text},
    )

    report = report_path.read_text(encoding="utf-8")
    assert '"model": "alpha:4b"' in report
    assert '"context_window": 8192' in report
    assert '"repetition": 1' in report
    assert "a" * 64 in report and "b" * 64 in report
    assert '"same_output": false' in report
    assert '"stable_output": false' in report
    assert private_path.name not in report
    assert str(private_path) not in report
    assert private_text not in report
    assert "epub-solo-traduccion" not in report
    payload = json.loads(report)
    assert all(
        key not in run["metrics"]
        for run in payload["runs"]
        for key in ("source_index", "source_extension", "case")
    )
    assert not (tmp_path / ".evaluation.json.tmp").exists()


def test_local_evaluation_paired_summary_accepts_identical_output_hashes() -> None:
    result = LiveWorkflowResult(
        source_index=1,
        source_extension=".pdf",
        case="epub-solo-traduccion",
        passed=True,
        elapsed_seconds=0.1,
        stages=("completed",),
        quality_gate_passed=True,
        output_sha256="a" * 64,
    )
    runs = (
        LocalEvaluationRun("opaque-case", "alpha:4b", 8192, 1, result),
        LocalEvaluationRun("opaque-case", "beta:4b", 8192, 1, result),
    )

    summary = workflow_module._paired_summary(runs)

    assert summary[0]["same_output"] is True


def test_local_evaluation_repeat_summary_requires_two_matching_outputs(tmp_path: Path) -> None:
    result = LiveWorkflowResult(
        source_index=1,
        source_extension=".pdf",
        case="epub-solo-traduccion",
        passed=True,
        elapsed_seconds=0.1,
        stages=("completed",),
        quality_gate_passed=True,
        output_sha256="a" * 64,
    )
    runs = (
        LocalEvaluationRun("opaque-case", "alpha:4b", 8192, 1, result),
        LocalEvaluationRun("opaque-case", "alpha:4b", 8192, 2, result),
    )

    summary = workflow_module._repeat_summary(runs)

    assert summary[0]["stable_output"] is True
