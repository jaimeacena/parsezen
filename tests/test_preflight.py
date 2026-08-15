from datetime import UTC, datetime
from pathlib import Path

from parsezen.application.preflight import (
    PreflightSeverity,
    analyze_preflight,
    combine_preflights,
    estimate_duration,
    estimate_remaining_time,
    format_duration_range,
    processing_metric,
    should_run_early_check,
)
from parsezen.domain.estimates import DurationEstimate, ProcessingMetric, WorkloadProfile
from parsezen.domain.jobs import (
    DocumentFormat,
    DocumentJob,
    DocumentSource,
    JobConfiguration,
    OutputConfiguration,
    ProcessingPlan,
    TranslationConfiguration,
    TranslationMethod,
)
from parsezen.processing import ProcessRequest, ProcessTelemetry
from parsezen.settings import AppSettings
from parsezen.workflow import OutputFormat


def _profile(*, units: int = 20) -> WorkloadProfile:
    return WorkloadProfile(
        source_format=DocumentFormat.MARKDOWN,
        output_format=DocumentFormat.EPUB,
        work_units=units,
        force_pdf_ocr=False,
        translation_method=TranslationMethod.LOCAL_AI,
        refinement_enabled=True,
        structure_enabled=False,
        include_images=True,
        preserve_styles=True,
        model_key="private-model-key",
    )


def test_duration_estimate_learns_from_matching_local_runs() -> None:
    now = datetime(2026, 7, 30, tzinfo=UTC)
    profile = _profile()
    metrics = tuple(
        ProcessingMetric(
            _profile(units=units),
            duration_ms=units * rate * 1_000,
            created_at=now,
        )
        for units, rate in ((10, 8), (20, 10), (30, 12), (40, 11))
    )

    estimate = estimate_duration(profile, metrics)

    assert estimate.sample_count == 4
    assert estimate.lower_seconds < estimate.likely_seconds < estimate.upper_seconds
    assert estimate.confidence_label == "Basada en trabajos similares"
    assert "–" in format_duration_range(estimate)


def test_preflight_explains_review_load_and_unsafe_automatic_changes(tmp_path: Path) -> None:
    source = tmp_path / "chapter.md"
    source.write_text("# Chapter\n\nText", encoding="utf-8")
    job = DocumentJob.create(
        DocumentSource.inspect(source),
        JobConfiguration(
            output=OutputConfiguration(format=DocumentFormat.EPUB),
            translation=TranslationConfiguration(
                enabled=True,
                method=TranslationMethod.OFFLINE,
                target_language="es",
            ),
            plan=ProcessingPlan.LOCAL_AI_REVIEWED,
        ),
        order=0,
        job_id="book",
    )
    request = ProcessRequest(
        source,
        convert_to_markdown=False,
        output_format=OutputFormat.EPUB,
        offline_translation_language="es",
    )

    analysis, profile = analyze_preflight(
        job,
        request,
        AppSettings(model="qwen3:4b-instruct"),
    )

    assert profile.model_key is not None
    assert all(source.name not in finding.detail for finding in analysis.findings)
    assert analysis.severity is PreflightSeverity.INFO
    assert any(finding.code == "two-language-passes" for finding in analysis.findings)
    assert analysis.flow_steps == (
        "Markdown",
        "Traducir con Argos a español",
        "Revisión bilingüe con IA local",
        "Revisión de estructura con IA local",
        "EPUB",
    )
    assert any(
        finding.code == "processing-passes" and "3 pasadas" in finding.detail
        for finding in analysis.findings
    )
    assert "texto revisado por IA local" in analysis.expected_reviews
    assert "confirmación EPUB final" in analysis.expected_reviews
    assert combine_preflights((analysis,)).documents == (analysis,)


def test_zero_duration_is_not_saved_as_learning_history() -> None:
    assert (
        processing_metric(
            _profile(),
            ProcessTelemetry(total_duration_ms=0, stages=()),
            created_at=datetime.now(UTC),
        )
        is None
    )


def test_long_uncertain_pdf_gets_an_automatic_representative_check() -> None:
    profile = WorkloadProfile(
        source_format=DocumentFormat.PDF,
        output_format=DocumentFormat.EPUB,
        work_units=60,
        force_pdf_ocr=False,
        translation_method=TranslationMethod.LOCAL_AI,
        refinement_enabled=True,
        structure_enabled=True,
        include_images=True,
        preserve_styles=True,
        model_key="local-model",
    )

    assert should_run_early_check(profile)
    assert not should_run_early_check(_profile(units=400))


def test_remaining_time_explains_when_real_progress_changes_the_range() -> None:
    estimate = estimate_remaining_time(
        DurationEstimate(600, 900, 1_200, 4),
        elapsed_seconds=300,
        stage_elapsed_seconds=240,
        progress_current=2,
        progress_total=10,
    )

    assert estimate.label.startswith("Quedan aprox.")
    assert "avance real" in estimate.explanation
    assert estimate.estimate is not None
    assert estimate.estimate.upper_seconds >= 1_200 - 300


def test_remaining_time_reports_an_overrun_without_fake_precision() -> None:
    estimate = estimate_remaining_time(
        DurationEstimate(60, 90, 120),
        elapsed_seconds=121,
    )

    assert estimate.label == "Más tiempo del previsto"
    assert estimate.overdue
    assert estimate.estimate is None
