"""Opt-in, content-free precision evaluation for the local Markdown reviewer.

The corpus is intentionally small, synthetic and versioned.  This script only
discovers models through Ollama's native ``/api/tags`` endpoint and evaluates
the explicitly requested installed tags.  It never pulls, installs or deletes
a model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkstemp
from time import monotonic
from typing import Any

from parsezen.improvement import ImprovementMode, improve_markdown, review_translation_markdown
from parsezen.local_models import (
    DEFAULT_CONTEXT_WINDOW,
    list_ollama_models,
    validate_ollama_model_id,
)
from parsezen.processing_metrics import BatchTelemetry, capture_batch_telemetry
from parsezen.settings import MAX_CONTEXT_WINDOW, MIN_CONTEXT_WINDOW, AppSettings

CORPUS_REVISION = "review-precision-synthetic-v1"
BILINGUAL_CORPUS_REVISION = "bilingual-review-precision-synthetic-v1"
BILINGUAL_SOURCE_LANGUAGE = "en"
BILINGUAL_TARGET_LANGUAGE = "Español"
REPORT_SCHEMA_VERSION = 1
MAX_MODELS = 16
MAX_REPETITIONS = 10
MAX_CASES = 32


@dataclass(frozen=True, slots=True)
class ReviewCase:
    """A public synthetic fragment with an exact expected reviewer result."""

    case_id: str
    class_label: str
    input_markdown: str
    expected_markdown: str


@dataclass(frozen=True, slots=True)
class TranslationReviewCase:
    """A synthetic EN→ES pair with an exact expected bilingual-review result."""

    case_id: str
    class_label: str
    source_markdown: str
    translated_markdown: str
    expected_markdown: str


# Original synthetic material: no user documents, prompts or model responses.
# The cases deliberately include clean controls and conservative repairs.
CORPUS: tuple[ReviewCase, ...] = (
    ReviewCase(
        "rps-v1-001",
        "clean",
        "# Registro\n\nEl archivo conserva `parsezen:1` y el enlace [local](https://example.test).",
        "# Registro\n\nEl archivo conserva `parsezen:1` y el enlace [local](https://example.test).",
    ),
    ReviewCase(
        "rps-v1-002",
        "clean",
        "- Primera entrada\n- Segunda entrada con 42 elementos\n\n> Nota breve.",
        "- Primera entrada\n- Segunda entrada con 42 elementos\n\n> Nota breve.",
    ),
    ReviewCase(
        "rps-v1-003",
        "clean",
        "## Parámetros\n\nLa opción `--offline` mantiene el procesamiento en el equipo.",
        "## Parámetros\n\nLa opción `--offline` mantiene el procesamiento en el equipo.",
    ),
    ReviewCase(
        "rps-v1-004",
        "clean",
        "| Campo | Valor |\n| --- | --- |\n| Estado | activo |",
        "| Campo | Valor |\n| --- | --- |\n| Estado | activo |",
    ),
    ReviewCase(
        "rps-v1-005",
        "repair",
        "La conversión produjo un resul tado estable.",
        "La conversión produjo un resultado estable.",
    ),
    ReviewCase(
        "rps-v1-006",
        "repair",
        "El informe esta listo para la revisión.",
        "El informe está listo para la revisión.",
    ),
    ReviewCase(
        "rps-v1-007",
        "repair",
        "Resumen\n\nPágina 7\n\nLos datos permanecen intactos.",
        "Resumen\n\nLos datos permanecen intactos.",
    ),
    ReviewCase(
        "rps-v1-008",
        "repair",
        "El  proceso   conserva el orden de lectura.",
        "El proceso conserva el orden de lectura.",
    ),
    ReviewCase(
        "rps-v1-009",
        "repair",
        "La cabecera Informe anual\n\nInforme anual\n\nEl resultado es reproducible.",
        "Informe anual\n\nEl resultado es reproducible.",
    ),
    ReviewCase(
        "rps-v1-010",
        "repair",
        "La salida fue validada correctamente .",
        "La salida fue validada correctamente.",
    ),
)


# Synthetic source/candidate pairs: one clean control and two objective semantic errors.
# The source and candidate are never written to the report.
BILINGUAL_CORPUS: tuple[TranslationReviewCase, ...] = (
    TranslationReviewCase(
        "brps-v1-001",
        "clean",
        "The local process preserves the original order.",
        "El proceso local conserva el orden original.",
        "El proceso local conserva el orden original.",
    ),
    TranslationReviewCase(
        "brps-v1-002",
        "repair",
        "The archive is ready for review.",
        "El archivo está cerrado para revisión.",
        "El archivo está listo para revisión.",
    ),
    TranslationReviewCase(
        "brps-v1-003",
        "repair",
        "The report contains three entries.",
        "El informe contiene dos entradas.",
        "El informe contiene tres entradas.",
    ),
)


@dataclass(frozen=True, slots=True)
class _RunResult:
    exact: bool = False
    true_positive: int = 0
    true_negative: int = 0
    false_positive: int = 0
    false_negative: int = 0
    error: bool = False
    output_sha256: str | None = None
    elapsed_ms: int = 0
    telemetry: BatchTelemetry | None = None


def corpus_sha256(corpus: Sequence[ReviewCase] = CORPUS) -> str:
    """Hash the versioned corpus without exposing its text in a report."""

    canonical = [
        {
            "case_id": case.case_id,
            "class": case.class_label,
            "input_sha256": _sha256(case.input_markdown),
            "expected_sha256": _sha256(case.expected_markdown),
        }
        for case in corpus
    ]
    return _sha256(json.dumps(canonical, ensure_ascii=False, separators=(",", ":")))


def bilingual_corpus_sha256(
    corpus: Sequence[TranslationReviewCase] = BILINGUAL_CORPUS,
) -> str:
    """Hash the synthetic EN→ES review corpus without exposing its text."""

    canonical = [
        {
            "case_id": case.case_id,
            "class": case.class_label,
            "source_sha256": _sha256(case.source_markdown),
            "translated_sha256": _sha256(case.translated_markdown),
            "expected_sha256": _sha256(case.expected_markdown),
        }
        for case in corpus
    ]
    return _sha256(json.dumps(canonical, ensure_ascii=False, separators=(",", ":")))


def evaluate_review_models(
    model_ids: Sequence[str],
    *,
    context_window: int = DEFAULT_CONTEXT_WINDOW,
    timeout_seconds: float = 120.0,
    repetitions: int = 1,
    corpus: Sequence[ReviewCase] = CORPUS,
    bilingual_corpus: Sequence[TranslationReviewCase] = BILINGUAL_CORPUS,
    installed_models: Mapping[str, str] | None = None,
    runner: Callable[..., object] | None = None,
    translation_runner: Callable[..., object] | None = None,
) -> dict[str, object]:
    """Evaluate explicitly installed model tags through the production reviewer.

    ``installed_models`` exists for deterministic tests.  In normal use it is
    omitted and the installed set comes solely from ``GET /api/tags``.
    """

    _validate_bounds(context_window, timeout_seconds, repetitions, corpus)
    _validate_bilingual_corpus(bilingual_corpus)
    requested = _normalize_requested_models(model_ids)
    available = (
        dict(installed_models)
        if installed_models is not None
        else {model.model_id: model.digest for model in list_ollama_models()}
    )
    missing = tuple(model for model in requested if model not in available)
    if missing:
        raise ValueError("Todos los modelos deben aparecer ya instalados en /api/tags.")
    if any(not _valid_digest(available[model]) for model in requested):
        raise ValueError("Ollama debe anunciar el digest exacto de cada modelo evaluado.")

    base_settings = AppSettings(
        context_window=context_window,
        timeout_seconds=timeout_seconds,
    )
    review_runner = improve_markdown if runner is None else runner
    bilingual_runner = (
        review_translation_markdown if translation_runner is None else translation_runner
    )
    model_reports = [
        _evaluate_model(
            model_id,
            digest=available[model_id],
            base_settings=base_settings,
            repetitions=repetitions,
            corpus=corpus,
            runner=review_runner,
        )
        for model_id in requested
    ]
    bilingual_model_reports = [
        _evaluate_bilingual_model(
            model_id,
            digest=available[model_id],
            base_settings=base_settings,
            repetitions=repetitions,
            corpus=bilingual_corpus,
            runner=bilingual_runner,
        )
        for model_id in requested
    ]
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "corpus_revision": CORPUS_REVISION,
        "corpus_sha256": corpus_sha256(corpus),
        "case_count": len(corpus),
        "options": {
            "mode": ImprovementMode.REVIEW_CONTENT.value,
            "context_window": context_window,
            "timeout_seconds": timeout_seconds,
            "repetitions": repetitions,
        },
        "models": model_reports,
        "bilingual_review": {
            "corpus_revision": BILINGUAL_CORPUS_REVISION,
            "corpus_sha256": bilingual_corpus_sha256(bilingual_corpus),
            "case_count": len(bilingual_corpus),
            "options": {
                "mode": "review_translation",
                "source_language": BILINGUAL_SOURCE_LANGUAGE,
                "target_language": "es",
                "context_window": context_window,
                "timeout_seconds": timeout_seconds,
                "repetitions": repetitions,
            },
            "models": bilingual_model_reports,
        },
    }


def write_report(path: Path, report: Mapping[str, object]) -> None:
    """Write a JSON report atomically, retaining only hashes and metrics."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _evaluate_model(
    model_id: str,
    *,
    digest: str,
    base_settings: AppSettings,
    repetitions: int,
    corpus: Sequence[ReviewCase],
    runner: Callable[..., object],
) -> dict[str, object]:
    case_reports: list[dict[str, object]] = []
    totals = _empty_counts()
    for case in corpus:
        case_counts = _empty_counts()
        output_hashes: list[str] = []
        for _ in range(repetitions):
            settings = AppSettings(
                model=model_id,
                context_window=base_settings.context_window,
                timeout_seconds=base_settings.timeout_seconds,
            )
            started = monotonic()
            with capture_batch_telemetry() as collector:
                try:
                    output = runner(case.input_markdown, ImprovementMode.REVIEW_CONTENT, settings)
                except Exception as exc:  # noqa: BLE001 - one bad run must not hide the matrix
                    run = _RunResult(
                        error=True,
                        elapsed_ms=_elapsed_ms(started),
                        telemetry=collector.snapshot(),
                    )
                    case_counts["errors"] += 1
                    case_counts["error_types"][type(exc).__name__] = (
                        case_counts["error_types"].get(type(exc).__name__, 0) + 1
                    )
                else:
                    if not isinstance(output, str):
                        error_type = "InvalidReviewerOutput"
                        run = _RunResult(
                            error=True,
                            elapsed_ms=_elapsed_ms(started),
                            telemetry=collector.snapshot(),
                        )
                        case_counts["errors"] += 1
                        case_counts["error_types"][error_type] = (
                            case_counts["error_types"].get(error_type, 0) + 1
                        )
                    else:
                        run = _classify_run(
                            case,
                            output,
                            _elapsed_ms(started),
                            collector.snapshot(),
                        )
                        if run.output_sha256 is not None:
                            output_hashes.append(run.output_sha256)
                        case_counts["exact_matches"] += int(run.exact)
                        for key in (
                            "true_positive",
                            "true_negative",
                            "false_positive",
                            "false_negative",
                        ):
                            case_counts[key] += getattr(run, key)
                case_counts["elapsed_ms"] += run.elapsed_ms
            _add_telemetry(case_counts, run.telemetry)
        for key, value in case_counts.items():
            if key == "error_types":
                for error_type, error_count in value.items():
                    totals["error_types"][error_type] = (
                        totals["error_types"].get(error_type, 0) + error_count
                    )
            else:
                totals[key] += value
        case_reports.append(
            {
                "case_id": _opaque_case_id(case.case_id),
                "class": case.class_label,
                "input_sha256": _sha256(case.input_markdown),
                "expected_sha256": _sha256(case.expected_markdown),
                "output_sha256": sorted(set(output_hashes)),
                **_public_counts(case_counts),
            }
        )

    return {
        "model_sha256": _sha256(f"{model_id}\0{digest}"),
        "ollama_digest": digest,
        "cases": case_reports,
        "metrics": _public_counts(totals),
    }


def _evaluate_bilingual_model(
    model_id: str,
    *,
    digest: str,
    base_settings: AppSettings,
    repetitions: int,
    corpus: Sequence[TranslationReviewCase],
    runner: Callable[..., object],
) -> dict[str, object]:
    case_reports: list[dict[str, object]] = []
    totals = _empty_counts()
    for case in corpus:
        case_counts = _empty_counts()
        output_hashes: list[str] = []
        for _ in range(repetitions):
            settings = AppSettings(
                model=model_id,
                context_window=base_settings.context_window,
                timeout_seconds=base_settings.timeout_seconds,
            )
            started = monotonic()
            with capture_batch_telemetry() as collector:
                try:
                    output = runner(
                        case.source_markdown,
                        case.translated_markdown,
                        settings,
                        BILINGUAL_TARGET_LANGUAGE,
                    )
                except Exception as exc:  # noqa: BLE001 - one bad run must not hide the matrix
                    run = _RunResult(
                        error=True,
                        elapsed_ms=_elapsed_ms(started),
                        telemetry=collector.snapshot(),
                    )
                    case_counts["errors"] += 1
                    case_counts["error_types"][type(exc).__name__] = (
                        case_counts["error_types"].get(type(exc).__name__, 0) + 1
                    )
                else:
                    if not isinstance(output, str):
                        error_type = "InvalidBilingualReviewerOutput"
                        run = _RunResult(
                            error=True,
                            elapsed_ms=_elapsed_ms(started),
                            telemetry=collector.snapshot(),
                        )
                        case_counts["errors"] += 1
                        case_counts["error_types"][error_type] = (
                            case_counts["error_types"].get(error_type, 0) + 1
                        )
                    else:
                        run = _classify_bilingual_run(
                            case,
                            output,
                            _elapsed_ms(started),
                            collector.snapshot(),
                        )
                        if run.output_sha256 is not None:
                            output_hashes.append(run.output_sha256)
                        case_counts["exact_matches"] += int(run.exact)
                        for key in (
                            "true_positive",
                            "true_negative",
                            "false_positive",
                            "false_negative",
                        ):
                            case_counts[key] += getattr(run, key)
                case_counts["elapsed_ms"] += run.elapsed_ms
            _add_telemetry(case_counts, run.telemetry)
        for key, value in case_counts.items():
            if key == "error_types":
                for error_type, error_count in value.items():
                    totals["error_types"][error_type] = (
                        totals["error_types"].get(error_type, 0) + error_count
                    )
            else:
                totals[key] += value
        case_reports.append(
            {
                "case_id": _opaque_case_id(case.case_id),
                "class": case.class_label,
                "source_sha256": _sha256(case.source_markdown),
                "translated_sha256": _sha256(case.translated_markdown),
                "expected_sha256": _sha256(case.expected_markdown),
                "output_sha256": sorted(set(output_hashes)),
                **_public_counts(case_counts),
            }
        )

    return {
        "model_sha256": _sha256(f"{model_id}\0{digest}"),
        "ollama_digest": digest,
        "cases": case_reports,
        "metrics": _public_counts(totals),
    }


def _classify_run(
    case: ReviewCase,
    output: str,
    elapsed_ms: int,
    telemetry: BatchTelemetry,
) -> _RunResult:
    return _classify_result(
        case.input_markdown,
        case.expected_markdown,
        output,
        elapsed_ms,
        telemetry,
    )


def _classify_bilingual_run(
    case: TranslationReviewCase,
    output: str,
    elapsed_ms: int,
    telemetry: BatchTelemetry,
) -> _RunResult:
    return _classify_result(
        case.translated_markdown,
        case.expected_markdown,
        output,
        elapsed_ms,
        telemetry,
    )


def _classify_result(
    input_markdown: str,
    expected_markdown: str,
    output: str,
    elapsed_ms: int,
    telemetry: BatchTelemetry,
) -> _RunResult:
    expected_change = expected_markdown != input_markdown
    actual_change = output != input_markdown
    exact = output == expected_markdown
    return _RunResult(
        exact=exact,
        true_positive=int(expected_change and exact),
        true_negative=int(not expected_change and not actual_change),
        false_positive=int(not expected_change and actual_change),
        false_negative=int(expected_change and not exact),
        output_sha256=_sha256(output),
        elapsed_ms=elapsed_ms,
        telemetry=telemetry,
    )


def _empty_counts() -> dict[str, Any]:
    return {
        "runs": 0,
        "exact_matches": 0,
        "true_positive": 0,
        "true_negative": 0,
        "false_positive": 0,
        "false_negative": 0,
        "errors": 0,
        "error_types": {},
        "guard_rejections": 0,
        "retries": 0,
        "local_ai_requests": 0,
        "prompt_tokens": 0,
        "output_tokens": 0,
        "model_wall_duration_ms": 0,
        "elapsed_ms": 0,
    }


def _add_telemetry(counts: dict[str, Any], telemetry: BatchTelemetry | None) -> None:
    counts["runs"] += 1
    if telemetry is None:
        return
    counts["guard_rejections"] += telemetry.validation_rejections
    counts["retries"] += telemetry.retries
    counts["local_ai_requests"] += telemetry.local_ai_requests
    counts["prompt_tokens"] += telemetry.prompt_tokens
    counts["output_tokens"] += telemetry.output_tokens
    counts["model_wall_duration_ms"] += telemetry.wall_duration_ms


def _public_counts(counts: Mapping[str, Any]) -> dict[str, object]:
    true_positive = int(counts["true_positive"])
    false_positive = int(counts["false_positive"])
    false_negative = int(counts["false_negative"])
    evaluated = true_positive + int(counts["true_negative"]) + false_positive + false_negative
    return {
        "runs": int(counts["runs"]),
        "exact_matches": int(counts["exact_matches"]),
        "accuracy": _ratio(
            int(counts["exact_matches"]),
            evaluated,
        ),
        "true_positive": true_positive,
        "true_negative": int(counts["true_negative"]),
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": _ratio(true_positive, true_positive + false_positive),
        "recall": _ratio(true_positive, true_positive + false_negative),
        "errors": int(counts["errors"]),
        "error_types": dict(sorted(counts["error_types"].items())),
        "guard_rejections": int(counts["guard_rejections"]),
        "retries": int(counts["retries"]),
        "local_ai_requests": int(counts["local_ai_requests"]),
        "prompt_tokens": int(counts["prompt_tokens"]),
        "output_tokens": int(counts["output_tokens"]),
        "model_wall_duration_ms": int(counts["model_wall_duration_ms"]),
        "elapsed_ms": int(counts["elapsed_ms"]),
    }


def _normalize_requested_models(model_ids: Sequence[str]) -> tuple[str, ...]:
    if isinstance(model_ids, str) or not model_ids or len(model_ids) > MAX_MODELS:
        raise ValueError(f"Indica entre 1 y {MAX_MODELS} tags de modelos instalados.")
    normalized = tuple(dict.fromkeys(validate_ollama_model_id(model) for model in model_ids))
    if len(normalized) != len(model_ids):
        raise ValueError("Cada tag de modelo debe aparecer una sola vez.")
    return normalized


def _validate_bounds(
    context_window: int,
    timeout_seconds: float,
    repetitions: int,
    corpus: Sequence[ReviewCase],
) -> None:
    if (
        isinstance(context_window, bool)
        or not isinstance(context_window, int)
        or not MIN_CONTEXT_WINDOW <= context_window <= MAX_CONTEXT_WINDOW
    ):
        raise ValueError("La ventana de contexto no está dentro de un tamaño seguro.")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 1 <= timeout_seconds <= 600
    ):
        raise ValueError("El timeout debe estar entre 1 y 600 segundos.")
    if isinstance(repetitions, bool) or not 1 <= repetitions <= MAX_REPETITIONS:
        raise ValueError(f"Las repeticiones deben estar entre 1 y {MAX_REPETITIONS}.")
    if not corpus or len(corpus) > MAX_CASES:
        raise ValueError(f"El corpus debe contener entre 1 y {MAX_CASES} casos.")
    if any(not isinstance(case, ReviewCase) for case in corpus):
        raise ValueError("El corpus contiene un caso no válido.")
    case_ids = [case.case_id for case in corpus]
    if len(set(case_ids)) != len(case_ids) or any(not case_id for case_id in case_ids):
        raise ValueError("Los IDs del corpus deben ser únicos y no vacíos.")
    for case in corpus:
        if (
            not isinstance(case, ReviewCase)
            or not isinstance(case.input_markdown, str)
            or not case.input_markdown.strip()
            or not isinstance(case.expected_markdown, str)
            or not case.expected_markdown.strip()
            or case.class_label not in {"clean", "repair"}
        ):
            raise ValueError("El corpus contiene un caso no válido.")
        expected_change = case.expected_markdown != case.input_markdown
        if (case.class_label == "clean" and expected_change) or (
            case.class_label == "repair" and not expected_change
        ):
            raise ValueError("La clase del corpus no coincide con su salida esperada.")


def _validate_bilingual_corpus(corpus: Sequence[TranslationReviewCase]) -> None:
    if not corpus or len(corpus) > MAX_CASES:
        raise ValueError(f"El corpus bilingüe debe contener entre 1 y {MAX_CASES} casos.")
    if any(not isinstance(case, TranslationReviewCase) for case in corpus):
        raise ValueError("El corpus bilingüe contiene un caso no válido.")
    case_ids = [case.case_id for case in corpus]
    if len(set(case_ids)) != len(case_ids) or any(not case_id for case_id in case_ids):
        raise ValueError("Los IDs del corpus bilingüe deben ser únicos y no vacíos.")
    for case in corpus:
        if (
            not isinstance(case.source_markdown, str)
            or not case.source_markdown.strip()
            or not isinstance(case.translated_markdown, str)
            or not case.translated_markdown.strip()
            or not isinstance(case.expected_markdown, str)
            or not case.expected_markdown.strip()
            or case.class_label not in {"clean", "repair"}
        ):
            raise ValueError("El corpus bilingüe contiene un caso no válido.")
        expected_change = case.expected_markdown != case.translated_markdown
        if (case.class_label == "clean" and expected_change) or (
            case.class_label == "repair" and not expected_change
        ):
            raise ValueError("La clase del corpus bilingüe no coincide con su salida esperada.")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _valid_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _opaque_case_id(case_id: str) -> str:
    return f"case-v{REPORT_SCHEMA_VERSION}-{_sha256(case_id)[:16]}"


def _elapsed_ms(started: float) -> int:
    return max(0, round((monotonic() - started) * 1_000))


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        dest="models",
        action="append",
        required=True,
        help="Tag exacto ya instalado; se puede repetir para comparar modelos.",
    )
    parser.add_argument("--output", type=Path, required=True, help="Ruta del informe JSON.")
    parser.add_argument("--context-window", type=int, default=DEFAULT_CONTEXT_WINDOW)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--repetitions", type=int, default=1)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = evaluate_review_models(
            args.models,
            context_window=args.context_window,
            timeout_seconds=args.timeout_seconds,
            repetitions=args.repetitions,
        )
        write_report(args.output, report)
    except (OSError, ValueError) as exc:
        print(f"No se pudo completar la evaluación local: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
