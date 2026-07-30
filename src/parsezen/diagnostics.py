"""Content-free local diagnostics for a non-technical support workflow."""

from __future__ import annotations

import importlib.util
import os
import platform
import re
import shutil
import sys
from collections import Counter
from itertools import islice
from pathlib import Path
from tempfile import gettempdir

from platformdirs import user_cache_path, user_log_path

from parsezen import APP_DISPLAY_NAME, APP_STORAGE_NAME, __version__
from parsezen.job_sessions import (
    RecentJobStatus,
    get_history_path,
    get_session_path,
    load_recent_jobs,
)
from parsezen.settings import AppSettings

_HASH_NAME_PATTERN = re.compile(r"[0-9a-f]{64}")
_SAFE_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_.:/-]{1,200}")
_MAX_LOG_TAIL_BYTES = 256 * 1024
_MAX_CHECKPOINT_JOBS = 500
_MAX_CHECKPOINT_FILES = 10_000
_OLLAMA_STATUS_TEXT = {
    "ready": "Preparada",
    "not_installed": "No instalado",
    "stopped": "Detenido",
    "missing_model": "Sin modelo",
    "local_only_required": "Requiere modo local",
    "unavailable": "No disponible",
}
_PROCESS_STAGE_TEXT = {
    "validating": "validación",
    "reading": "lectura",
    "converting": "conversión",
    "ocr": "OCR",
    "preserving_images": "imágenes",
    "structuring": "estructura inicial",
    "preparing_translation": "preparación de traducción",
    "improving": "traducción o mejora",
    "reviewing_content": "revisión de contenido",
    "organizing_structure": "personalización",
    "translating": "traducción",
    "building_epub": "creación del EPUB",
    "writing": "guardado",
    "completed": "finalización",
    "not_started": "inicio",
}


def build_diagnostic_report(
    settings: AppSettings,
    *,
    ollama_status: str | None,
    installed_models: int,
    queued_documents: int | None = None,
    session_path: Path | None = None,
    history_path: Path | None = None,
    work_checkpoint_root: Path | None = None,
    log_path: Path | None = None,
) -> str:
    """Return a copyable report without paths, document names, prompts or converted text."""
    actual_session_path = (
        (session_path if session_path is not None else get_session_path())
        if queued_documents is None
        else None
    )
    actual_history_path = history_path if history_path is not None else get_history_path()
    jobs = load_recent_jobs(path=actual_history_path)
    outcomes = Counter(job.status for job in jobs)
    checkpoint_jobs, checkpoint_files, checkpoint_bytes = _checkpoint_stats(work_checkpoint_root)
    free_bytes = _available_bytes(settings)
    status_text = _OLLAMA_STATUS_TEXT.get(ollama_status or "", "Sin comprobar")
    selected_model = _safe_model(settings.model)
    latest_failure = _latest_safe_failure(log_path)
    ocr_available = _module_available("docling") and _module_available("easyocr")
    argos_available = _module_available("argostranslate")
    recoverable_session = actual_session_path is not None and actual_session_path.is_file()
    system = platform.system() or "Desconocido"
    release = platform.release() or ""
    return "\n".join(
        (
            f"{APP_DISPLAY_NAME} — diagnóstico local",
            f"Versión: {__version__}",
            f"Sistema: {system} {release}".rstrip(),
            f"Python: {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "",
            f"Estado de la IA: {status_text}",
            f"Modelo seleccionado: {selected_model}",
            f"Modelos instalados detectados: {max(installed_models, 0)}",
            f"OCR local: {'Disponible' if ocr_available else 'No disponible'}",
            f"Traducción local: {'Disponible' if argos_available else 'No disponible'}",
            f"Espacio temporal disponible: {_format_bytes(free_bytes)}",
            "",
            (
                f"Documentos guardados en la cola: {max(queued_documents, 0)}"
                if queued_documents is not None
                else (f"Trabajo recuperable activo: {'Sí' if recoverable_session else 'No'}")
            ),
            (
                "Actividad reciente: "
                f"{len(jobs)} total · {outcomes[RecentJobStatus.COMPLETED]} completadas · "
                f"{outcomes[RecentJobStatus.FAILED]} con error · "
                f"{outcomes[RecentJobStatus.CANCELLED]} canceladas"
            ),
            (
                f"Checkpoints: {checkpoint_jobs} trabajos · {checkpoint_files} partes · "
                f"{_format_bytes(checkpoint_bytes)}"
            ),
            (
                "Retención cifrada de trabajo: "
                + (
                    "solo hasta finalizar"
                    if settings.checkpoint_retention_days == 0
                    else f"{settings.checkpoint_retention_days} días"
                )
            ),
            f"Último fallo registrado: {latest_failure or 'Ninguno'}",
            "",
            "Privacidad: este informe no incluye rutas, nombres ni contenido de documentos.",
        )
    )


def _module_available(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _available_bytes(settings: AppSettings) -> int:
    candidate = settings.output_directory
    if candidate is None or not candidate.is_dir():
        candidate = Path(gettempdir())
    try:
        return shutil.disk_usage(candidate).free
    except OSError:
        return 0


def _safe_model(model: str | None) -> str:
    if model is None:
        return "Ninguno"
    stripped = model.strip()
    return stripped if _SAFE_TOKEN_PATTERN.fullmatch(stripped) else "Identificador no mostrable"


def _checkpoint_stats(root: Path | None) -> tuple[int, int, int]:
    cache_root = (
        root
        if root is not None
        else user_cache_path(APP_STORAGE_NAME, appauthor=False) / "work-checkpoints"
    )
    jobs = files = total_bytes = 0
    try:
        directories = tuple(islice(cache_root.iterdir(), _MAX_CHECKPOINT_JOBS))
    except OSError:
        return 0, 0, 0
    for directory in directories:
        if (
            directory.is_symlink()
            or not directory.is_dir()
            or not _HASH_NAME_PATTERN.fullmatch(directory.name)
        ):
            continue
        jobs += 1
        try:
            entries = directory.iterdir()
            for entry in entries:
                if files >= _MAX_CHECKPOINT_FILES:
                    return jobs, files, total_bytes
                if (
                    entry.is_symlink()
                    or not entry.is_file()
                    or entry.suffix != ".json"
                    or not _HASH_NAME_PATTERN.fullmatch(entry.stem)
                ):
                    continue
                files += 1
                total_bytes += entry.stat().st_size
        except OSError:
            continue
    return jobs, files, total_bytes


def _latest_safe_failure(log_path: Path | None) -> str | None:
    path = (
        log_path
        if log_path is not None
        else user_log_path(APP_STORAGE_NAME, appauthor=False) / "parsezen.log"
    )
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - _MAX_LOG_TAIL_BYTES))
            lines = stream.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if "processing_failed" not in line:
            continue
        expected_type = _log_value(line, "error_type")
        stage = _safe_stage_text(_log_value(line, "stage"))
        if expected_type is not None:
            return f"{expected_type}{f' · etapa {stage}' if stage is not None else ''}"
        incident = _log_value(line, "incident")
        error_type = _log_value(line, "unexpected_error_type")
        module = _log_value(line, "module")
        function = _log_value(line, "function")
        line_number = _log_value(line, "line")
        if all((incident, error_type, module, function, line_number)):
            stage_text = f" · etapa {stage}" if stage is not None else ""
            return (
                f"{error_type} · referencia {incident}{stage_text} · "
                f"{module}.{function}:{line_number}"
            )
    return None


def _safe_stage_text(value: str | None) -> str | None:
    if value is None:
        return None
    return _PROCESS_STAGE_TEXT.get(value)


def _log_value(line: str, name: str) -> str | None:
    match = re.search(rf"(?:^|\s){re.escape(name)}=([A-Za-z0-9_.-]{{1,200}})(?:\s|$)", line)
    return match.group(1) if match is not None else None


def _format_bytes(value: int) -> str:
    if value >= 1024**3:
        return f"{value / 1024**3:.1f} GiB"
    if value >= 1024**2:
        return f"{value / 1024**2:.1f} MiB"
    if value >= 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value} B"
