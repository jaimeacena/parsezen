"""Opt-in end-to-end validation with the user's real local Ollama model."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import secrets
import sys
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from itertools import product
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from xml.etree import ElementTree
from zipfile import ZIP_STORED, BadZipFile, ZipFile

from parsezen.conversion import SUPPORTED_EXTENSIONS
from parsezen.epub_conversion import inspect_epub_package
from parsezen.errors import ParsezenError, SettingsError
from parsezen.glossary import GlossaryEntry, validate_glossary
from parsezen.improvement import ImprovementMode
from parsezen.local_models import (
    OllamaStatus,
    discover_ollama,
    is_cloud_model_id,
    is_reasoning_model_id,
)
from parsezen.pdf_conversion import PdfPageRange
from parsezen.processing import (
    OutputFormat,
    ProcessRequest,
    ProcessStage,
    apply_reviewed_revision,
    process_document,
)
from parsezen.revision import RevisionDecision
from parsezen.settings import AppSettings, load_settings

REPORT_SCHEMA_VERSION = 14
EVALUATION_REPORT_SCHEMA_VERSION = 1
DEFAULT_REPORT_PATH = Path("local-benchmarks") / "real-workflows" / "latest.json"
SYNTHETIC_PAGE_COUNT = 20
MAX_EPUB_HEADING_CHARACTERS = 320
MAX_EPUB_HEADING_WORDS = 40
MAX_EVALUATION_MODELS = 16
MAX_EVALUATION_REPETITIONS = 10


@dataclass(frozen=True, slots=True)
class LiveWorkflowCase:
    """One bounded combination exercised against the actual local model."""

    name: str
    output_format: OutputFormat
    translate: bool
    review_content: bool
    review_structure: bool


@dataclass(frozen=True, slots=True)
class LiveWorkflowResult:
    """Content-free outcome suitable for a local diagnostic report."""

    source_index: int
    source_extension: str
    case: str
    passed: bool
    elapsed_seconds: float
    stages: tuple[str, ...]
    translation_engine: str | None = None
    quality_gate_passed: bool = False
    output_extension: str | None = None
    output_bytes: int | None = None
    output_sha256: str | None = None
    revision_created: bool = False
    revision_changes: int = 0
    recommended_revision_changes: int = 0
    rejected_revision_changes: int = 0
    revision_changes_by_kind: dict[str, int] = field(default_factory=dict)
    recommended_revision_changes_by_kind: dict[str, int] = field(default_factory=dict)
    processed_pages: int = 0
    ocr_pages: int = 0
    pdf_issues: int = 0
    blocking_pdf_issues: int = 0
    translation_issues: int = 0
    translation_issue_kinds: dict[str, int] = field(default_factory=dict)
    preserved_translation_chunks: int = 0
    preserved_translation_chunk_numbers: tuple[int, ...] = ()
    preserved_images: int = 0
    epub_chapters: int = 0
    epub_structure_issues: int = 0
    source_epub_structure_issues: int = 0
    epub_structure_regressions: int = 0
    ocr_replaced_pages: int = 0
    low_confidence_pages: int = 0
    front_matter_blocks: int = 0
    toc_blocks: int = 0
    terminology_terms: int = 0
    stage_duration_ms: dict[str, int] = field(default_factory=dict)
    planned_text_passes: int = 0
    planned_ai_passes: int = 0
    local_ai_requests: int = 0
    checkpoint_hits: int = 0
    checkpoint_misses: int = 0
    retries: int = 0
    validation_rejections: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    local_ai_wall_duration_ms: int = 0
    output_tokens_per_second: float = 0.0
    ai_operations: dict[str, dict[str, int]] = field(default_factory=dict)
    error_type: str | None = None
    failed_stage: str | None = None


@dataclass(frozen=True, slots=True)
class LocalEvaluationRun:
    """One model/repetition run, paired without exposing a source identity."""

    case_id: str
    model: str
    context_window: int
    repetition: int
    result: LiveWorkflowResult


CRITICAL_CASES = (
    LiveWorkflowCase(
        "epub-completo-con-tres-mejoras",
        OutputFormat.EPUB,
        translate=True,
        review_content=True,
        review_structure=True,
    ),
)

TRANSLATION_CASES = (
    LiveWorkflowCase(
        "epub-solo-traduccion",
        OutputFormat.EPUB,
        translate=True,
        review_content=False,
        review_structure=False,
    ),
)

REVIEW_CASES = (
    LiveWorkflowCase(
        "markdown-procesamiento-directo",
        OutputFormat.MARKDOWN,
        translate=False,
        review_content=False,
        review_structure=False,
    ),
    LiveWorkflowCase(
        "markdown-revision-semantica",
        OutputFormat.MARKDOWN,
        translate=False,
        review_content=True,
        review_structure=False,
    ),
)

TRANSLATION_REVIEW_CASES = (
    LiveWorkflowCase(
        "epub-traduccion-directa",
        OutputFormat.EPUB,
        translate=True,
        review_content=False,
        review_structure=False,
    ),
    LiveWorkflowCase(
        "epub-traduccion-revisada",
        OutputFormat.EPUB,
        translate=True,
        review_content=True,
        review_structure=True,
    ),
)

WORKFLOW_PROFILES = frozenset({"critical", "translation", "review", "translation-review"})


def full_matrix_cases() -> tuple[LiveWorkflowCase, ...]:
    """Return both outputs for all eight combinations of optional improvements."""
    cases: list[LiveWorkflowCase] = []
    for output_format in (OutputFormat.MARKDOWN, OutputFormat.EPUB):
        for translate, review_content, review_structure in product((False, True), repeat=3):
            enabled = "-".join(
                label
                for active, label in (
                    (translate, "traduccion"),
                    (review_content, "contenido"),
                    (review_structure, "estructura"),
                )
                if active
            )
            cases.append(
                LiveWorkflowCase(
                    f"{output_format.value}-{enabled or 'sin-mejoras'}",
                    output_format,
                    translate,
                    review_content,
                    review_structure,
                )
            )
    return tuple(cases)


def select_live_settings(
    requested_model: str | None = None,
    *,
    context_window: int | None = None,
) -> AppSettings:
    """Select only a model currently announced by the protected local Ollama server."""
    try:
        saved = load_settings()
    except SettingsError:
        saved = AppSettings()
    preferred = requested_model or saved.model
    connection = discover_ollama(preferred)
    if connection.status is not OllamaStatus.READY:
        detail = connection.message or "Ollama no está preparado."
        raise RuntimeError(detail)
    document_models = tuple(
        model for model in connection.models if not is_reasoning_model_id(model.model_id)
    )
    installed_ids = {model.model_id for model in document_models}
    selected = connection.selected_model
    if requested_model is not None:
        selected = requested_model if requested_model in installed_ids else None
    if selected is None and requested_model is None and document_models:
        selected = document_models[0].model_id
    if selected is None:
        raise RuntimeError(
            "El modelo solicitado no está instalado o no es apto para transformar documentos. "
            "Elige una variante Instruct."
        )
    model = next(item for item in document_models if item.model_id == selected)
    resolved_context = context_window or saved.context_window or model.recommended_context
    return AppSettings(
        model=selected,
        context_window=resolved_context,
        timeout_seconds=max(saved.timeout_seconds, 300.0),
        checkpoint_retention_days=saved.checkpoint_retention_days,
    )


def run_live_workflows(
    sources: tuple[Path, ...],
    output_root: Path,
    settings: AppSettings,
    *,
    target_language: str = "Español",
    full_matrix: bool = False,
    page_range: PdfPageRange | None = None,
    glossary: tuple[GlossaryEntry, ...] = (),
    translation_engine: str = "local_ai",
    workflow_profile: str = "critical",
    on_results: Callable[[tuple[LiveWorkflowResult, ...]], None] | None = None,
) -> tuple[LiveWorkflowResult, ...]:
    """Run real workflows while collecting no document text, names or paths."""
    if not sources:
        raise ValueError("Añade al menos un documento para la comprobación real.")
    if translation_engine not in {"argos", "local_ai"}:
        raise ValueError("El motor de traducción de la comprobación no es válido.")
    if workflow_profile not in WORKFLOW_PROFILES:
        raise ValueError("El perfil de comprobación no es válido.")
    if full_matrix and workflow_profile != "critical":
        raise ValueError("La matriz completa no se puede combinar con otro perfil.")
    cases = _workflow_cases(workflow_profile, full_matrix=full_matrix)
    output_root.mkdir(parents=True, exist_ok=True)
    results: list[LiveWorkflowResult] = []
    for source_index, raw_source in enumerate(sources, start=1):
        source = raw_source.resolve(strict=True)
        extension = source.suffix.lower()
        if extension not in SUPPORTED_EXTENSIONS:
            raise ValueError("La comprobación contiene un formato no compatible.")
        if full_matrix and extension != ".pdf":
            raise ValueError("La matriz completa se reserva para una muestra PDF breve.")
        source_hash = _file_sha256(source)
        source_epub_structure_issues = (
            _validate_generated_output(source, OutputFormat.EPUB) if extension == ".epub" else 0
        )
        for case in cases:
            stages: list[ProcessStage] = []
            started = monotonic()
            case_output = output_root / f"source-{source_index}" / case.name
            case_output.mkdir(parents=True, exist_ok=True)
            result_path: Path | None = None
            output_sha256: str | None = None
            revision_created = False
            revision_changes = 0
            recommended_revision_changes = 0
            rejected_revision_changes = 0
            revision_changes_by_kind: dict[str, int] = {}
            recommended_revision_changes_by_kind: dict[str, int] = {}
            processed_pages = 0
            ocr_pages = 0
            pdf_issues = 0
            blocking_pdf_issues = 0
            translation_issues = 0
            translation_issue_kinds: dict[str, int] = {}
            preserved_translation_chunks = 0
            preserved_translation_chunk_numbers: tuple[int, ...] = ()
            preserved_images = 0
            epub_chapters = 0
            epub_structure_issues = 0
            epub_structure_regressions = 0
            ocr_replaced_pages = 0
            low_confidence_pages = 0
            front_matter_blocks = 0
            toc_blocks = 0
            terminology_terms = 0
            stage_duration_ms: dict[str, int] = {}
            local_ai_requests = 0
            checkpoint_hits = 0
            checkpoint_misses = 0
            retries = 0
            validation_rejections = 0
            prompt_tokens = 0
            output_tokens = 0
            local_ai_wall_duration_ms = 0
            output_tokens_per_second = 0.0
            ai_operations: dict[str, dict[str, int]] = {}
            planned_text_passes, planned_ai_passes = _planned_passes(
                case,
                translation_engine,
            )
            error_type: str | None = None
            try:
                request = ProcessRequest(
                    source,
                    convert_to_markdown=True,
                    output_directory=case_output,
                    output_format=case.output_format,
                    improvement_mode=(
                        ImprovementMode.TRANSLATE
                        if case.translate and translation_engine == "local_ai"
                        else None
                    ),
                    target_language=(
                        target_language
                        if case.translate and translation_engine == "local_ai"
                        else None
                    ),
                    offline_translation_language=(
                        target_language
                        if case.translate and translation_engine == "argos"
                        else None
                    ),
                    pdf_page_range=page_range if extension == ".pdf" else None,
                    review_content=case.review_content,
                    review_structure=case.review_structure,
                    include_images=True,
                    epub_first_page_cover=(
                        extension == ".pdf" and case.output_format is OutputFormat.EPUB
                    ),
                    glossary=glossary,
                )
                processed = process_document(
                    request,
                    settings=(
                        settings
                        if _case_needs_ai(case)
                        or (case.translate and translation_engine == "local_ai")
                        else None
                    ),
                    on_stage=stages.append,
                    work_checkpoint_root=case_output / ".checkpoints",
                    epub_checkpoint_root=case_output / ".epub-checkpoints",
                )
                revision_created = processed.revision_draft is not None
                pdf_report = processed.pdf_quality_report
                translation_report = processed.translation_quality_report
                processed_pages = len(pdf_report.processed_pages) if pdf_report is not None else 0
                ocr_pages = len(pdf_report.ocr_pages) if pdf_report is not None else 0
                ocr_replaced_pages = (
                    len(pdf_report.ocr_replaced_pages) if pdf_report is not None else 0
                )
                low_confidence_pages = (
                    len(pdf_report.low_confidence_pages) if pdf_report is not None else 0
                )
                pdf_issues = len(pdf_report.issues) if pdf_report is not None else 0
                blocking_pdf_issues = (
                    sum(issue.blocking for issue in pdf_report.issues)
                    if pdf_report is not None
                    else 0
                )
                translation_issues = (
                    translation_report.total_issues if translation_report is not None else 0
                )
                if translation_report is not None:
                    translation_issue_kinds = {
                        kind.value: count
                        for kind, count in translation_report.issues_by_kind.items()
                    }
                preserved_translation_chunks = len(processed.preserved_translation_chunks)
                preserved_translation_chunk_numbers = processed.preserved_translation_chunks
                preserved_images = processed.preserved_images
                epub_chapters = processed.epub_chapters
                front_matter_blocks = processed.front_matter_blocks
                toc_blocks = processed.toc_blocks
                terminology_terms = processed.terminology_terms
                if processed.telemetry is not None:
                    stage_duration_ms = {
                        stage.stage.value: stage.duration_ms for stage in processed.telemetry.stages
                    }
                    batches = processed.telemetry.batches
                    local_ai_requests = batches.local_ai_requests
                    checkpoint_hits = batches.checkpoint_hits
                    checkpoint_misses = batches.checkpoint_misses
                    retries = batches.retries
                    validation_rejections = batches.validation_rejections
                    prompt_tokens = batches.prompt_tokens
                    output_tokens = batches.output_tokens
                    local_ai_wall_duration_ms = batches.wall_duration_ms
                    output_tokens_per_second = round(batches.output_tokens_per_second, 2)
                    ai_operations = {
                        operation.operation: {
                            "requests": operation.requests,
                            "prompt_tokens": operation.prompt_tokens,
                            "output_tokens": operation.output_tokens,
                            "wall_duration_ms": operation.wall_duration_ms,
                        }
                        for operation in batches.operations
                    }
                if processed.revision_draft is not None:
                    revision_changes = len(processed.revision_draft.changes)
                    revision_changes_by_kind = dict(
                        Counter(change.kind.value for change in processed.revision_draft.changes)
                    )
                    recommended_revision_changes = sum(
                        change.recommended_decision is RevisionDecision.ACCEPTED
                        for change in processed.revision_draft.changes
                    )
                    recommended_revision_changes_by_kind = dict(
                        Counter(
                            change.kind.value
                            for change in processed.revision_draft.changes
                            if change.recommended_decision is RevisionDecision.ACCEPTED
                        )
                    )
                    rejected_revision_changes = revision_changes - recommended_revision_changes
                    processed = apply_reviewed_revision(
                        processed,
                        processed.revision_draft.render(),
                    )
                    preserved_images = processed.preserved_images
                    epub_chapters = processed.epub_chapters
                result_path = processed.final_path
                epub_structure_issues = (
                    _validate_generated_output(result_path, case.output_format) or 0
                )
                output_sha256 = _file_sha256(result_path) if result_path.is_file() else None
                epub_structure_regressions = max(
                    0,
                    epub_structure_issues - source_epub_structure_issues,
                )
                if not stages or stages[-1] is not ProcessStage.COMPLETED:
                    raise RuntimeError("El flujo terminó sin anunciar su finalización.")
            except (ParsezenError, OSError, RuntimeError, ValueError, BadZipFile) as exc:
                error_type = type(exc).__name__
            elapsed = monotonic() - started
            passed = error_type is None
            quality_gate_passed = (
                passed
                and blocking_pdf_issues == 0
                and preserved_translation_chunks == 0
                and epub_structure_regressions == 0
            )
            results.append(
                LiveWorkflowResult(
                    source_index=source_index,
                    source_extension=extension,
                    case=case.name,
                    passed=passed,
                    elapsed_seconds=round(elapsed, 3),
                    stages=tuple(stage.value for stage in stages),
                    translation_engine=translation_engine if case.translate else None,
                    quality_gate_passed=quality_gate_passed,
                    output_extension=result_path.suffix.lower()
                    if result_path is not None
                    else None,
                    output_bytes=(
                        result_path.stat().st_size
                        if result_path is not None and result_path.is_file()
                        else None
                    ),
                    output_sha256=output_sha256,
                    revision_created=revision_created,
                    revision_changes=revision_changes,
                    recommended_revision_changes=recommended_revision_changes,
                    rejected_revision_changes=rejected_revision_changes,
                    revision_changes_by_kind=revision_changes_by_kind,
                    recommended_revision_changes_by_kind=(recommended_revision_changes_by_kind),
                    processed_pages=processed_pages,
                    ocr_pages=ocr_pages,
                    pdf_issues=pdf_issues,
                    blocking_pdf_issues=blocking_pdf_issues,
                    translation_issues=translation_issues,
                    translation_issue_kinds=translation_issue_kinds,
                    preserved_translation_chunks=preserved_translation_chunks,
                    preserved_translation_chunk_numbers=preserved_translation_chunk_numbers,
                    preserved_images=preserved_images,
                    epub_chapters=epub_chapters,
                    epub_structure_issues=epub_structure_issues,
                    source_epub_structure_issues=source_epub_structure_issues,
                    epub_structure_regressions=epub_structure_regressions,
                    ocr_replaced_pages=ocr_replaced_pages,
                    low_confidence_pages=low_confidence_pages,
                    front_matter_blocks=front_matter_blocks,
                    toc_blocks=toc_blocks,
                    terminology_terms=terminology_terms,
                    stage_duration_ms=stage_duration_ms,
                    planned_text_passes=planned_text_passes,
                    planned_ai_passes=planned_ai_passes,
                    local_ai_requests=local_ai_requests,
                    checkpoint_hits=checkpoint_hits,
                    checkpoint_misses=checkpoint_misses,
                    retries=retries,
                    validation_rejections=validation_rejections,
                    prompt_tokens=prompt_tokens,
                    output_tokens=output_tokens,
                    local_ai_wall_duration_ms=local_ai_wall_duration_ms,
                    output_tokens_per_second=output_tokens_per_second,
                    ai_operations=ai_operations,
                    error_type=error_type,
                    failed_stage=stages[-1].value if error_type is not None and stages else None,
                )
            )
            if on_results is not None:
                on_results(tuple(results))
        if _file_sha256(source) != source_hash:
            raise RuntimeError("La comprobación modificó un documento de origen.")
    return tuple(results)


def write_report(
    destination: Path,
    results: tuple[LiveWorkflowResult, ...],
    *,
    model: str,
) -> None:
    """Write a local report containing operational metadata but no document identity or text."""
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "model": model,
        "results": [asdict(result) for result in results],
        "privacy": "No contiene rutas, nombres, prompts, respuestas ni texto documental.",
    }
    _write_json_atomically(destination, payload)


def run_local_model_evaluation(
    sources: tuple[Path, ...],
    output_root: Path,
    models: tuple[str, ...],
    *,
    repetitions: int = 1,
    context_window: int | None = None,
    target_language: str = "Español",
    full_matrix: bool = False,
    page_range: PdfPageRange | None = None,
    glossary: tuple[GlossaryEntry, ...] = (),
    translation_engine: str = "local_ai",
    workflow_profile: str = "critical",
    evaluation_id: str | None = None,
    on_runs: Callable[[tuple[LocalEvaluationRun, ...]], None] | None = None,
) -> tuple[LocalEvaluationRun, ...]:
    """Compare explicitly installed local tags with identical options and repetitions.

    This function only inspects the installed model list and invokes the existing local
    workflow.  It intentionally has no model installation or download path.
    """
    if not models or len(models) > MAX_EVALUATION_MODELS:
        raise ValueError(
            f"La evaluación necesita entre 1 y {MAX_EVALUATION_MODELS} tags explícitos."
        )
    if len(set(models)) != len(models):
        raise ValueError("La evaluación no puede repetir un tag de modelo.")
    if isinstance(repetitions, bool) or not 1 <= repetitions <= MAX_EVALUATION_REPETITIONS:
        raise ValueError(f"Las repeticiones deben estar entre 1 y {MAX_EVALUATION_REPETITIONS}.")
    if context_window is not None and (isinstance(context_window, bool) or context_window < 1):
        raise ValueError("La ventana de contexto debe ser un entero positivo.")

    connection = discover_ollama(None)
    if connection.status is not OllamaStatus.READY:
        raise RuntimeError(connection.message or "Ollama no está preparado.")
    installed = {model.model_id: model for model in connection.models}
    missing = tuple(model for model in models if model not in installed)
    if missing:
        raise RuntimeError("Todos los tags de la evaluación deben estar instalados en Ollama.")
    if any(is_cloud_model_id(model) or model.casefold().endswith("-cloud") for model in models):
        raise RuntimeError("Los tags cloud no participan en una evaluación local.")

    # Resolve context once, then pass that exact value to every model.  This prevents
    # per-model recommendations from changing the comparison conditions.
    first_settings = select_live_settings(models[0], context_window=context_window)
    shared_context = first_settings.context_window
    settings_by_model = {models[0]: first_settings}
    settings_by_model.update(
        {model: select_live_settings(model, context_window=shared_context) for model in models[1:]}
    )
    run_id = _normalise_opaque_identity(evaluation_id)
    runs: list[LocalEvaluationRun] = []
    for model in models:
        settings = settings_by_model[model]
        for repetition in range(1, repetitions + 1):
            repetition_root = output_root / f"model-{models.index(model) + 1}" / f"run-{repetition}"
            results = run_live_workflows(
                sources,
                repetition_root,
                settings,
                target_language=target_language,
                full_matrix=full_matrix,
                page_range=page_range,
                glossary=glossary,
                translation_engine=translation_engine,
                workflow_profile=workflow_profile,
            )
            runs.extend(
                LocalEvaluationRun(
                    case_id=_evaluation_case_id(run_id, result.source_index, result.case),
                    model=model,
                    context_window=shared_context,
                    repetition=repetition,
                    result=result,
                )
                for result in results
            )
            if on_runs is not None:
                on_runs(tuple(runs))
    return tuple(runs)


def write_evaluation_report(
    destination: Path,
    runs: tuple[LocalEvaluationRun, ...],
    *,
    evaluation_id: str | None = None,
    options: dict[str, object] | None = None,
) -> None:
    """Atomically write only paired identities, output hashes and existing metrics."""
    run_id = _normalise_opaque_identity(evaluation_id)
    safe_case_ids = {run.case_id: _normalise_opaque_identity(run.case_id) for run in runs}
    safe_options = _safe_evaluation_options(options)
    payload = {
        "schema_version": EVALUATION_REPORT_SCHEMA_VERSION,
        "evaluation_id": run_id,
        "options": safe_options,
        "runs": [
            {
                "case_id": safe_case_ids[run.case_id],
                "model": run.model,
                "context_window": run.context_window,
                "repetition": run.repetition,
                "metrics": _safe_result_metrics(run.result),
            }
            for run in runs
        ],
        "paired_summary": _paired_summary(runs, case_ids=safe_case_ids),
        "repeat_summary": _repeat_summary(runs, case_ids=safe_case_ids),
        "privacy": "No contiene rutas, nombres, títulos, prompts, respuestas ni texto documental.",
    }
    _write_json_atomically(destination, payload)


def _safe_result_metrics(result: LiveWorkflowResult) -> dict[str, object]:
    """Remove source/case identity fields from one otherwise content-free result."""

    metrics = asdict(result)
    for field_name in ("source_index", "source_extension", "case"):
        metrics.pop(field_name, None)
    return metrics


def _paired_summary(
    runs: tuple[LocalEvaluationRun, ...],
    *,
    case_ids: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    grouped: dict[tuple[str, int], list[LocalEvaluationRun]] = {}
    for run in runs:
        grouped.setdefault((run.case_id, run.repetition), []).append(run)
    summary: list[dict[str, object]] = []
    for (case_id, repetition), group in sorted(grouped.items()):
        hashes = {
            run.model: run.result.output_sha256 for run in sorted(group, key=lambda x: x.model)
        }
        available_hashes = {value for value in hashes.values() if value is not None}
        summary.append(
            {
                "case_id": (case_ids or {}).get(case_id, case_id),
                "repetition": repetition,
                "models": tuple(hashes),
                "output_hashes": hashes,
                "same_output": (
                    len(hashes) >= 2
                    and len(available_hashes) == 1
                    and all(value is not None for value in hashes.values())
                ),
                "quality_gate_passed": {
                    run.model: run.result.quality_gate_passed
                    for run in sorted(group, key=lambda x: x.model)
                },
                "passed": {
                    run.model: run.result.passed for run in sorted(group, key=lambda x: x.model)
                },
            }
        )
    return summary


def _repeat_summary(
    runs: tuple[LocalEvaluationRun, ...],
    *,
    case_ids: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str], list[LocalEvaluationRun]] = {}
    for run in runs:
        grouped.setdefault((run.case_id, run.model), []).append(run)
    summary: list[dict[str, object]] = []
    for (case_id, model), group in sorted(grouped.items()):
        ordered = sorted(group, key=lambda run: run.repetition)
        hashes = [run.result.output_sha256 for run in ordered]
        available_hashes = {value for value in hashes if value is not None}
        summary.append(
            {
                "case_id": (case_ids or {}).get(case_id, case_id),
                "model": model,
                "repetitions": [run.repetition for run in ordered],
                "output_hashes": hashes,
                "stable_output": (
                    len(hashes) >= 2
                    and len(available_hashes) == 1
                    and all(value is not None for value in hashes)
                ),
                "all_quality_gates_passed": all(run.result.quality_gate_passed for run in ordered),
            }
        )
    return summary


def _safe_evaluation_options(options: dict[str, object] | None) -> dict[str, object]:
    """Keep only scalar benchmark controls; never serialize user-provided text options."""
    if not options:
        return {}
    allowed = {
        "context_window",
        "repetitions",
        "translation_engine",
        "workflow_profile",
        "full_matrix",
        "page_start",
        "page_end",
    }
    safe: dict[str, object] = {}
    for key in sorted(allowed):
        value = options.get(key)
        if isinstance(value, (str, int, bool, float)) or value is None:
            safe[key] = value
    return safe


def _new_opaque_identity() -> str:
    return secrets.token_hex(16)


def _normalise_opaque_identity(value: str | None) -> str:
    if value is None:
        return _new_opaque_identity()
    if re.fullmatch(r"[0-9a-fA-F]{32}", value):
        return value.lower()
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return digest[:32]


def _evaluation_case_id(evaluation_id: str, source_index: int, case: str) -> str:
    digest = hashlib.sha256()
    digest.update(evaluation_id.encode("ascii", "strict"))
    digest.update(b"\0")
    digest.update(str(source_index).encode("ascii"))
    digest.update(b"\0")
    digest.update(case.encode("utf-8"))
    return digest.hexdigest()[:32]


def _write_json_atomically(destination: Path, payload: dict[str, object]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def write_synthetic_pdf(destination: Path) -> None:
    """Create a 20-page textual PDF for a representative real-model validation."""
    opening_pages = (
        (
            "PRACTICAL GUIDE",
            (
                "This guide explains a careful local document workflow for ordinary readers.",
                "The first chapter keeps every idea, paragraph, and number in its original order.",
                "A reliable conversion should preserve meaning while improving useful structure.",
                "The reader must always know whether the process is active and what remains.",
                "Clear headings make a long book easier to navigate without rewriting its voice.",
                "Small conversion errors may be corrected only when their meaning is unambiguous.",
                "Private documents remain on this computer during every stage of the workflow.",
                "The final publication should open correctly and retain a coherent reading order.",
            ),
        ),
        (
            "FINAL CHECKS",
            (
                "The second chapter describes checks performed before a result is ready.",
                "Translation must cover every sentence and preserve the intent of the author.",
                "Content review may remove obvious conversion noise but never uncertain passages.",
                "Structure review may promote a visible title without changing any of its words.",
                "If a proposal is unsafe, the original fragment must remain complete.",
                "An electronic book needs navigation, readable chapters, and a sound package.",
                "The source document must remain unchanged even when the final review is approved.",
                "These checks provide a small repeatable signal before publishing a new version.",
            ),
        ),
    )
    additional_topics = (
        ("SOURCE INTEGRITY", "preserving every source statement and numerical value"),
        ("READING ORDER", "keeping columns, captions, and paragraphs in logical order"),
        ("IMAGE PRESERVATION", "retaining useful illustrations without duplicating them"),
        ("OCR CONFIDENCE", "using OCR only when it improves an unreliable text layer"),
        ("TABLE RECOVERY", "recovering tabular relationships without inventing missing cells"),
        ("PAGE GEOMETRY", "using page position as evidence rather than as document content"),
        ("FRONT MATTER", "distinguishing publication details from the main chapters"),
        ("CONTENTS MAP", "using the contents list as a navigation signal"),
        ("TERM CONSISTENCY", "translating recurring specialist terms consistently"),
        ("TRANSLATION FIDELITY", "preserving negation, conditions, names, and quantities"),
        ("SAFE CORRECTION", "correcting obvious defects while retaining uncertain wording"),
        ("CHAPTER HIERARCHY", "building a shallow and coherent heading outline"),
        ("REVIEW PRIORITY", "showing consequential risks before cosmetic suggestions"),
        ("CHECKPOINT RECOVERY", "resuming validated work after a safe interruption"),
        ("EPUB NAVIGATION", "making chapters reachable through a complete navigation map"),
        ("PACKAGE VALIDATION", "checking the final archive and all declared resources"),
        ("RESOURCE EFFICIENCY", "reducing repeated work without lowering output quality"),
        ("PUBLICATION REVIEW", "confirming that the final book remains complete and readable"),
    )
    additional_pages = tuple(
        (
            title,
            (
                f"Page {page_number} focuses on {focus}.",
                (
                    "The workflow records evidence for this stage without storing "
                    "private text in logs."
                ),
                "Automated decisions must remain conservative when the source is incomplete.",
                (
                    f"A distinct reference value, {page_number * 17}, helps detect "
                    "accidental omissions."
                ),
                (
                    "Warning OCR: this synthetic sentence deliberately exercises the "
                    "targeted local review path."
                    if page_number == 3
                    else "Reviewers should be able to locate any warning in the final document."
                ),
                "A successful transformation preserves meaning before improving presentation.",
                "Repeated work should be avoided only after the earlier result has been validated.",
                "The published EPUB must remain usable on ordinary reading applications.",
            ),
        )
        for page_number, (title, focus) in enumerate(additional_topics, start=3)
    )
    pages = opening_pages + additional_pages
    if len(pages) != SYNTHETIC_PAGE_COUNT:
        raise RuntimeError("La muestra sintética no contiene exactamente 20 páginas.")
    _write_text_pdf(destination, pages)


def _case_needs_ai(case: LiveWorkflowCase) -> bool:
    return case.review_content or case.review_structure


def _workflow_cases(
    workflow_profile: str,
    *,
    full_matrix: bool,
) -> tuple[LiveWorkflowCase, ...]:
    if full_matrix:
        return full_matrix_cases()
    return {
        "critical": CRITICAL_CASES,
        "translation": TRANSLATION_CASES,
        "review": REVIEW_CASES,
        "translation-review": TRANSLATION_REVIEW_CASES,
    }[workflow_profile]


def _planned_passes(case: LiveWorkflowCase, translation_engine: str) -> tuple[int, int]:
    translated = int(case.translate)
    content_review = int(case.review_content)
    structure_review = int(case.review_structure)
    text_passes = translated + content_review + structure_review
    ai_translation = int(case.translate and translation_engine == "local_ai")
    ai_passes = ai_translation + content_review + structure_review
    return text_passes, ai_passes


def _parse_glossary_arguments(values: list[str] | None) -> tuple[GlossaryEntry, ...]:
    entries: list[GlossaryEntry] = []
    for value in values or ():
        source, separator, target = value.partition("=")
        if not separator or not source.strip() or not target.strip():
            raise ValueError("Cada término debe usar el formato ORIGEN=DESTINO.")
        entries.append(GlossaryEntry(source, target))
    return validate_glossary(tuple(entries))


def _validate_generated_output(path: Path, output_format: OutputFormat) -> int:
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError("El flujo no publicó un resultado utilizable.")
    if output_format is OutputFormat.MARKDOWN:
        if path.suffix.lower() != ".md" or not path.read_text(encoding="utf-8").strip():
            raise RuntimeError("El Markdown generado no es utilizable.")
        return 0
    if path.suffix.lower() != ".epub":
        raise RuntimeError("El flujo no publicó un EPUB.")
    inspect_epub_package(path)
    with ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("El EPUB contiene una entrada dañada.")
        names = archive.namelist()
        if (
            not names
            or names[0] != "mimetype"
            or archive.getinfo("mimetype").compress_type != ZIP_STORED
            or archive.read("mimetype") != b"application/epub+zip"
        ):
            raise RuntimeError("El EPUB no tiene la estructura mínima esperada.")
        structure_issues = 0
        for name in names:
            if not name.lower().endswith((".xhtml", ".html")):
                continue
            root = ElementTree.fromstring(archive.read(name))
            heading_levels: list[int] = []
            for element in root.iter():
                tag = element.tag.rsplit("}", 1)[-1].lower()
                if tag not in {
                    "h1",
                    "h2",
                    "h3",
                    "h4",
                    "h5",
                    "h6",
                }:
                    continue
                heading_levels.append(int(tag[1]))
                visible = " ".join("".join(element.itertext()).split())
                if (
                    len(visible) > MAX_EPUB_HEADING_CHARACTERS
                    or len(re.findall(r"\S+", visible)) > MAX_EPUB_HEADING_WORDS
                ):
                    structure_issues += 1
            if name != "EPUB/nav.xhtml" and heading_levels:
                if min(heading_levels) > 2:
                    structure_issues += 1
                structure_issues += sum(
                    current > previous + 1
                    for previous, current in zip(
                        heading_levels,
                        heading_levels[1:],
                        strict=False,
                    )
                )
        return structure_issues


def _write_text_pdf(
    destination: Path,
    pages: Iterable[tuple[str, tuple[str, ...]]],
) -> None:
    page_list = tuple(pages)
    page_count = len(page_list)
    regular_font = 3 + page_count
    bold_font = regular_font + 1
    first_stream = bold_font + 1
    page_references = " ".join(f"{number} 0 R" for number in range(3, 3 + page_count))
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{page_references}] /Count {page_count} >>".encode(),
    ]
    for index in range(page_count):
        stream_reference = first_stream + index
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {regular_font} 0 R /F2 {bold_font} 0 R >> >> "
                f"/Contents {stream_reference} 0 R >>"
            ).encode()
        )
    objects.extend(
        (
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
        )
    )
    for title, lines in page_list:
        commands = [
            "BT",
            "/F2 18 Tf",
            "72 740 Td",
            f"({_pdf_text(title)}) Tj",
            "ET",
            "BT",
            "/F1 11 Tf",
            "72 705 Td",
            "18 TL",
        ]
        for line in lines:
            commands.extend((f"({_pdf_text(line)}) Tj", "T*"))
        commands.append("ET")
        objects.append(_pdf_stream("\n".join(commands).encode("ascii")))
    _write_pdf_objects(destination, objects)


def _pdf_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _pdf_stream(content: bytes) -> bytes:
    return b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream"


def _write_pdf_objects(destination: Path, objects: list[bytes]) -> None:
    payload = bytearray(b"%PDF-1.4\n%Parsezen\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{number} 0 obj\n".encode())
        payload.extend(obj)
        payload.extend(b"\nendobj\n")
    xref_offset = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode())
    payload.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode()
    )
    destination.write_bytes(payload)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Comprueba un flujo completo con Ollama local sin guardar texto documental.")
    )
    parser.add_argument(
        "sources",
        nargs="*",
        type=Path,
        help="Muestras locales; sin argumentos se crea un PDF sintético de 20 páginas.",
    )
    parser.add_argument("--model", help="Modelo instalado; por defecto usa el elegido en la app.")
    parser.add_argument(
        "--models",
        nargs="+",
        metavar="TAG",
        help="Tags instalados que se compararán bajo las mismas opciones.",
    )
    parser.add_argument(
        "--repetitions",
        type=int,
        default=1,
        help="Repeticiones idénticas por modelo (1–10; el informe las empareja).",
    )
    parser.add_argument("--context-window", type=int, help="Ventana de contexto de la prueba.")
    parser.add_argument("--target-language", default="Español")
    parser.add_argument(
        "--translation-engine",
        choices=("argos", "local_ai"),
        default="local_ai",
        help=(
            "Motor usado por los casos que traducen; IA local es el valor predeterminado y Argos "
            "solo se usa al pedirlo explícitamente."
        ),
    )
    parser.add_argument(
        "--profile",
        choices=tuple(sorted(WORKFLOW_PROFILES)),
        default="critical",
        help=(
            "Flujo completo, traducción aislada o comparaciones directas de revisión y traducción."
        ),
    )
    parser.add_argument(
        "--glossary",
        action="append",
        metavar="ORIGEN=DESTINO",
        help="Fija una equivalencia terminológica; se puede repetir sin exponerla en el informe.",
    )
    parser.add_argument(
        "--pages",
        nargs=2,
        type=int,
        metavar=("INICIO", "FIN"),
        help="Intervalo inclusivo para todas las muestras PDF.",
    )
    parser.add_argument(
        "--full-matrix",
        action="store_true",
        help="Ejecuta 16 casos sobre un PDF de 20 páginas; puede tardar bastante.",
    )
    parser.add_argument("--output-directory", type=Path)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    return parser


def _run_from_arguments(arguments: argparse.Namespace) -> int:
    if arguments.models is not None and arguments.model is not None:
        raise ValueError("Usa --model o --models, no ambos.")
    with TemporaryDirectory(prefix="parsezen-real-") as temporary_name:
        temporary = Path(temporary_name)
        if arguments.sources:
            sources = tuple(arguments.sources)
        else:
            synthetic = temporary / "synthetic-workflow.pdf"
            write_synthetic_pdf(synthetic)
            sources = (synthetic,)
        output_root = arguments.output_directory or (temporary / "outputs")

        def persist_results(partial: tuple[LiveWorkflowResult, ...]) -> None:
            write_report(
                arguments.report,
                partial,
                model=settings.model or "none",
            )

        page_range = PdfPageRange(*arguments.pages) if arguments.pages is not None else None
        glossary = _parse_glossary_arguments(arguments.glossary)
        if arguments.models is not None:
            evaluation_id = _new_opaque_identity()

            def persist_evaluation(partial: tuple[LocalEvaluationRun, ...]) -> None:
                write_evaluation_report(
                    arguments.report,
                    partial,
                    evaluation_id=evaluation_id,
                    options={
                        "context_window": partial[0].context_window if partial else None,
                        "repetitions": arguments.repetitions,
                        "translation_engine": arguments.translation_engine,
                        "workflow_profile": arguments.profile,
                        "full_matrix": arguments.full_matrix,
                        "page_start": page_range.first_page if page_range is not None else None,
                        "page_end": page_range.last_page if page_range is not None else None,
                    },
                )

            runs = run_local_model_evaluation(
                sources,
                output_root,
                tuple(arguments.models),
                repetitions=arguments.repetitions,
                context_window=arguments.context_window,
                target_language=arguments.target_language,
                full_matrix=arguments.full_matrix,
                page_range=page_range,
                glossary=glossary,
                translation_engine=arguments.translation_engine,
                workflow_profile=arguments.profile,
                evaluation_id=evaluation_id,
                on_runs=persist_evaluation,
            )
            write_evaluation_report(
                arguments.report,
                runs,
                evaluation_id=evaluation_id,
                options={
                    "context_window": runs[0].context_window if runs else arguments.context_window,
                    "repetitions": arguments.repetitions,
                    "translation_engine": arguments.translation_engine,
                    "workflow_profile": arguments.profile,
                    "full_matrix": arguments.full_matrix,
                    "page_start": page_range.first_page if page_range is not None else None,
                    "page_end": page_range.last_page if page_range is not None else None,
                },
            )
            for run in runs:
                state = (
                    "OK"
                    if run.result.quality_gate_passed
                    else ("REVISAR" if run.result.passed else "FALLO")
                )
                print(
                    f"{state} modelo {run.model} · repetición {run.repetition} · "
                    f"muestra {run.result.source_index} · {run.result.case} · "
                    f"{run.result.elapsed_seconds:.1f} s"
                )
            print(f"Informe comparativo local sin contenido documental: {arguments.report}")
            return 0 if all(run.result.quality_gate_passed for run in runs) else 1

        settings = select_live_settings(arguments.model, context_window=arguments.context_window)
        results = run_live_workflows(
            sources,
            output_root,
            settings,
            target_language=arguments.target_language,
            full_matrix=arguments.full_matrix,
            page_range=page_range,
            glossary=glossary,
            translation_engine=arguments.translation_engine,
            workflow_profile=arguments.profile,
            on_results=persist_results,
        )
    for result in results:
        state = "OK" if result.quality_gate_passed else ("REVISAR" if result.passed else "FALLO")
        detail = (
            f" · {result.error_type} en {result.failed_stage or 'inicio'}"
            if not result.passed
            else (
                " · "
                f"{result.pdf_issues} incidencias PDF, "
                f"{result.translation_issues} de traducción y "
                f"{result.preserved_translation_chunks} fragmentos conservados, "
                f"{result.epub_structure_regressions} regresiones estructurales EPUB"
                if not result.quality_gate_passed
                else ""
            )
        )
        print(
            f"{state} muestra {result.source_index} · {result.case} · "
            f"{result.elapsed_seconds:.1f} s{detail}"
        )
    print(f"Informe local sin contenido documental: {arguments.report}")
    return 0 if all(result.quality_gate_passed for result in results) else 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    arguments = _parser().parse_args(argv)
    try:
        return _run_from_arguments(arguments)
    except (ParsezenError, OSError, RuntimeError, ValueError) as exc:
        print(f"No se pudo completar la comprobación real: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
