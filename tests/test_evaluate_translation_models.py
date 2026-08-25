from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts import evaluate_translation_models as evaluator

from parsezen.glossary import protect_glossary
from parsezen.local_models import OllamaConnection, OllamaModel, OllamaStatus
from parsezen.settings import AppSettings


@pytest.fixture
def installed_connection() -> OllamaConnection:
    return OllamaConnection(
        OllamaStatus.READY,
        models=(
            OllamaModel("alpha:1b", "Alpha", digest="a" * 64, recommended_context=4096),
            OllamaModel("beta:1b", "Beta", digest="b" * 64, recommended_context=8192),
        ),
    )


def _corpus_with_expected() -> tuple[evaluator.CorpusCase, ...]:
    return evaluator.load_corpus()


def test_compares_installed_tags_with_shared_context_and_repetitions(
    monkeypatch: pytest.MonkeyPatch,
    installed_connection: OllamaConnection,
) -> None:
    cases = _corpus_with_expected()
    calls: list[tuple[str, str, int | None, str]] = []

    def fake_improve(
        source: str,
        _mode: evaluator.ImprovementMode,
        settings: AppSettings,
        target_language: str,
        **_kwargs: object,
    ) -> str:
        calls.append((settings.model or "", source, settings.context_window, target_language))
        for case in cases:
            protected = protect_glossary(case.source, case.glossary)
            if source == protected.text:
                result = case.expected or case.source
                for marker, target in protected.replacements:
                    result = result.replace(target, marker, 1)
                return result
        raise AssertionError("unexpected synthetic case")

    monkeypatch.setattr(evaluator, "improve_markdown", fake_improve)
    report = evaluator.evaluate_models(
        ("alpha:1b", "beta:1b"),
        repetitions=2,
        context_window=4096,
        ollama_connection=installed_connection,
    )

    assert report["options"]["context_window"] == 4096
    assert report["options"]["repetitions"] == 2
    assert len(report["cases"]) == len(cases) * 2 * 2
    assert {call[0] for call in calls} == {"alpha:1b", "beta:1b"}
    assert {call[2] for call in calls} == {4096}
    assert {call[3] for call in calls} == {"Español"}
    assert all(record["metrics"]["quality_gate_passed"] for record in report["cases"])
    assert all(summary["exact_match_rate"] == 1.0 for summary in report["models"].values())
    assert {summary["ollama_digest"] for summary in report["models"].values()} == {
        "a" * 64,
        "b" * 64,
    }


def test_rejects_missing_and_cloud_tags_without_translation_calls(
    installed_connection: OllamaConnection,
) -> None:
    with pytest.raises(RuntimeError, match="instalados"):
        evaluator.evaluate_models(
            ("missing:1b",),
            ollama_connection=installed_connection,
        )
    with pytest.raises(RuntimeError, match="cloud"):
        evaluator.evaluate_models(
            ("alpha:1b-cloud",),
            ollama_connection=OllamaConnection(
                OllamaStatus.READY,
                models=(OllamaModel("alpha:1b-cloud", "Alpha Cloud", digest="c" * 64),),
            ),
        )


def test_default_context_is_order_independent_and_missing_digest_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    installed_connection: OllamaConnection,
) -> None:
    case = evaluator.CorpusCase("simple-v1", ("paragraph",), "A complete sentence.", None)
    monkeypatch.setattr(evaluator, "improve_markdown", lambda source, *_args, **_kwargs: source)

    first = evaluator.evaluate_models(
        ("alpha:1b", "beta:1b"),
        corpus=(case,),
        ollama_connection=installed_connection,
    )
    second = evaluator.evaluate_models(
        ("beta:1b", "alpha:1b"),
        corpus=(case,),
        ollama_connection=installed_connection,
    )
    assert first["options"]["context_window"] == evaluator.DEFAULT_CONTEXT_WINDOW
    assert second["options"]["context_window"] == evaluator.DEFAULT_CONTEXT_WINDOW

    with pytest.raises(RuntimeError, match="digest"):
        evaluator.evaluate_models(
            ("alpha:1b",),
            corpus=(case,),
            ollama_connection=OllamaConnection(
                OllamaStatus.READY,
                models=(OllamaModel("alpha:1b", "Alpha"),),
            ),
        )


def test_quality_metrics_capture_residual_and_number_gate(
    monkeypatch: pytest.MonkeyPatch,
    installed_connection: OllamaConnection,
) -> None:
    case = evaluator.CorpusCase(
        "numbers-v1",
        ("numbers",),
        "The report keeps 12 pages.",
        "El informe conserva 12 páginas.",
    )
    monkeypatch.setattr(evaluator, "improve_markdown", lambda source, *_args, **_kwargs: source)

    report = evaluator.evaluate_models(
        ("alpha:1b",),
        context_window=4096,
        corpus=(case,),
        ollama_connection=installed_connection,
    )

    metrics = report["cases"][0]["metrics"]
    assert metrics["passed"] is True
    assert metrics["quality_gate_passed"] is False
    assert metrics["residual_count"] >= 1
    assert metrics["numbers_conserved"] is True
    assert metrics["exact_match"] is False


def test_report_is_atomic_and_contains_no_corpus_or_destination_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    installed_connection: OllamaConnection,
) -> None:
    case = evaluator.CorpusCase(
        "private-looking-label-v1",
        ("paragraph",),
        "The public paragraph is complete.",
        "El párrafo público está completo.",
    )
    monkeypatch.setattr(evaluator, "improve_markdown", lambda *_args, **_kwargs: case.expected)
    report = evaluator.evaluate_models(
        ("alpha:1b",),
        context_window=4096,
        corpus=(case,),
        ollama_connection=installed_connection,
    )
    destination = tmp_path / "nested" / "evaluation.json"
    evaluator.write_report(destination, report)

    payload = json.loads(destination.read_text(encoding="utf-8"))
    serialized = destination.read_text(encoding="utf-8")
    assert payload["cases"][0]["case_id"].startswith("case-v1-")
    assert payload["cases"][0]["model"].startswith("model-v1-")
    assert "private-looking-label-v1" not in serialized
    assert "The public paragraph is complete." not in serialized
    assert "El párrafo público está completo." not in serialized
    assert "alpha:1b" not in serialized
    assert str(destination) not in serialized
    assert not list(destination.parent.glob("*.tmp"))
