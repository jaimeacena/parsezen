"""Opt-in, privacy-safe comparison of installed Ollama translation models.

This evaluator is deliberately separate from the real-workflow validator.  It exercises the
production ``improve_markdown`` translation entry point against the checked-in synthetic corpus,
never installs a model, and emits only opaque case IDs, hashes and aggregate metrics.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkstemp
from time import monotonic
from typing import Any

import httpx

from parsezen.errors import ParsezenError
from parsezen.glossary import GlossaryEntry, protect_glossary, validate_glossary
from parsezen.improvement import ImprovementMode, improve_markdown
from parsezen.local_models import (
    DEFAULT_CONTEXT_WINDOW,
    OllamaConnection,
    OllamaStatus,
    discover_ollama,
    is_cloud_model_id,
)
from parsezen.processing_metrics import BatchTelemetry, capture_batch_telemetry
from parsezen.settings import MAX_CONTEXT_WINDOW, MIN_CONTEXT_WINDOW, AppSettings, validate_settings
from parsezen.translation_quality import (
    TranslationQualityError,
    build_translation_quality_report,
    find_untranslated_source_sentences,
    find_untranslated_title_lines,
    numeric_tokens_are_conserved,
    validate_translation_quality,
)

CORPUS_PATH = Path(__file__).with_name("translation_evaluation_corpus_v1.json")
REPORT_SCHEMA_VERSION = 1
CORPUS_VERSION = "parsezen-translation-en-es-v1"
MAX_MODELS = 16
MAX_REPETITIONS = 10
TARGET_LANGUAGE = "Español"
SOURCE_LANGUAGE = "en"


@dataclass(frozen=True, slots=True)
class CorpusCase:
    """One public synthetic source and its optional exact-match expectation."""

    case_id: str
    tags: tuple[str, ...]
    source: str
    expected: str | None
    glossary: tuple[GlossaryEntry, ...] = ()


def load_corpus(path: Path = CORPUS_PATH) -> tuple[CorpusCase, ...]:
    """Load and validate the versioned, synthetic EN→ES corpus."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("No se pudo cargar el corpus sintético versionado.") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("La versión del corpus sintético no es compatible.")
    if raw.get("corpus_version") != CORPUS_VERSION:
        raise ValueError("La versión declarada del corpus sintético no es compatible.")
    if raw.get("source_language") != SOURCE_LANGUAGE or raw.get("target_language") != "es":
        raise ValueError("El corpus sintético debe ser EN→ES.")
    raw_cases = raw.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("El corpus sintético no contiene casos.")
    cases: list[CorpusCase] = []
    seen_ids: set[str] = set()
    for item in raw_cases:
        if not isinstance(item, dict):
            raise ValueError("El corpus sintético contiene un caso no válido.")
        case_id = item.get("id")
        tags = item.get("tags")
        source = item.get("source")
        expected = item.get("expected")
        raw_glossary = item.get("glossary", [])
        normalized_case_id = case_id.strip() if isinstance(case_id, str) else ""
        if (
            not isinstance(case_id, str)
            or not normalized_case_id
            or normalized_case_id in seen_ids
            or not isinstance(tags, list)
            or not tags
            or not all(isinstance(tag, str) and tag.strip() for tag in tags)
            or not isinstance(source, str)
            or not source.strip()
            or expected is not None
            and not isinstance(expected, str)
            or not isinstance(raw_glossary, list)
        ):
            raise ValueError("El corpus sintético contiene campos no válidos.")
        glossary = validate_glossary(
            tuple(
                GlossaryEntry(entry["source"], entry["target"])
                for entry in raw_glossary
                if isinstance(entry, dict)
                and isinstance(entry.get("source"), str)
                and isinstance(entry.get("target"), str)
            )
        )
        if len(glossary) != len(raw_glossary):
            raise ValueError("El corpus sintético contiene un glosario no válido.")
        seen_ids.add(normalized_case_id)
        cases.append(
            CorpusCase(
                case_id=normalized_case_id,
                tags=tuple(tag.strip() for tag in tags),
                source=source,
                expected=expected,
                glossary=glossary,
            )
        )
    return tuple(cases)


def evaluate_models(
    models: Sequence[str],
    *,
    repetitions: int = 1,
    context_window: int | None = None,
    timeout_seconds: float = 120.0,
    target_language: str = TARGET_LANGUAGE,
    corpus: tuple[CorpusCase, ...] | None = None,
    ollama_connection: OllamaConnection | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Compare installed model tags under identical translation options.

    ``transport`` exists solely for deterministic tests.  In normal use the production function
    talks to Ollama's fixed loopback transport, and model discovery only reads ``/api/tags``.
    """

    requested_models = _normalise_models(models)
    if isinstance(repetitions, bool) or not 1 <= repetitions <= MAX_REPETITIONS:
        raise ValueError(f"Las repeticiones deben estar entre 1 y {MAX_REPETITIONS}.")
    if context_window is not None and (
        isinstance(context_window, bool)
        or not MIN_CONTEXT_WINDOW <= context_window <= MAX_CONTEXT_WINDOW
    ):
        raise ValueError("La ventana de contexto no está dentro de un tamaño seguro.")
    if target_language != TARGET_LANGUAGE:
        raise ValueError("El evaluador versionado solo admite Español como destino.")
    if isinstance(timeout_seconds, bool) or not 1.0 <= timeout_seconds <= 600.0:
        raise ValueError("El timeout debe estar entre 1 y 600 segundos.")

    connection = ollama_connection if ollama_connection is not None else discover_ollama(None)
    if connection.status is not OllamaStatus.READY:
        raise RuntimeError(connection.message or "Ollama no está preparado.")
    installed = {item.model_id: item for item in connection.models}
    missing = tuple(model for model in requested_models if model not in installed)
    if missing:
        raise RuntimeError("Todos los tags de la evaluación deben estar instalados en Ollama.")
    if any(
        is_cloud_model_id(model) or model.casefold().endswith("-cloud")
        for model in requested_models
    ):
        raise RuntimeError("Los tags cloud no participan en una evaluación local.")
    if any(installed[model].digest is None for model in requested_models):
        raise RuntimeError("Ollama no anunció el digest de todos los modelos instalados.")

    cases = load_corpus() if corpus is None else _validate_cases(corpus)
    shared_context = context_window or DEFAULT_CONTEXT_WINDOW
    if any(
        installed[model].max_context is not None and shared_context > installed[model].max_context
        for model in requested_models
    ):
        raise RuntimeError("La ventana común supera el máximo anunciado por un modelo.")
    settings = {
        model: validate_settings(
            AppSettings(
                model=model,
                context_window=shared_context,
                timeout_seconds=timeout_seconds,
            )
        )
        for model in requested_models
    }
    run_id = f"eval-v{REPORT_SCHEMA_VERSION}-{secrets.token_hex(8)}"
    case_ids = {case.case_id: _opaque_case_id(case.case_id) for case in cases}
    records: list[dict[str, Any]] = []
    for model in requested_models:
        for repetition in range(1, repetitions + 1):
            for case in cases:
                records.append(
                    _run_case(
                        case,
                        model=model,
                        settings=settings[model],
                        repetition=repetition,
                        case_id=case_ids[case.case_id],
                        transport=transport,
                    )
                )

    corpus_digest = _corpus_digest(cases)
    model_ids = {
        model: _opaque_model_id(model, installed[model].digest or "") for model in requested_models
    }
    model_records = {
        model_ids[model]: {
            "ollama_digest": installed[model].digest,
            **_aggregate_model_records(
                tuple(record for record in records if record["model"] == model)
            ),
        }
        for model in requested_models
    }
    for record in records:
        record["model"] = model_ids[record["model"]]
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "evaluation_id": run_id,
        "corpus": {
            "version": CORPUS_VERSION,
            "sha256": corpus_digest,
            "case_count": len(cases),
        },
        "options": {
            "source_language": SOURCE_LANGUAGE,
            "target_language": "es",
            "mode": ImprovementMode.TRANSLATE.value,
            "context_window": shared_context,
            "repetitions": repetitions,
            "timeout_seconds": timeout_seconds,
            "glossary_cases": sum(bool(case.glossary) for case in cases),
        },
        "models": model_records,
        "cases": records,
        "privacy": (
            "No contiene contenido documental, nombres de casos, rutas, prompts ni respuestas; "
            "solo IDs opacos, hashes y métricas agregadas."
        ),
    }


def write_report(destination: Path, report: dict[str, Any]) -> None:
    """Write a JSON report atomically without adding the destination path to its payload."""

    destination = Path(destination)
    temporary_path: Path | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = mkstemp(
            dir=destination.parent,
            prefix=".parsezen-translation-evaluation-",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeError("No se pudo escribir atómicamente el informe de evaluación.") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _run_case(
    case: CorpusCase,
    *,
    model: str,
    settings: AppSettings,
    repetition: int,
    case_id: str,
    transport: httpx.BaseTransport | None,
) -> dict[str, Any]:
    protected = protect_glossary(case.source, case.glossary)
    started = monotonic()
    translated = ""
    error_type: str | None = None
    with capture_batch_telemetry() as collector:
        try:
            # This is the same production entry point used by the document pipeline.  Glossary
            # protection/restoration mirrors the pipeline boundary and keeps terms out of prompts.
            translated = improve_markdown(
                protected.text,
                ImprovementMode.TRANSLATE,
                settings,
                TARGET_LANGUAGE,
                transport=transport,
                source_language_code=SOURCE_LANGUAGE,
            )
            if not isinstance(translated, str):
                raise TypeError("La traducción no devolvió texto.")
            translated = protected.restore(translated)
        except Exception as exc:  # noqa: BLE001 - one failed case must not hide the matrix
            error_type = type(exc).__name__
    telemetry = collector.snapshot()
    elapsed_seconds = round(monotonic() - started, 3)
    metrics = _case_metrics(case, translated, error_type, telemetry, elapsed_seconds)
    return {
        "case_id": case_id,
        "model": model,
        "repetition": repetition,
        "metrics": metrics,
    }


def _case_metrics(
    case: CorpusCase,
    translated: str,
    error_type: str | None,
    telemetry: BatchTelemetry,
    elapsed_seconds: float,
) -> dict[str, Any]:
    report = None
    validation_passed = False
    residual_count = 0
    numbers_conserved = False
    glossary_preserved = not case.glossary
    coverage = 0.0
    exact_match = None if case.expected is None else False
    output_sha256 = None
    if error_type is None:
        output_sha256 = _sha256_text(translated)
        report = build_translation_quality_report(
            case.source,
            translated,
            target_language="es",
            source_language=SOURCE_LANGUAGE,
        )
        try:
            validate_translation_quality(
                case.source,
                translated,
                source_language=SOURCE_LANGUAGE,
                target_language="es",
                preserve_paragraphs=True,
            )
        except TranslationQualityError:
            validation_passed = False
        else:
            validation_passed = True
        residual = set(find_untranslated_source_sentences(case.source, translated, SOURCE_LANGUAGE))
        residual.update(find_untranslated_title_lines(case.source, translated, SOURCE_LANGUAGE))
        residual_count = len(residual)
        numbers_conserved = numeric_tokens_are_conserved(case.source, translated)
        glossary_preserved = _glossary_targets_preserved(case, translated)
        coverage = report.checked_segments / report.source_blocks if report.source_blocks else 1.0
        if case.expected is not None:
            exact_match = _normalise_text(translated) == _normalise_text(case.expected)
    return {
        "passed": error_type is None,
        "quality_gate_passed": bool(
            error_type is None
            and validation_passed
            and residual_count == 0
            and numbers_conserved
            and glossary_preserved
        ),
        "gates": {
            "translation_validation": validation_passed,
            "residual_free": residual_count == 0,
            "numbers_conserved": numbers_conserved,
            "glossary_preserved": glossary_preserved,
        },
        "coverage_ratio": round(coverage, 4),
        "residual_count": residual_count,
        "quality_issue_count": report.total_issues if report is not None else 0,
        "source_blocks": report.source_blocks if report is not None else 0,
        "translated_blocks": report.translated_blocks if report is not None else 0,
        "numbers_conserved": numbers_conserved,
        "glossary_terms_checked": len(case.glossary),
        "glossary_preserved": glossary_preserved,
        "exact_match": exact_match,
        "output_sha256": output_sha256,
        "elapsed_seconds": elapsed_seconds,
        "local_ai_requests": telemetry.local_ai_requests,
        "prompt_tokens": telemetry.prompt_tokens,
        "output_tokens": telemetry.output_tokens,
        "wall_duration_ms": telemetry.wall_duration_ms,
        "ollama_total_duration_ms": telemetry.ollama_total_duration_ms,
        "ollama_load_duration_ms": telemetry.ollama_load_duration_ms,
        "output_tokens_per_second": round(telemetry.output_tokens_per_second, 2),
        "retries": telemetry.retries,
        "validation_rejections": telemetry.validation_rejections,
        "error_type": error_type,
    }


def _aggregate_model_records(records: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    metrics = tuple(record["metrics"] for record in records)
    count = len(metrics)

    def rate(predicate: Iterable[bool]) -> float:
        values = tuple(predicate)
        return round(sum(values) / len(values), 4) if values else 0.0

    exact_values = tuple(item["exact_match"] for item in metrics if item["exact_match"] is not None)
    return {
        "run_count": count,
        "quality_gate_pass_rate": rate(item["quality_gate_passed"] for item in metrics),
        "translation_validation_pass_rate": rate(
            item["gates"]["translation_validation"] for item in metrics
        ),
        "coverage_ratio_mean": round(sum(item["coverage_ratio"] for item in metrics) / count, 4)
        if count
        else 0.0,
        "residual_count_total": sum(item["residual_count"] for item in metrics),
        "numbers_conserved_pass_rate": rate(item["numbers_conserved"] for item in metrics),
        "glossary_preserved_pass_rate": rate(item["glossary_preserved"] for item in metrics),
        "exact_match_count": sum(exact_values),
        "exact_match_applicable": len(exact_values),
        "exact_match_rate": rate(exact_values),
        "elapsed_seconds_total": round(sum(item["elapsed_seconds"] for item in metrics), 3),
        "prompt_tokens_total": sum(item["prompt_tokens"] for item in metrics),
        "output_tokens_total": sum(item["output_tokens"] for item in metrics),
        "local_ai_requests_total": sum(item["local_ai_requests"] for item in metrics),
        "retries_total": sum(item["retries"] for item in metrics),
        "validation_rejections_total": sum(item["validation_rejections"] for item in metrics),
        "wall_duration_ms_total": sum(item["wall_duration_ms"] for item in metrics),
        "ollama_total_duration_ms_total": sum(item["ollama_total_duration_ms"] for item in metrics),
        "ollama_load_duration_ms_total": sum(item["ollama_load_duration_ms"] for item in metrics),
        "error_count": sum(item["error_type"] is not None for item in metrics),
        "error_types": _error_type_counts(metrics),
        "tokens_available": any(item["prompt_tokens"] or item["output_tokens"] for item in metrics),
    }


def _error_type_counts(metrics: tuple[dict[str, Any], ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in metrics:
        error_type = item["error_type"]
        if isinstance(error_type, str):
            counts[error_type] = counts.get(error_type, 0) + 1
    return dict(sorted(counts.items()))


def _validate_cases(cases: tuple[CorpusCase, ...]) -> tuple[CorpusCase, ...]:
    if not cases or len({case.case_id for case in cases}) != len(cases):
        raise ValueError("El corpus sintético contiene IDs repetidos o está vacío.")
    for case in cases:
        if not isinstance(case, CorpusCase) or not case.source.strip():
            raise ValueError("El corpus sintético contiene un caso no válido.")
        validate_glossary(case.glossary)
    return cases


def _normalise_models(models: Sequence[str]) -> tuple[str, ...]:
    if isinstance(models, str) or not models or len(models) > MAX_MODELS:
        raise ValueError(f"La evaluación necesita entre 1 y {MAX_MODELS} tags explícitos.")
    if any(not isinstance(model, str) for model in models):
        raise ValueError("Los tags de modelo no son válidos.")
    normalized = tuple(model.strip() for model in models)
    if any(not model or any(character in model for character in "\r\n\0") for model in normalized):
        raise ValueError("Los tags de modelo no son válidos.")
    if len(set(normalized)) != len(normalized):
        raise ValueError("La evaluación no puede repetir un tag de modelo.")
    return normalized


def _opaque_case_id(case_id: str) -> str:
    return f"case-v{REPORT_SCHEMA_VERSION}-{hashlib.sha256(case_id.encode()).hexdigest()[:16]}"


def _opaque_model_id(model_id: str, digest: str) -> str:
    identity = f"{model_id}\0{digest}"
    return f"model-v{REPORT_SCHEMA_VERSION}-{hashlib.sha256(identity.encode()).hexdigest()[:16]}"


def _corpus_digest(cases: tuple[CorpusCase, ...]) -> str:
    canonical = [
        {
            "id": case.case_id,
            "tags": case.tags,
            "source": case.source,
            "expected": case.expected,
            "glossary": [(entry.source, entry.target) for entry in case.glossary],
        }
        for case in cases
    ]
    return hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _glossary_targets_preserved(case: CorpusCase, translated: str) -> bool:
    if not case.glossary:
        return True
    protected = protect_glossary(case.source, case.glossary)
    for _marker, target in protected.replacements:
        if not target or translated.count(target) < 1:
            return False
    return True


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalise_text(value: str) -> str:
    return value.replace("\r\n", "\n").strip()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compara tags locales de Ollama con un corpus sintético EN-to-ES."
    )
    parser.add_argument(
        "--models",
        nargs="+",
        required=True,
        metavar="TAG",
        help="Tags explícitos ya instalados; no se descargan modelos.",
    )
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--context-window", type=int)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("local-benchmarks") / "translation-evaluation" / "latest.json",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        report = evaluate_models(
            arguments.models,
            repetitions=arguments.repetitions,
            context_window=arguments.context_window,
            timeout_seconds=arguments.timeout,
        )
        write_report(arguments.report, report)
    except (ParsezenError, OSError, RuntimeError, ValueError) as exc:
        print(f"Evaluación no ejecutada: {type(exc).__name__}", file=sys.stderr)
        return 2
    print("Informe comparativo local creado.")
    return (
        0
        if all(summary["quality_gate_pass_rate"] == 1.0 for summary in report["models"].values())
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
