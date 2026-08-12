"""Content-free summaries for early checks and completed document outcomes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EarlyCheckReport:
    """What a bounded representative check observed before a long PDF run."""

    sampled_pages: tuple[int, ...]
    ocr_pages: int = 0
    low_confidence_pages: int = 0
    conversion_issues: int = 0
    translation_issues: int = 0
    preserved_segments: int = 0
    warning_pages: int = 0
    blocking_reasons: tuple[str, ...] = ()
    duration_seconds: int = 0

    @property
    def blocking(self) -> bool:
        return bool(self.blocking_reasons)


@dataclass(frozen=True, slots=True)
class OutcomeSummary:
    """Small privacy-safe account of one completed transformation."""

    output_format: str
    operations: tuple[str, ...] = ()
    processed_pages: int = 0
    ocr_pages: int = 0
    preserved_images: int = 0
    chapters: int = 0
    conversion_issues: int = 0
    translation_issues: int = 0
    preserved_segments: int = 0
    review_units: int = 0
    review_changes: int = 0
    review_edits: int = 0
    review_originals: int = 0
    editor_completed: bool = False
    manual_review_expected: bool = False
    integrity_verified: bool = False
    integrity_checks: int = 0
    integrity_warnings: int = 0
    duration_seconds: int | None = None
    estimate_lower_seconds: int | None = None
    estimate_upper_seconds: int | None = None
    early_check_pages: int = 0
    early_check_warnings: int = 0
    ai_review_recommended: bool = False
    ai_review_blocks: int = 0
    ai_review_signals: int = 0
