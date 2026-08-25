from __future__ import annotations

import json
from pathlib import Path

import pytest
import scripts.evaluate_review_models as evaluation

from parsezen.improvement import ImprovementMode
from parsezen.processing_metrics import (
    record_local_ai_request,
    record_retry,
    record_validation_rejection,
)

_TINY_DIGEST = "a" * 64
_WIDE_DIGEST = "b" * 64


def _corpus() -> tuple[evaluation.ReviewCase, ...]:
    return (
        evaluation.ReviewCase("test-v1-clean", "clean", "Texto estable.", "Texto estable."),
        evaluation.ReviewCase("test-v1-repair", "repair", "Texto  roto.", "Texto roto."),
    )


def _bilingual_corpus() -> tuple[evaluation.TranslationReviewCase, ...]:
    return (
        evaluation.TranslationReviewCase(
            "test-v1-bilingual-clean",
            "clean",
            "The local process is ready.",
            "El proceso local está listo.",
            "El proceso local está listo.",
        ),
        evaluation.TranslationReviewCase(
            "test-v1-bilingual-repair",
            "repair",
            "The archive is ready.",
            "El archivo está cerrado.",
            "El archivo está listo.",
        ),
    )


def _fake_translation_review(
    source: str,
    translated: str,
    settings: object,
    target_language: str,
) -> str:
    del settings, target_language
    return next(
        case.expected_markdown
        for case in _bilingual_corpus()
        if case.source_markdown == source and case.translated_markdown == translated
    )


def test_evaluation_uses_production_reviewer_with_identical_options_and_repetitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ImprovementMode, object]] = []
    bilingual_calls: list[tuple[str, str, object, str]] = []

    def fake_improve(markdown: str, mode: ImprovementMode, settings: object) -> str:
        calls.append((markdown, mode, settings))
        return next(case.expected_markdown for case in _corpus() if case.input_markdown == markdown)

    def fake_bilingual(
        source: str,
        translated: str,
        settings: object,
        target_language: str,
    ) -> str:
        bilingual_calls.append((source, translated, settings, target_language))
        return _fake_translation_review(source, translated, settings, target_language)

    monkeypatch.setattr(evaluation, "improve_markdown", fake_improve)
    report = evaluation.evaluate_review_models(
        ("tiny:1b", "wide:3b"),
        context_window=8_192,
        timeout_seconds=45,
        repetitions=2,
        corpus=_corpus(),
        bilingual_corpus=_bilingual_corpus(),
        installed_models={"tiny:1b": _TINY_DIGEST, "wide:3b": _WIDE_DIGEST},
        runner=fake_improve,
        translation_runner=fake_bilingual,
    )

    assert len(calls) == 8
    assert len(bilingual_calls) == 8
    assert all(mode is ImprovementMode.REVIEW_CONTENT for _markdown, mode, _settings in calls)
    assert {settings.model for _markdown, _mode, settings in calls} == {"tiny:1b", "wide:3b"}
    assert {settings.context_window for _markdown, _mode, settings in calls} == {8_192}
    assert {settings.timeout_seconds for _markdown, _mode, settings in calls} == {45.0}
    for model in report["models"]:
        assert model["metrics"]["runs"] == 4
        assert model["metrics"]["exact_matches"] == 4
        assert model["metrics"]["true_positive"] == 2
        assert model["metrics"]["true_negative"] == 2
        assert model["metrics"]["false_positive"] == 0
        assert model["metrics"]["false_negative"] == 0
        assert model["metrics"]["precision"] == 1.0
        assert model["metrics"]["recall"] == 1.0
    bilingual_options = report["bilingual_review"]["options"]
    assert bilingual_options["mode"] == "review_translation"
    assert bilingual_options["source_language"] == "en"
    assert bilingual_options["target_language"] == "es"
    assert bilingual_options["context_window"] == 8_192
    assert bilingual_options["timeout_seconds"] == 45.0
    assert bilingual_options["repetitions"] == 2
    for source, translated, settings, target_language in bilingual_calls:
        assert source and translated
        assert target_language == "Español"
        assert settings.model in {"tiny:1b", "wide:3b"}
        assert settings.context_window == 8_192
        assert settings.timeout_seconds == 45.0
    for model in report["bilingual_review"]["models"]:
        assert model["metrics"]["runs"] == 4
        assert model["metrics"]["exact_matches"] == 4
        assert model["metrics"]["true_positive"] == 2
        assert model["metrics"]["true_negative"] == 2
        assert model["metrics"]["false_positive"] == 0
        assert model["metrics"]["false_negative"] == 0
        assert model["metrics"]["precision"] == 1.0
        assert model["metrics"]["recall"] == 1.0


def test_evaluation_discovers_and_accepts_only_already_installed_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovered: list[str] = []

    class Installed:
        model_id = "installed:1b"
        digest = _TINY_DIGEST

    def fake_tags() -> tuple[Installed, ...]:
        discovered.append("tags")
        return (Installed(),)

    monkeypatch.setattr(evaluation, "list_ollama_models", fake_tags)
    monkeypatch.setattr(evaluation, "improve_markdown", lambda markdown, mode, settings: markdown)

    report = evaluation.evaluate_review_models(
        ("installed:1b",),
        corpus=_corpus()[:1],
        translation_runner=_fake_translation_review,
    )

    assert discovered == ["tags"]
    assert report["models"][0]["model_sha256"] == evaluation._sha256(
        f"installed:1b\0{_TINY_DIGEST}"
    )
    assert report["models"][0]["ollama_digest"] == _TINY_DIGEST

    with pytest.raises(ValueError, match="instalados"):
        evaluation.evaluate_review_models(
            ("not-installed:1b",),
            corpus=_corpus()[:1],
            translation_runner=_fake_translation_review,
        )


def test_bilingual_evaluation_defaults_to_production_review_translation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, object, str]] = []

    def fake_review_translation(
        source: str,
        translated: str,
        settings: object,
        target_language: str,
    ) -> str:
        calls.append((source, translated, settings, target_language))
        return translated

    monkeypatch.setattr(evaluation, "review_translation_markdown", fake_review_translation)
    evaluation.evaluate_review_models(
        ("tiny:1b",),
        corpus=_corpus()[:1],
        bilingual_corpus=_bilingual_corpus()[:1],
        installed_models={"tiny:1b": _TINY_DIGEST},
        runner=lambda markdown, mode, settings: markdown,
    )

    assert len(calls) == 1
    assert calls[0][3] == "Español"


def test_evaluation_captures_guard_and_token_telemetry_without_document_content() -> None:
    def fake_improve(markdown: str, mode: ImprovementMode, settings: object) -> str:
        record_local_ai_request(
            input_characters=len(markdown),
            prompt_tokens=7,
            output_tokens=3,
            wall_duration_ms=11,
            ollama_total_duration_ms=12,
            ollama_load_duration_ms=1,
            operation="review_content",
        )
        record_retry()
        record_validation_rejection()
        return markdown

    report = evaluation.evaluate_review_models(
        ("tiny:1b",),
        repetitions=2,
        corpus=_corpus()[:1],
        installed_models={"tiny:1b": _TINY_DIGEST},
        runner=fake_improve,
        translation_runner=_fake_translation_review,
    )
    metrics = report["models"][0]["metrics"]

    assert metrics["local_ai_requests"] == 2
    assert metrics["prompt_tokens"] == 14
    assert metrics["output_tokens"] == 6
    assert metrics["model_wall_duration_ms"] == 22
    assert metrics["guard_rejections"] == 2
    assert metrics["retries"] == 2


def test_precision_metrics_distinguish_false_positive_and_false_negative() -> None:
    cases = _corpus()

    def wrong_reviewer(markdown: str, mode: ImprovementMode, settings: object) -> str:
        return cases[1].expected_markdown if markdown == cases[0].input_markdown else markdown

    report = evaluation.evaluate_review_models(
        ("tiny:1b",),
        corpus=cases,
        installed_models={"tiny:1b": _TINY_DIGEST},
        runner=wrong_reviewer,
        translation_runner=_fake_translation_review,
    )
    metrics = report["models"][0]["metrics"]

    assert metrics["true_positive"] == 0
    assert metrics["true_negative"] == 0
    assert metrics["false_positive"] == 1
    assert metrics["false_negative"] == 1
    assert metrics["precision"] == 0.0
    assert metrics["recall"] == 0.0


def test_bilingual_precision_uses_semantic_expected_text() -> None:
    cases = _bilingual_corpus()

    def wrong_bilingual(
        source: str,
        translated: str,
        settings: object,
        target_language: str,
    ) -> str:
        del settings, target_language
        if source == cases[0].source_markdown:
            return "El proceso remoto está listo."
        return translated

    report = evaluation.evaluate_review_models(
        ("tiny:1b",),
        corpus=_corpus()[:1],
        bilingual_corpus=cases,
        installed_models={"tiny:1b": _TINY_DIGEST},
        runner=lambda markdown, mode, settings: markdown,
        translation_runner=wrong_bilingual,
    )
    metrics = report["bilingual_review"]["models"][0]["metrics"]

    assert metrics["exact_matches"] == 0
    assert metrics["true_positive"] == 0
    assert metrics["true_negative"] == 0
    assert metrics["false_positive"] == 1
    assert metrics["false_negative"] == 1
    assert metrics["precision"] == 0.0
    assert metrics["recall"] == 0.0


def test_report_is_atomic_and_contains_only_hashes_ids_labels_and_metrics(
    tmp_path: Path,
) -> None:
    case = _corpus()[0]
    report = evaluation.evaluate_review_models(
        ("tiny:1b",),
        corpus=(case,),
        installed_models={"tiny:1b": _TINY_DIGEST},
        runner=lambda markdown, mode, settings: markdown,
        translation_runner=_fake_translation_review,
    )
    destination = tmp_path / "nested" / "report.json"
    evaluation.write_report(destination, report)
    raw = destination.read_text(encoding="utf-8")
    payload = json.loads(raw)

    assert payload["schema_version"] == evaluation.REPORT_SCHEMA_VERSION
    assert payload["corpus_revision"] == evaluation.CORPUS_REVISION
    assert case.input_markdown not in raw
    assert case.expected_markdown not in raw
    assert case.case_id not in raw
    assert "tiny:1b" not in raw
    assert str(tmp_path) not in raw
    for bilingual_case in evaluation.BILINGUAL_CORPUS:
        assert bilingual_case.source_markdown not in raw
        assert bilingual_case.translated_markdown not in raw
        assert bilingual_case.expected_markdown not in raw
        assert bilingual_case.case_id not in raw
    assert payload["models"][0]["cases"][0]["output_sha256"] == [
        evaluation._sha256(case.input_markdown)
    ]
