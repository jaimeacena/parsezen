"""Privacy-safe workload and duration contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from parsezen.domain.jobs import DocumentFormat, TranslationMethod


@dataclass(frozen=True, slots=True)
class WorkloadProfile:
    """Content-free characteristics that materially affect processing time."""

    source_format: DocumentFormat
    output_format: DocumentFormat
    work_units: int
    force_pdf_ocr: bool
    translation_method: TranslationMethod | None
    refinement_enabled: bool
    structure_enabled: bool
    include_images: bool
    preserve_styles: bool
    model_key: str | None = None

    def __post_init__(self) -> None:
        if self.work_units < 1:
            raise ValueError("Workload units must be positive.")
        uses_local_ai = (
            self.translation_method is TranslationMethod.LOCAL_AI
            or self.refinement_enabled
            or self.structure_enabled
        )
        if not uses_local_ai and self.model_key is not None:
            raise ValueError("Only local-AI workloads can identify a model family.")

    @property
    def compatibility_key(self) -> tuple[object, ...]:
        """Return the fields that must match before timings are comparable."""

        return (
            self.source_format,
            self.output_format,
            self.force_pdf_ocr,
            self.translation_method,
            self.refinement_enabled,
            self.structure_enabled,
            self.include_images,
            self.preserve_styles,
            self.model_key,
        )


@dataclass(frozen=True, slots=True)
class ProcessingMetric:
    """One completed automatic run without paths or document content."""

    profile: WorkloadProfile
    duration_ms: int
    created_at: datetime

    def __post_init__(self) -> None:
        if self.duration_ms < 1:
            raise ValueError("A processing duration must be positive.")


@dataclass(frozen=True, slots=True)
class DurationEstimate:
    """Bounded estimate for automatic processing, excluding manual review."""

    lower_seconds: int
    likely_seconds: int
    upper_seconds: int
    sample_count: int = 0

    def __post_init__(self) -> None:
        if not 1 <= self.lower_seconds <= self.likely_seconds <= self.upper_seconds:
            raise ValueError("Duration estimate bounds must be positive and ordered.")
        if self.sample_count < 0:
            raise ValueError("Estimate sample count cannot be negative.")

    @property
    def confidence_label(self) -> str:
        if self.sample_count >= 6:
            return "Calibrada en este equipo"
        if self.sample_count >= 3:
            return "Basada en trabajos similares"
        if self.sample_count:
            return "Aprendiendo de este equipo"
        return "Estimación inicial"
