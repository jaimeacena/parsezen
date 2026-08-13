"""Validate one prepared queue run without owning queue state."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from parsezen.errors import ParsezenError
from parsezen.pipeline.contracts import ProcessRequest
from parsezen.processing import validate_process_request
from parsezen.settings import AppSettings


@dataclass(frozen=True, slots=True)
class BatchValidationIssue:
    document_number: int
    document_name: str
    message: str


def validate_batch_requests(
    requests: tuple[ProcessRequest, ...],
    settings: AppSettings | None,
) -> tuple[BatchValidationIssue, ...]:
    """Validate requests sharing settings and their aggregate output space."""

    return validate_independent_batch_requests(tuple((request, settings) for request in requests))


def validate_independent_batch_requests(
    items: tuple[tuple[ProcessRequest, AppSettings | None], ...],
) -> tuple[BatchValidationIssue, ...]:
    """Validate requests that may use different runtime settings."""

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
