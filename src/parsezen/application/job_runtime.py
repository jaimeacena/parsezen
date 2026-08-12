"""Ephemeral physical execution data that does not duplicate domain job state."""

from __future__ import annotations

from dataclasses import dataclass

from parsezen.pdf_conversion import PdfPageRange
from parsezen.processing import ProcessResult, ProcessStage


@dataclass(slots=True)
class JobRuntime:
    """Transient worker details keyed externally by ``DocumentJob.id``.

    Status, errors, source, configuration and order deliberately remain in the domain job.
    This record only contains values that exist while a physical worker is active or whose
    complete result has not yet been discarded from memory.
    """

    pdf_page_range: PdfPageRange | None = None
    result: ProcessResult | None = None
    stage: ProcessStage | None = None
    progress_current: int = 0
    progress_total: int = 0
    stages_seen: list[ProcessStage] | None = None
    started_at: float | None = None
    stage_started_at: float | None = None
    finished_at: float | None = None

    def reset_for_run(self) -> None:
        self.result = None
        self.stage = None
        self.progress_current = 0
        self.progress_total = 0
        self.stages_seen = []
        self.started_at = None
        self.stage_started_at = None
        self.finished_at = None
