"""Explain one planned run and estimate its automatic duration."""

from __future__ import annotations

import hashlib
import math
import statistics
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from parsezen.domain.estimates import (
    DurationEstimate,
    ProcessingMetric,
    WorkloadProfile,
)
from parsezen.domain.jobs import (
    DocumentFormat,
    DocumentJob,
    TranslationMethod,
)
from parsezen.pdf_conversion import PdfPageRange, resolve_pdf_page_range
from parsezen.processing import ProcessRequest, ProcessTelemetry
from parsezen.settings import AppSettings


class PreflightSeverity(StrEnum):
    INFO = "info"
    ATTENTION = "attention"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return {
            PreflightSeverity.INFO: 0,
            PreflightSeverity.ATTENTION: 1,
            PreflightSeverity.HIGH: 2,
        }[self]


@dataclass(frozen=True, slots=True)
class PreflightFinding:
    code: str
    severity: PreflightSeverity
    title: str
    detail: str


@dataclass(frozen=True, slots=True)
class DocumentPreflight:
    job_id: str
    workload_label: str
    estimate: DurationEstimate
    expected_reviews: tuple[str, ...]
    findings: tuple[PreflightFinding, ...]
    early_check_recommended: bool = False

    @property
    def severity(self) -> PreflightSeverity:
        return max(
            (finding.severity for finding in self.findings),
            key=lambda severity: severity.rank,
            default=PreflightSeverity.INFO,
        )


@dataclass(frozen=True, slots=True)
class QueuePreflight:
    documents: tuple[DocumentPreflight, ...]
    estimate: DurationEstimate

    @property
    def requires_confirmation(self) -> bool:
        """Interrupt only for material risk or a long unattended run."""

        return self.estimate.upper_seconds >= 30 * 60 or any(
            item.severity is PreflightSeverity.HIGH for item in self.documents
        )


@dataclass(frozen=True, slots=True)
class RuntimeEstimate:
    """Remaining automatic time and a short explanation of its current basis."""

    label: str
    explanation: str
    estimate: DurationEstimate | None = None
    overdue: bool = False


def build_workload_profile(
    job: DocumentJob,
    request: ProcessRequest,
    settings: AppSettings,
) -> tuple[WorkloadProfile, str]:
    """Describe comparable work without retaining a path or any document text."""

    if job.source.format is DocumentFormat.PDF:
        requested = request.pdf_page_range or PdfPageRange(1, 2_147_483_647)
        resolved = resolve_pdf_page_range(request.source_path, requested)
        work_units = resolved.last_page - resolved.first_page + 1
        workload_label = f"{work_units} {'página' if work_units == 1 else 'páginas'}"
    else:
        divisor = {
            DocumentFormat.TEXT: 3_500,
            DocumentFormat.MARKDOWN: 4_000,
            DocumentFormat.DOCX: 16_000,
            DocumentFormat.EPUB: 24_000,
        }[job.source.format]
        work_units = max(1, math.ceil(max(job.source.size_bytes, 1) / divisor))
        workload_label = (
            "Documento breve"
            if work_units <= 10
            else "Documento medio"
            if work_units <= 60
            else "Documento extenso"
        )

    translation_method = (
        job.configuration.translation.method if job.configuration.translation.enabled else None
    )
    model_key = (
        _model_key(settings.model)
        if translation_method is TranslationMethod.LOCAL_AI
        or job.configuration.refinement.enabled
        or job.configuration.structure.enabled
        else None
    )
    profile = WorkloadProfile(
        source_format=job.source.format,
        output_format=job.configuration.output.format,
        work_units=work_units,
        force_pdf_ocr=job.configuration.force_pdf_ocr,
        translation_method=translation_method,
        refinement_enabled=job.configuration.refinement.enabled,
        structure_enabled=job.configuration.structure.enabled,
        include_images=job.configuration.output.include_images,
        preserve_styles=job.configuration.output.preserve_styles,
        model_key=model_key,
    )
    return profile, workload_label


def analyze_preflight(
    job: DocumentJob,
    request: ProcessRequest,
    settings: AppSettings,
    metrics: tuple[ProcessingMetric, ...] = (),
) -> tuple[DocumentPreflight, WorkloadProfile]:
    profile, workload_label = build_workload_profile(job, request, settings)
    early_check_recommended = should_run_early_check(profile)
    estimate = estimate_duration(profile, metrics)
    if early_check_recommended:
        estimate = _include_early_check_time(estimate, profile.work_units)
    findings = _findings(job, profile, early_check_recommended=early_check_recommended)
    return (
        DocumentPreflight(
            job_id=job.id,
            workload_label=workload_label,
            estimate=estimate,
            expected_reviews=_expected_reviews(job),
            findings=findings,
            early_check_recommended=early_check_recommended,
        ),
        profile,
    )


def combine_preflights(documents: tuple[DocumentPreflight, ...]) -> QueuePreflight:
    if not documents:
        empty = DurationEstimate(1, 1, 1)
        return QueuePreflight((), empty)
    return QueuePreflight(
        documents,
        DurationEstimate(
            lower_seconds=sum(item.estimate.lower_seconds for item in documents),
            likely_seconds=sum(item.estimate.likely_seconds for item in documents),
            upper_seconds=sum(item.estimate.upper_seconds for item in documents),
            sample_count=min(item.estimate.sample_count for item in documents),
        ),
    )


def estimate_duration(
    profile: WorkloadProfile,
    metrics: tuple[ProcessingMetric, ...],
) -> DurationEstimate:
    """Use robust local rates when available and a conservative baseline otherwise."""

    compatible = tuple(
        metric
        for metric in metrics
        if metric.profile.compatibility_key == profile.compatibility_key
        and 0.2 <= metric.profile.work_units / profile.work_units <= 5
    )
    rates = tuple(metric.duration_ms / 1000 / metric.profile.work_units for metric in compatible)
    baseline_rate = _baseline_seconds_per_unit(profile)
    fixed_overhead = 8.0
    if rates:
        observed = statistics.median(rates)
        baseline_weight = max(0, 3 - len(rates))
        likely_rate = (observed * len(rates) + baseline_rate * baseline_weight) / (
            len(rates) + baseline_weight
        )
    else:
        likely_rate = baseline_rate
    likely = max(15, round(fixed_overhead + likely_rate * profile.work_units))

    if len(rates) >= 4:
        ordered = sorted(rates)
        low_rate = ordered[max(0, len(ordered) // 4 - 1)]
        high_rate = ordered[min(len(ordered) - 1, math.ceil(len(ordered) * 0.75))]
        lower = max(10, round((fixed_overhead + low_rate * profile.work_units) * 0.85))
        upper = max(likely, round((fixed_overhead + high_rate * profile.work_units) * 1.2))
    elif rates:
        lower = max(10, round(likely * 0.65))
        upper = max(likely, round(likely * 1.55))
    else:
        lower = max(10, round(likely * 0.55))
        upper = max(likely, round(likely * 1.8))
    return DurationEstimate(lower, likely, upper, len(rates))


def processing_metric(
    profile: WorkloadProfile,
    telemetry: ProcessTelemetry,
    *,
    created_at: datetime,
) -> ProcessingMetric | None:
    if telemetry.total_duration_ms < 1:
        return None
    return ProcessingMetric(profile, telemetry.total_duration_ms, created_at)


def format_duration(seconds: int) -> str:
    seconds = max(1, seconds)
    if seconds < 60:
        return "menos de 1 min"
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, remainder = divmod(minutes, 60)
    if remainder < 5:
        return f"{hours} h"
    return f"{hours} h {remainder} min"


def format_duration_range(estimate: DurationEstimate) -> str:
    lower = format_duration(estimate.lower_seconds)
    upper = format_duration(estimate.upper_seconds)
    return lower if lower == upper else f"{lower}–{upper}"


def should_run_early_check(profile: WorkloadProfile) -> bool:
    """Limit representative work to PDFs where it can prevent a material wait."""

    if profile.source_format is not DocumentFormat.PDF:
        return False
    uncertain = (
        profile.force_pdf_ocr
        or profile.translation_method is TranslationMethod.LOCAL_AI
        or profile.output_format is DocumentFormat.EPUB
    )
    return profile.work_units >= 120 or (uncertain and profile.work_units >= 60)


def _include_early_check_time(
    estimate: DurationEstimate,
    work_units: int,
) -> DurationEstimate:
    """Account for three real sample pages without pretending they are free."""

    sample_ratio = min(0.15, 3 / max(work_units, 1))
    lower_overhead = max(20, round(estimate.lower_seconds * sample_ratio * 0.6))
    likely_overhead = max(45, round(estimate.likely_seconds * sample_ratio))
    upper_overhead = max(90, round(estimate.upper_seconds * sample_ratio * 1.4))
    return DurationEstimate(
        estimate.lower_seconds + lower_overhead,
        estimate.likely_seconds + likely_overhead,
        estimate.upper_seconds + upper_overhead,
        estimate.sample_count,
    )


def estimate_remaining_time(
    forecast: DurationEstimate,
    *,
    elapsed_seconds: float,
    stage_elapsed_seconds: float | None = None,
    progress_current: int = 0,
    progress_total: int = 0,
) -> RuntimeEstimate:
    """Count down the plan and widen it only when real stage progress justifies it."""

    elapsed = max(0, round(elapsed_seconds))
    if elapsed > forecast.upper_seconds:
        return RuntimeEstimate(
            "Más tiempo del previsto",
            "La duración ya supera el límite superior del plan inicial. "
            "Parsezen sigue procesando y conserva los checkpoints seguros.",
            overdue=True,
        )

    lower = max(1, forecast.lower_seconds - elapsed)
    likely = max(1, forecast.likely_seconds - elapsed)
    upper = max(likely, forecast.upper_seconds - elapsed)
    explanation = (
        f"Se han descontado {format_duration(max(1, elapsed))} del plan inicial. "
        f"{forecast.confidence_label}."
    )
    if (
        stage_elapsed_seconds is not None
        and stage_elapsed_seconds >= 10
        and progress_total >= 2
        and 0 < progress_current < progress_total
    ):
        observed_remaining = max(
            1,
            round(stage_elapsed_seconds * (progress_total - progress_current) / progress_current),
        )
        lower = max(1, min(lower, round(observed_remaining * 0.75)))
        upper = max(upper, round(observed_remaining * 1.35))
        likely = max(lower, min(upper, round((likely + observed_remaining) / 2)))
        explanation = (
            f"Actualizada con el avance real de la etapa actual "
            f"({progress_current} de {progress_total}). "
            "Las fases posteriores conservan el margen del plan inicial."
        )
    remaining = DurationEstimate(lower, likely, upper, forecast.sample_count)
    return RuntimeEstimate(
        f"Quedan aprox. {format_duration_range(remaining)}",
        explanation,
        remaining,
    )


def _model_key(model: str | None) -> str | None:
    if not model:
        return None
    return hashlib.sha256(model.strip().casefold().encode("utf-8")).hexdigest()[:16]


def _baseline_seconds_per_unit(profile: WorkloadProfile) -> float:
    seconds = {
        DocumentFormat.PDF: 1.4,
        DocumentFormat.DOCX: 0.7,
        DocumentFormat.EPUB: 0.7,
        DocumentFormat.MARKDOWN: 0.25,
        DocumentFormat.TEXT: 0.2,
    }[profile.source_format]
    if profile.force_pdf_ocr:
        seconds += 8.0
    if profile.translation_method is TranslationMethod.LOCAL_AI:
        seconds += 18.0
    elif profile.translation_method is TranslationMethod.OFFLINE:
        seconds += 2.0
    if profile.refinement_enabled:
        seconds += 16.0
    if profile.structure_enabled:
        seconds += 4.0
    if profile.output_format is DocumentFormat.EPUB:
        seconds += 0.7
    if profile.include_images and profile.source_format is DocumentFormat.PDF:
        seconds += 0.6
    return seconds


def _expected_reviews(job: DocumentJob) -> tuple[str, ...]:
    reviews: list[str] = []
    if job.source.format is DocumentFormat.PDF:
        reviews.append("OCR o conversión si se detectan páginas dudosas")
    if job.configuration.translation.enabled and job.configuration.translation.manual_review:
        reviews.append("traducción")
    if job.configuration.refinement.enabled and job.configuration.refinement.manual_review:
        reviews.append("corrección")
    if job.configuration.structure.enabled and job.configuration.structure.manual_review:
        reviews.append("estructura")
    if job.configuration.output.format is DocumentFormat.EPUB:
        reviews.append("editor EPUB final")
    return tuple(reviews)


def _findings(
    job: DocumentJob,
    profile: WorkloadProfile,
    *,
    early_check_recommended: bool,
) -> tuple[PreflightFinding, ...]:
    findings: list[PreflightFinding] = []
    if profile.force_pdf_ocr:
        severity = (
            PreflightSeverity.HIGH if profile.work_units >= 50 else PreflightSeverity.ATTENTION
        )
        findings.append(
            PreflightFinding(
                "forced-ocr",
                severity,
                "OCR completo activado",
                "Se reconocerá cada página y aumentarán el tiempo y la revisión posterior.",
            )
        )
    if profile.source_format is DocumentFormat.PDF and profile.output_format is DocumentFormat.EPUB:
        findings.append(
            PreflightFinding(
                "pdf-to-epub",
                PreflightSeverity.ATTENTION,
                "El diseño se reconstruirá",
                "Un PDF no contiene estructura EPUB; revisa capítulos, tablas y elementos visuales "
                "en el editor final.",
            )
        )
    if profile.translation_method is TranslationMethod.LOCAL_AI and profile.work_units >= 80:
        findings.append(
            PreflightFinding(
                "long-local-ai",
                PreflightSeverity.ATTENTION,
                "Trabajo local prolongado",
                "La traducción se ejecutará íntegramente en este equipo y puede durar "
                "varias horas.",
            )
        )
    if (
        profile.translation_method is TranslationMethod.LOCAL_AI
        and not job.configuration.translation.manual_review
    ):
        findings.append(
            PreflightFinding(
                "translation-without-review",
                PreflightSeverity.HIGH,
                "Traducción sin revisión de contenido",
                "Las propuestas de traducción no se comprobarán una a una antes del resultado.",
            )
        )
    if profile.refinement_enabled and not job.configuration.refinement.manual_review:
        findings.append(
            PreflightFinding(
                "refinement-without-review",
                PreflightSeverity.HIGH,
                "Corrección sin revisión de contenido",
                "Las correcciones propuestas se aplicarán sin una comparación manual previa.",
            )
        )
    if profile.translation_method is TranslationMethod.OFFLINE and profile.refinement_enabled:
        findings.append(
            PreflightFinding(
                "two-language-passes",
                PreflightSeverity.INFO,
                "Dos pasadas sobre el texto",
                "Primero se traducirá y después se corregirá el resultado.",
            )
        )
    elif profile.translation_method is TranslationMethod.LOCAL_AI and profile.refinement_enabled:
        findings.append(
            PreflightFinding(
                "combined-language-pass",
                PreflightSeverity.INFO,
                "Una pasada combinada",
                "La IA local traducirá y corregirá en una sola transformación validada.",
            )
        )
    if early_check_recommended:
        findings.append(
            PreflightFinding(
                "early-check",
                PreflightSeverity.INFO,
                "Comprobación temprana",
                "Antes del recorrido completo se comprobarán tres páginas representativas. "
                "Si no aparece un riesgo material, el procesamiento continuará automáticamente.",
            )
        )
    return tuple(findings)
