"""Record and verify private PDF conversion baselines without storing document text."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkstemp
from time import monotonic

from parsezen.errors import ParsezenError
from parsezen.pdf_conversion import (
    PdfPageRange,
    PdfQualityReport,
    convert_pdf,
    extract_pdf_warning_pages,
)

SCHEMA_VERSION = 1
BENCHMARK_REVISION = "pdf-regression-v2"
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_DOCUMENTS = 100
MAX_RECORDING_RUNS = 10
_HEADING_PATTERN = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_LINK_PATTERN = re.compile(r"\[[^\]]+\]\((?:<[^>]+>|[^)]+)\)")


@dataclass(frozen=True, slots=True)
class PdfMetrics:
    markdown_sha256: str
    characters: int
    headings: int
    links: int
    warnings: int
    processed_pages: int
    ocr_pages: int
    elapsed_seconds: float
    peak_incremental_mib: float | None


@dataclass(frozen=True, slots=True)
class PdfBaseline:
    source_path: Path
    source_sha256: str
    page_range: PdfPageRange | None
    force_ocr: bool
    expected: PdfMetrics
    max_elapsed_seconds: float
    max_peak_mib: float | None


@dataclass(frozen=True, slots=True)
class RegressionCheck:
    source_path: Path
    passed: bool
    problems: tuple[str, ...]
    metrics: PdfMetrics | None = None


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("page_fault_count", ctypes.c_ulong),
        ("peak_working_set_size", ctypes.c_size_t),
        ("working_set_size", ctypes.c_size_t),
        ("quota_peak_paged_pool_usage", ctypes.c_size_t),
        ("quota_paged_pool_usage", ctypes.c_size_t),
        ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
        ("quota_non_paged_pool_usage", ctypes.c_size_t),
        ("pagefile_usage", ctypes.c_size_t),
        ("peak_pagefile_usage", ctypes.c_size_t),
    ]


def measure_pdf(
    source_path: Path,
    *,
    page_range: PdfPageRange | None = None,
    force_ocr: bool = False,
) -> PdfMetrics:
    """Convert one PDF in memory and return content-free structural and resource metrics."""
    source = source_path.resolve(strict=True)
    if source.suffix.lower() != ".pdf" or not source.is_file():
        raise ValueError(f"No existe un PDF válido en {source_path}.")
    baseline_memory = _working_set_bytes()
    peak_memory = baseline_memory
    stop_sampling = threading.Event()

    def sample_memory() -> None:
        nonlocal peak_memory
        while not stop_sampling.wait(0.025):
            current = _working_set_bytes()
            if current is not None and (peak_memory is None or current > peak_memory):
                peak_memory = current

    sampler = threading.Thread(target=sample_memory, daemon=True)
    sampler.start()
    quality_report: PdfQualityReport | None = None

    def capture_quality(report: PdfQualityReport) -> None:
        nonlocal quality_report
        quality_report = report

    started = monotonic()
    try:
        markdown = convert_pdf(
            source,
            on_quality_report=capture_quality,
            page_range=page_range,
            force_ocr=force_ocr,
        )
    finally:
        stop_sampling.set()
        sampler.join()
    elapsed = monotonic() - started
    final_memory = _working_set_bytes()
    if final_memory is not None and (peak_memory is None or final_memory > peak_memory):
        peak_memory = final_memory
    incremental_mib = (
        max(0, peak_memory - baseline_memory) / (1024 * 1024)
        if peak_memory is not None and baseline_memory is not None
        else None
    )
    return PdfMetrics(
        markdown_sha256=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        characters=len(markdown),
        headings=len(_HEADING_PATTERN.findall(markdown)),
        links=len(_LINK_PATTERN.findall(markdown)),
        warnings=len(extract_pdf_warning_pages(markdown)),
        processed_pages=(len(quality_report.processed_pages) if quality_report is not None else 0),
        ocr_pages=(len(quality_report.ocr_pages) if quality_report is not None else 0),
        elapsed_seconds=elapsed,
        peak_incremental_mib=incremental_mib,
    )


def record_manifest(
    destination: Path,
    sources: tuple[Path, ...],
    *,
    page_range: PdfPageRange | None = None,
    force_ocr: bool = False,
    runs: int = 1,
) -> tuple[PdfBaseline, ...]:
    """Measure private PDFs and atomically write only their paths, hashes and metrics."""
    if not sources or len(sources) > MAX_DOCUMENTS:
        raise ValueError("El banco debe contener entre 1 y 100 documentos.")
    if isinstance(runs, bool) or not 1 <= runs <= MAX_RECORDING_RUNS:
        raise ValueError(f"Las repeticiones deben estar entre 1 y {MAX_RECORDING_RUNS}.")
    baselines: list[PdfBaseline] = []
    for source in sources:
        resolved = source.resolve(strict=True)
        samples = tuple(
            measure_pdf(resolved, page_range=page_range, force_ocr=force_ocr) for _ in range(runs)
        )
        metrics = _aggregate_recording_samples(samples)
        baselines.append(
            PdfBaseline(
                source_path=resolved,
                source_sha256=_sha256_file(resolved),
                page_range=page_range,
                force_ocr=force_ocr,
                expected=metrics,
                max_elapsed_seconds=max(
                    metrics.elapsed_seconds * 1.35, metrics.elapsed_seconds + 1
                ),
                max_peak_mib=(
                    max(metrics.peak_incremental_mib * 1.35, metrics.peak_incremental_mib + 32)
                    if metrics.peak_incremental_mib is not None
                    else None
                ),
            )
        )
    _write_manifest(destination, tuple(baselines))
    return tuple(baselines)


def _aggregate_recording_samples(samples: tuple[PdfMetrics, ...]) -> PdfMetrics:
    """Require stable output and retain the worst observed resource measurements."""
    if not samples:
        raise ValueError("La referencia necesita al menos una medición.")
    reference = samples[0]
    reference_structure = _structural_metrics(reference)
    if any(_structural_metrics(sample) != reference_structure for sample in samples[1:]):
        raise RuntimeError(
            "La conversión no produjo una salida estructural estable entre repeticiones."
        )
    memory_samples = tuple(
        sample.peak_incremental_mib for sample in samples if sample.peak_incremental_mib is not None
    )
    return PdfMetrics(
        markdown_sha256=reference.markdown_sha256,
        characters=reference.characters,
        headings=reference.headings,
        links=reference.links,
        warnings=reference.warnings,
        processed_pages=reference.processed_pages,
        ocr_pages=reference.ocr_pages,
        elapsed_seconds=max(sample.elapsed_seconds for sample in samples),
        peak_incremental_mib=max(memory_samples) if memory_samples else None,
    )


def _structural_metrics(metrics: PdfMetrics) -> tuple[str | int, ...]:
    return (
        metrics.markdown_sha256,
        metrics.characters,
        metrics.headings,
        metrics.links,
        metrics.warnings,
        metrics.processed_pages,
        metrics.ocr_pages,
    )


def check_manifest(path: Path) -> tuple[RegressionCheck, ...]:
    """Run every private baseline and report structural or performance regressions."""
    checks: list[RegressionCheck] = []
    for baseline in load_manifest(path):
        source = baseline.source_path
        if not source.is_file():
            checks.append(RegressionCheck(source, False, ("El documento ya no existe.",)))
            continue
        if _sha256_file(source) != baseline.source_sha256:
            checks.append(
                RegressionCheck(
                    source,
                    False,
                    ("El documento cambió; vuelve a registrar su referencia.",),
                )
            )
            continue
        try:
            metrics = measure_pdf(
                source,
                page_range=baseline.page_range,
                force_ocr=baseline.force_ocr,
            )
        except (ParsezenError, OSError, RuntimeError, ValueError) as exc:
            checks.append(RegressionCheck(source, False, (str(exc),)))
            continue
        problems = _compare_metrics(metrics, baseline)
        checks.append(RegressionCheck(source, not problems, problems, metrics))
    return tuple(checks)


def load_manifest(path: Path) -> tuple[PdfBaseline, ...]:
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise ValueError("El manifiesto local es demasiado grande.")
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"No existe el manifiesto {path}.") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("No se pudo leer el manifiesto local.") from exc
    if (
        not isinstance(raw, dict)
        or set(raw) != {"schema_version", "revision", "documents"}
        or raw["schema_version"] != SCHEMA_VERSION
        or raw["revision"] != BENCHMARK_REVISION
        or not isinstance(raw["documents"], list)
        or not 1 <= len(raw["documents"]) <= MAX_DOCUMENTS
    ):
        raise ValueError("El manifiesto local no tiene un formato compatible.")
    return tuple(_baseline_from_json(item) for item in raw["documents"])


def _compare_metrics(metrics: PdfMetrics, baseline: PdfBaseline) -> tuple[str, ...]:
    problems: list[str] = []
    expected = baseline.expected
    for label, actual, reference in (
        ("huella Markdown", metrics.markdown_sha256, expected.markdown_sha256),
        ("caracteres", metrics.characters, expected.characters),
        ("encabezados", metrics.headings, expected.headings),
        ("enlaces", metrics.links, expected.links),
        ("avisos", metrics.warnings, expected.warnings),
        ("páginas", metrics.processed_pages, expected.processed_pages),
        ("páginas OCR", metrics.ocr_pages, expected.ocr_pages),
    ):
        if actual != reference:
            problems.append(f"Cambió {label}: {reference} → {actual}.")
    if metrics.elapsed_seconds > baseline.max_elapsed_seconds:
        problems.append(
            f"Tiempo: {metrics.elapsed_seconds:.2f} s supera {baseline.max_elapsed_seconds:.2f} s."
        )
    if (
        metrics.peak_incremental_mib is not None
        and baseline.max_peak_mib is not None
        and metrics.peak_incremental_mib > baseline.max_peak_mib
    ):
        problems.append(
            f"Memoria: {metrics.peak_incremental_mib:.1f} MiB supera "
            f"{baseline.max_peak_mib:.1f} MiB."
        )
    return tuple(problems)


def _write_manifest(path: Path, baselines: tuple[PdfBaseline, ...]) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "revision": BENCHMARK_REVISION,
        "documents": [_baseline_json(baseline) for baseline in baselines],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = mkstemp(
        dir=path.parent,
        prefix=".pdf-regression-",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _baseline_json(baseline: PdfBaseline) -> dict[str, object]:
    metrics = baseline.expected
    return {
        "path": str(baseline.source_path),
        "source_sha256": baseline.source_sha256,
        "page_range": (
            [baseline.page_range.first_page, baseline.page_range.last_page]
            if baseline.page_range is not None
            else None
        ),
        "force_ocr": baseline.force_ocr,
        "expected": {
            "markdown_sha256": metrics.markdown_sha256,
            "characters": metrics.characters,
            "headings": metrics.headings,
            "links": metrics.links,
            "warnings": metrics.warnings,
            "processed_pages": metrics.processed_pages,
            "ocr_pages": metrics.ocr_pages,
        },
        "max_elapsed_seconds": baseline.max_elapsed_seconds,
        "max_peak_mib": baseline.max_peak_mib,
    }


def _baseline_from_json(raw: object) -> PdfBaseline:
    if not isinstance(raw, dict) or set(raw) != {
        "path",
        "source_sha256",
        "page_range",
        "force_ocr",
        "expected",
        "max_elapsed_seconds",
        "max_peak_mib",
    }:
        raise ValueError("Una entrada del manifiesto no es válida.")
    source_text = raw["path"]
    source_sha256 = raw["source_sha256"]
    if (
        not isinstance(source_text, str)
        or not source_text
        or len(source_text) > 32_767
        or not isinstance(source_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", source_sha256)
        or type(raw["force_ocr"]) is not bool
    ):
        raise ValueError("Una entrada del manifiesto no es válida.")
    page_range = _page_range_from_json(raw["page_range"])
    metrics = _metrics_from_json(raw["expected"])
    max_elapsed = _positive_number(raw["max_elapsed_seconds"])
    max_peak_raw = raw["max_peak_mib"]
    max_peak = None if max_peak_raw is None else _positive_number(max_peak_raw)
    return PdfBaseline(
        source_path=Path(source_text),
        source_sha256=source_sha256,
        page_range=page_range,
        force_ocr=raw["force_ocr"],
        expected=metrics,
        max_elapsed_seconds=max_elapsed,
        max_peak_mib=max_peak,
    )


def _metrics_from_json(raw: object) -> PdfMetrics:
    if not isinstance(raw, dict) or set(raw) != {
        "markdown_sha256",
        "characters",
        "headings",
        "links",
        "warnings",
        "processed_pages",
        "ocr_pages",
    }:
        raise ValueError("Las métricas del manifiesto no son válidas.")
    digest = raw["markdown_sha256"]
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Las métricas del manifiesto no son válidas.")
    counts = [
        _non_negative_integer(raw[key])
        for key in (
            "characters",
            "headings",
            "links",
            "warnings",
            "processed_pages",
            "ocr_pages",
        )
    ]
    return PdfMetrics(
        markdown_sha256=digest,
        characters=counts[0],
        headings=counts[1],
        links=counts[2],
        warnings=counts[3],
        processed_pages=counts[4],
        ocr_pages=counts[5],
        elapsed_seconds=0,
        peak_incremental_mib=None,
    )


def _page_range_from_json(raw: object) -> PdfPageRange | None:
    if raw is None:
        return None
    if (
        not isinstance(raw, list)
        or len(raw) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) for value in raw)
        or raw[0] < 1
        or raw[1] < raw[0]
    ):
        raise ValueError("El rango del manifiesto no es válido.")
    return PdfPageRange(raw[0], raw[1])


def _positive_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError("Un límite del manifiesto no es válido.")
    return float(value)


def _non_negative_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Un contador del manifiesto no es válido.")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _working_set_bytes() -> int | None:
    try:
        import psutil
    except ImportError:
        pass
    else:
        try:
            process = psutil.Process(os.getpid())
            processes = (process, *process.children(recursive=True))
            total = 0
            for item in processes:
                try:
                    total += int(item.memory_info().rss)
                except (psutil.Error, OSError):
                    continue
            if total:
                return total
        except (psutil.Error, OSError):
            pass
    if os.name != "nt":
        return None
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = []
    get_current_process.restype = ctypes.c_void_p
    get_process_memory_info = psapi.GetProcessMemoryInfo
    get_process_memory_info.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_ProcessMemoryCounters),
        ctypes.c_ulong,
    ]
    get_process_memory_info.restype = ctypes.c_int
    if not get_process_memory_info(
        get_current_process(),
        ctypes.byref(counters),
        counters.cb,
    ):
        return None
    return int(counters.working_set_size)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Registra o comprueba conversiones PDF privadas sin guardar su contenido."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    record = subparsers.add_parser("record", help="Crear una referencia local")
    record.add_argument("manifest", type=Path)
    record.add_argument("sources", type=Path, nargs="+")
    record.add_argument("--pages", nargs=2, type=int, metavar=("INICIO", "FIN"))
    record.add_argument("--force-ocr", action="store_true")
    record.add_argument(
        "--runs",
        type=int,
        default=1,
        help="Repite cada conversión y usa el peor consumo observado (máximo 10).",
    )
    check = subparsers.add_parser("check", help="Comprobar una referencia local")
    check.add_argument("manifest", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "record":
            page_range = PdfPageRange(*arguments.pages) if arguments.pages is not None else None
            baselines = record_manifest(
                arguments.manifest,
                tuple(arguments.sources),
                page_range=page_range,
                force_ocr=arguments.force_ocr,
                runs=arguments.runs,
            )
            for baseline in baselines:
                print(
                    f"Registrado {baseline.source_path.name}: "
                    f"{baseline.expected.elapsed_seconds:.2f} s, "
                    f"{baseline.expected.peak_incremental_mib or 0:.1f} MiB."
                )
            return 0
        checks = check_manifest(arguments.manifest)
        for check in checks:
            state = "OK" if check.passed else "FALLO"
            print(f"{state} {check.source_path.name}")
            for problem in check.problems:
                print(f"  {problem}")
        return 0 if all(check.passed for check in checks) else 1
    except (ParsezenError, OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
