"""Qt-free batch state and whole-queue validation."""

from __future__ import annotations

import shutil
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from parsezen.errors import ParsezenError
from parsezen.pdf_conversion import PdfPageRange
from parsezen.processing import (
    ProcessRequest,
    ProcessResult,
    ProcessStage,
    validate_process_request,
)
from parsezen.settings import AppSettings


class BatchStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"
    REVIEW_PENDING = "review_pending"
    FINALIZING = "finalizing"


@dataclass(slots=True)
class BatchEntry:
    path: Path
    status: BatchStatus = BatchStatus.PENDING
    pdf_page_range: PdfPageRange | None = None
    result: ProcessResult | None = None
    error: str | None = None
    stage: ProcessStage | None = None
    progress_current: int = 0
    progress_total: int = 0
    stages_seen: list[ProcessStage] | None = None
    started_at: float | None = None
    stage_started_at: float | None = None
    finished_at: float | None = None
    request: ProcessRequest | None = None
    settings: AppSettings | None = None


class BatchQueue:
    """The ordered mutable queue, independent from its Qt presentation."""

    def __init__(self, entries: Iterable[BatchEntry] = ()) -> None:
        self._entries = list(entries)

    def __bool__(self) -> bool:
        return bool(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[BatchEntry]:
        return iter(self._entries)

    def __getitem__(self, index: int) -> BatchEntry:
        return self._entries[index]

    def index(self, entry: BatchEntry) -> int:
        return self._entries.index(entry)

    def append(self, entry: BatchEntry) -> None:
        self._entries.append(entry)

    def extend(self, entries: Iterable[BatchEntry]) -> None:
        self._entries.extend(entries)

    def insert(self, index: int, entry: BatchEntry) -> None:
        self._entries.insert(index, entry)

    def pop(self, index: int = -1) -> BatchEntry:
        return self._entries.pop(index)

    def remove(self, entry: BatchEntry) -> None:
        self._entries.remove(entry)

    def clear(self) -> None:
        self._entries.clear()

    def replace(self, entries: Iterable[BatchEntry]) -> None:
        self._entries = list(entries)

    def find(self, path: Path) -> BatchEntry | None:
        return next((entry for entry in self._entries if entry.path == path), None)

    def next_pending(self) -> BatchEntry | None:
        return next(
            (entry for entry in self._entries if entry.status is BatchStatus.PENDING),
            None,
        )

    @property
    def paths(self) -> tuple[Path, ...]:
        return tuple(entry.path for entry in self._entries)


@dataclass(frozen=True, slots=True)
class BatchValidationIssue:
    document_number: int
    document_name: str
    message: str


def validate_batch_requests(
    requests: tuple[ProcessRequest, ...],
    settings: AppSettings | None,
) -> tuple[BatchValidationIssue, ...]:
    """Validate every pending request and the aggregate output space before starting."""
    issues: list[BatchValidationIssue] = []
    valid_requests: list[tuple[int, ProcessRequest]] = []
    for index, request in enumerate(requests, start=1):
        try:
            validate_process_request(request, settings)
        except (ParsezenError, OSError, ValueError) as exc:
            issues.append(
                BatchValidationIssue(
                    index,
                    request.source_path.name,
                    str(exc) or "La solicitud no es válida.",
                )
            )
        else:
            valid_requests.append((index, request))
    if issues:
        return tuple(issues)

    return _validate_aggregate_output_space(tuple(valid_requests))


def validate_independent_batch_requests(
    items: tuple[tuple[ProcessRequest, AppSettings | None], ...],
) -> tuple[BatchValidationIssue, ...]:
    """Validate a queue whose documents can use different runtime settings."""

    issues: list[BatchValidationIssue] = []
    valid_requests: list[tuple[int, ProcessRequest]] = []
    for index, (request, settings) in enumerate(items, start=1):
        try:
            validate_process_request(request, settings)
        except (ParsezenError, OSError, ValueError) as exc:
            issues.append(
                BatchValidationIssue(
                    index,
                    request.source_path.name,
                    str(exc) or "La solicitud no es válida.",
                )
            )
        else:
            valid_requests.append((index, request))
    if issues:
        return tuple(issues)
    return _validate_aggregate_output_space(tuple(valid_requests))


def _validate_aggregate_output_space(
    valid_requests: tuple[tuple[int, ProcessRequest], ...],
) -> tuple[BatchValidationIssue, ...]:
    issues: list[BatchValidationIssue] = []
    required_by_directory: dict[Path, int] = {}
    for index, request in valid_requests:
        output_directory = request.output_directory or request.source_path.parent
        try:
            source_size = request.source_path.stat().st_size
        except OSError:
            issues.append(
                BatchValidationIssue(
                    index,
                    request.source_path.name,
                    "No se pudo comprobar el tamaño del documento.",
                )
            )
            continue
        estimate = max(8 * 1024 * 1024, min(source_size * 2, 1024 * 1024 * 1024))
        required_by_directory[output_directory] = (
            required_by_directory.get(output_directory, 0) + estimate
        )
    for directory, required_bytes in required_by_directory.items():
        try:
            free_bytes = shutil.disk_usage(directory).free
        except OSError:
            issues.append(BatchValidationIssue(0, "Lote", "No se pudo comprobar el espacio libre."))
            continue
        if free_bytes < required_bytes:
            issues.append(
                BatchValidationIssue(
                    0,
                    "Lote",
                    "No hay espacio libre suficiente para guardar todos los resultados del lote.",
                )
            )
    return tuple(issues)
