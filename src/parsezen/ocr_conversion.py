"""Lazy, local OCR and table extraction for selected PDF pages."""

from __future__ import annotations

import gc
import logging
import os
import re
import shutil
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from typing import Any

import pdfplumber
from PIL import ImageStat

from parsezen.cancellation import CancellationToken, check_cancelled
from parsezen.errors import ConversionError, ProcessingCancelledError

_IMAGE_PLACEHOLDER_PATTERN = re.compile(r"<!--\s*image\s*-->", re.IGNORECASE)
_HEADING_PATTERN = re.compile(r"^(#{1,6}\s+)(.+)$")
_SPACED_HEADING_MARKERS_PATTERN = re.compile(
    r"^(?P<markers>#(?:[ \t]+#){1,5})[ \t]+(?P<body>\S.*)$"
)
_STANDALONE_PAGE_NUMBER_PATTERN = re.compile(r"(?:\d{1,3}|[ivxlcdm]+)", re.IGNORECASE)
_INVALID_XML_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_UNESCAPED_PIPE_PATTERN = re.compile(r"(?<!\\)\|")
_TABLE_DIVIDER_PATTERN = re.compile(r"\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*")
_TABLE_CAPTION_PATTERN = re.compile(
    r"^[*_`\s]*(?:table|tabla|tableau|tabelle|tavola|tabela)\s+"
    r"(?:\d+|[ivxlcdm]+)\b",
    re.IGNORECASE,
)
# Docling keeps page images and intermediate layout tensors alive for the whole
# conversion call. Small batches cap the peak without rebuilding the worker model.
_MAX_PAGES_PER_BATCH = 2
_MAX_PAGES_PER_WORKER = 8
OCR_LANGUAGES = ("es", "en", "fr", "de", "it", "pt")

LOGGER = logging.getLogger(__name__)


def convert_pdf_pages_with_ocr(
    source_path: Path,
    page_numbers: set[int],
    cancellation: CancellationToken | None = None,
    *,
    force_full_page_numbers: set[int] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    on_page_result: Callable[[int, str], None] | None = None,
) -> dict[int, str]:
    """Return local OCR Markdown from bounded, restartable worker requests.

    A request is deliberately split into the same small ranges used by the
    in-process converter.  Each range has its own worker lifecycle, so a
    crashed or poisoned batch can be retried with a fresh process and then
    degraded to one page at a time without losing successful pages.
    """
    if not page_numbers:
        return {}
    check_cancelled(cancellation)
    from parsezen.ocr_executor import run_ocr_worker

    requested = set(page_numbers)
    forced = set(force_full_page_numbers or ())
    completed_pages: set[int] = set()
    results: dict[int, str] = {}
    last_failure: ConversionError | None = None
    reported_pages: set[int] = set()
    streamed_results: dict[int, str] = {}
    request_completed_offset = 0

    def report_progress(current: int, total: int) -> None:
        del total
        if on_progress is not None:
            on_progress(min(request_completed_offset + current, len(requested)), len(requested))

    def report_page(page_number: int, markdown: str) -> None:
        streamed_results[page_number] = markdown
        if page_number in reported_pages:
            return
        reported_pages.add(page_number)
        if on_page_result is not None:
            on_page_result(page_number, markdown)

    def run_request(batch: set[int]) -> dict[int, str]:
        nonlocal request_completed_offset
        request_completed_offset = len(completed_pages | streamed_results.keys())
        arguments: dict[str, Any] = {
            "force_full_page_numbers": forced & batch,
            "on_progress": report_progress,
            "on_page_result": report_page,
        }
        return run_ocr_worker(source_path, batch, cancellation, **arguments)

    for batch in _worker_page_batches(requested):
        first_page = min(batch)
        last_page = max(batch)
        check_cancelled(cancellation)
        batch_result = {
            page_number: streamed_results[page_number]
            for page_number in batch
            if page_number in streamed_results
        }
        pending = batch - batch_result.keys()
        # A fresh invocation starts a fresh private worker. This is the
        # watchdog boundary: no worker is reused after a protocol/engine fail.
        for attempt in range(2):
            if not pending:
                break
            try:
                batch_result.update(run_request(set(pending)))
            except ProcessingCancelledError:
                raise
            except ConversionError as exc:
                last_failure = exc
                LOGGER.warning(
                    "ocr_worker_batch_retry pages=%d-%d attempt=%d",
                    first_page,
                    last_page,
                    attempt + 1,
                )
            batch_result.update(
                {
                    page_number: streamed_results[page_number]
                    for page_number in pending
                    if page_number in streamed_results
                }
            )
            pending = batch - batch_result.keys()
        if pending and len(batch) > 1:
            LOGGER.warning(
                "ocr_worker_batch_degraded pages=%d-%d",
                first_page,
                last_page,
            )
            for page_number in sorted(pending):
                page_result: dict[int, str] | None = None
                for attempt in range(2):
                    try:
                        page_result = run_request({page_number})
                        break
                    except ProcessingCancelledError:
                        raise
                    except ConversionError as exc:
                        last_failure = exc
                        LOGGER.warning(
                            "ocr_worker_page_retry page=%d attempt=%d",
                            page_number,
                            attempt + 1,
                        )
                if page_result is not None:
                    batch_result.update(page_result)
                if page_number in streamed_results:
                    batch_result[page_number] = streamed_results[page_number]
        if not batch_result:
            continue
        results.update(batch_result)
        completed_pages.update(batch_result)
        # Workers that do not support streaming page callbacks still report
        # their completed pages here, preserving the checkpoint contract.
        for page_number, markdown in batch_result.items():
            report_page(page_number, markdown)

    if not results and streamed_results:
        results.update(streamed_results)
    if not results and last_failure is not None:
        raise last_failure
    return results


def _convert_pdf_pages_in_process(
    source_path: Path,
    page_numbers: set[int],
    cancellation: CancellationToken | None = None,
    *,
    force_full_page_numbers: set[int] | None = None,
    on_stage: Callable[[str], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    on_page_result: Callable[[int, str], None] | None = None,
) -> dict[int, str]:
    """Run Docling inside the dedicated OCR worker process."""
    if not page_numbers:
        return {}

    try:
        check_cancelled(cancellation)
        if on_stage is not None:
            on_stage("preparing")
        try:
            converter = _document_converter()
        except Exception as exc:
            raise ConversionError(
                "Los modelos OCR locales no están disponibles. "
                "Comprueba la instalación y vuelve a intentarlo."
            ) from exc
        check_cancelled(cancellation)
        if on_stage is not None:
            on_stage("running")
        markdown_pages: dict[int, str] = {}
        reported_pages: set[int] = set()
        reported_results: set[int] = set()

        def report_page(page_number: int) -> None:
            if page_number in reported_pages:
                return
            reported_pages.add(page_number)
            if on_progress is not None:
                on_progress(len(reported_pages), len(page_numbers))

        def report_result(page_number: int, markdown: str) -> None:
            if page_number in reported_results:
                return
            reported_results.add(page_number)
            if on_page_result is not None:
                on_page_result(page_number, markdown)

        first_conversion_error: Exception | None = None
        with _ascii_pdf_path(source_path) as conversion_path:
            for first_page, last_page in _page_ranges(page_numbers):
                check_cancelled(cancellation)
                try:
                    result = converter.convert(
                        conversion_path,
                        page_range=(first_page, last_page),
                    )
                except ProcessingCancelledError:
                    raise
                except Exception as exc:
                    if first_conversion_error is None:
                        first_conversion_error = exc
                    continue
                check_cancelled(cancellation)
                for page_number in range(first_page, last_page + 1):
                    check_cancelled(cancellation)
                    markdown = result.document.export_to_markdown(
                        page_no=page_number,
                        image_placeholder="",
                        traverse_pictures=True,
                    )
                    markdown = _strip_low_prominence_ocr_lines(
                        markdown,
                        result,
                        page_number=page_number,
                        fallback_index=page_number - first_page,
                    )
                    cleaned = _clean_ocr_markdown(markdown)
                    if cleaned:
                        markdown_pages[page_number] = cleaned
                    if cleaned and page_number not in (force_full_page_numbers or set()):
                        report_result(page_number, cleaned)
                        report_page(page_number)
                del result
                gc.collect()

        forced_pages = set(force_full_page_numbers or ()) & page_numbers
        recovery_pages = (page_numbers - markdown_pages.keys()) | forced_pages
        if recovery_pages:
            # Docling conversion results retain page images and model tensors. The
            # final native batch is no longer needed once its Markdown is exported;
            # collect it before allocating the recovery images for the same pages.
            gc.collect()
            try:
                recovered = _recover_pages_from_images(
                    converter,
                    source_path,
                    recovery_pages,
                    cancellation,
                    on_page_finished=report_page,
                )
            except ProcessingCancelledError:
                raise
            except Exception as exc:
                if first_conversion_error is None:
                    first_conversion_error = exc
            else:
                markdown_pages.update(recovered)

        for page_number in page_numbers:
            markdown = markdown_pages.get(page_number)
            if markdown is not None:
                report_result(page_number, markdown)
            elif first_conversion_error is None:
                # An empty result is still a completed OCR analysis. Report it so
                # callers can persist a negative checkpoint instead of spending
                # the same OCR work again on every resume.
                report_result(page_number, "")
            report_page(page_number)

        if first_conversion_error is not None and not markdown_pages:
            raise _ocr_conversion_error(first_conversion_error)
        check_cancelled(cancellation)
        return markdown_pages
    except ProcessingCancelledError:
        raise
    except ConversionError:
        raise
    except Exception as exc:
        raise _ocr_conversion_error(exc) from exc


@lru_cache(maxsize=1)
def _document_converter() -> Any:
    """Build and retain one expensive local Docling pipeline per app process."""
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        EasyOcrOptions,
        PdfPipelineOptions,
        TableFormerMode,
    )
    from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption

    thread_count = max(1, min(os.cpu_count() or 4, 4))
    pdf_pipeline_options = _pipeline_options(
        PdfPipelineOptions,
        AcceleratorOptions,
        AcceleratorDevice,
        EasyOcrOptions,
        TableFormerMode,
        thread_count,
        force_full_page_ocr=False,
    )
    image_pipeline_options = _pipeline_options(
        PdfPipelineOptions,
        AcceleratorOptions,
        AcceleratorDevice,
        EasyOcrOptions,
        TableFormerMode,
        thread_count,
        force_full_page_ocr=True,
    )
    return DocumentConverter(
        allowed_formats=[InputFormat.PDF, InputFormat.IMAGE],
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_pipeline_options),
            InputFormat.IMAGE: ImageFormatOption(pipeline_options=image_pipeline_options),
        },
    )


def _pipeline_options(
    options_type: Any,
    accelerator_options_type: Any,
    accelerator_device: Any,
    ocr_options_type: Any,
    table_mode: Any,
    thread_count: int,
    *,
    force_full_page_ocr: bool,
) -> Any:
    pipeline_options = options_type(
        accelerator_options=accelerator_options_type(
            num_threads=thread_count,
            device=accelerator_device.AUTO,
        ),
        enable_remote_services=False,
        allow_external_plugins=False,
        do_ocr=True,
        do_table_structure=True,
        images_scale=2.0,
    )
    pipeline_options.ocr_options = ocr_options_type(
        lang=list(OCR_LANGUAGES),
        force_full_page_ocr=force_full_page_ocr,
        bitmap_area_threshold=0.01 if force_full_page_ocr else 0.05,
        confidence_threshold=0.4 if force_full_page_ocr else 0.45,
        use_gpu=None,
        download_enabled=True,
    )
    pipeline_options.table_structure_options.mode = table_mode.ACCURATE
    return pipeline_options


def _document_stream(name: str, content: bytes | BytesIO) -> Any:
    from docling.datamodel.base_models import DocumentStream

    stream = content if isinstance(content, BytesIO) else BytesIO(content)
    stream.seek(0)
    return DocumentStream(name=name, stream=stream)


@contextmanager
def _ascii_pdf_path(source_path: Path) -> Iterator[Path]:
    """Expose a short ASCII path without loading the whole PDF into memory."""

    try:
        str(source_path).encode("ascii")
    except UnicodeEncodeError:
        pass
    else:
        yield source_path
        return

    with TemporaryDirectory(prefix="parsezen-ocr-") as temporary_directory:
        temporary_path = Path(temporary_directory) / "parsezen.pdf"
        try:
            os.link(source_path, temporary_path)
        except OSError:
            try:
                shutil.copyfile(source_path, temporary_path)
            except OSError as exc:
                raise ConversionError("No se pudo preparar el PDF para el OCR local.") from exc
        yield temporary_path


def _recover_pages_from_images(
    converter: Any,
    source_path: Path,
    page_numbers: set[int],
    cancellation: CancellationToken | None,
    *,
    on_page_finished: Callable[[int], None] | None = None,
) -> dict[int, str]:
    recovered: dict[int, str] = {}
    with pdfplumber.open(source_path) as pdf:
        for page_number in sorted(page_numbers):
            check_cancelled(cancellation)
            if page_number < 1 or page_number > len(pdf.pages):
                continue
            page = pdf.pages[page_number - 1]
            image = page.to_image(resolution=144, antialias=True).original
            if not _has_meaningful_visual_content(image):
                recovered[page_number] = ""
                if on_page_finished is not None:
                    on_page_finished(page_number)
                continue

            best_markdown = ""
            best_score = -1
            strong_candidate_found = False
            for candidate_image, angles in _recovery_image_candidates(page, image):
                for angle in angles:
                    check_cancelled(cancellation)
                    candidate = candidate_image.rotate(angle, expand=angle != 0)
                    buffer = BytesIO()
                    candidate.save(buffer, format="PNG")
                    result = converter.convert(
                        _document_stream("parsezen-page.png", buffer.getvalue())
                    )
                    markdown = result.document.export_to_markdown(
                        page_no=1,
                        image_placeholder="",
                        traverse_pictures=True,
                    )
                    markdown = _strip_low_prominence_ocr_lines(markdown, result)
                    del result
                    cleaned = _clean_ocr_markdown(markdown)
                    score = _ocr_markdown_score(cleaned)
                    if score > best_score:
                        best_markdown = cleaned
                        best_score = score
                    if _contains_markdown_table(cleaned) or _letter_count(cleaned) >= 40:
                        strong_candidate_found = True
                        break
                if strong_candidate_found:
                    break
            del buffer, candidate, candidate_image, image
            if best_markdown:
                recovered[page_number] = best_markdown
            if on_page_finished is not None:
                on_page_finished(page_number)
    return recovered


def _strip_low_prominence_ocr_lines(
    markdown: str,
    conversion_result: Any,
    *,
    page_number: int | None = None,
    fallback_index: int | None = None,
) -> str:
    """Drop tiny isolated labels from the matching OCR page only."""

    candidates: set[str] = set()
    for page in _matching_ocr_pages(
        conversion_result,
        page_number=page_number,
        fallback_index=fallback_index,
    ):
        assembled = getattr(page, "assembled", None)
        elements = getattr(assembled, "elements", ()) if assembled is not None else ()
        cells_by_identity: dict[int, Any] = {}
        for element in elements:
            cluster = getattr(element, "cluster", None)
            for cell in getattr(cluster, "cells", ()):
                if getattr(cell, "from_ocr", False):
                    cells_by_identity[id(cell)] = cell
        cells = tuple(cells_by_identity.values())
        heights = [
            box.height
            for cell in cells
            if (text := " ".join(str(getattr(cell, "text", "")).split()))
            and (box := cell.to_bounding_box()).height > 0
        ]
        page_size = getattr(page, "size", None)
        if len(heights) < 4 or page_size is None or page_size.width <= 0:
            continue
        typical_height = median(heights)
        for cell in cells:
            text = " ".join(str(getattr(cell, "text", "")).split())
            words = re.findall(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", text, re.UNICODE)
            if len(words) != 1 or len(words[0]) > 4:
                continue
            box = cell.to_bounding_box()
            if box.height < typical_height * 0.34 and box.width / page_size.width < 0.12:
                candidates.add(text.casefold())
    if not candidates:
        return markdown

    kept: list[str] = []
    for line in markdown.splitlines(keepends=True):
        visible = line.strip()
        heading = _HEADING_PATTERN.fullmatch(visible)
        if heading is not None:
            visible = heading.group(2).strip()
        if visible.casefold() in candidates:
            continue
        kept.append(line)
    return "".join(kept)


def _matching_ocr_pages(
    conversion_result: Any,
    *,
    page_number: int | None,
    fallback_index: int | None,
) -> tuple[Any, ...]:
    pages = getattr(conversion_result, "pages", None)
    if pages is None:
        document = getattr(conversion_result, "document", None)
        pages = getattr(document, "pages", ()) if document is not None else ()
    if isinstance(pages, Mapping):
        if page_number is not None:
            for key in (page_number, str(page_number)):
                if key in pages:
                    return (pages[key],)
        normalized = tuple(pages.values())
    else:
        normalized = tuple(pages) if isinstance(pages, Iterable) else ()
    if len(normalized) <= 1 or page_number is None:
        return normalized
    for page in normalized:
        candidate_number = next(
            (
                getattr(page, attribute)
                for attribute in ("page_no", "page_number", "number")
                if getattr(page, attribute, None) is not None
            ),
            None,
        )
        if candidate_number == page_number:
            return (page,)
    if fallback_index is not None and 0 <= fallback_index < len(normalized):
        return (normalized[fallback_index],)
    return ()


def _recovery_image_candidates(
    page: Any, full_page_image: Any
) -> list[tuple[Any, tuple[int, ...]]]:
    """Prefer a rotated image region when its PDF placement changed orientation."""
    candidates: list[tuple[Any, tuple[int, ...]]] = []
    mismatched_image = _dominant_orientation_mismatched_image(page)
    if mismatched_image is not None:
        try:
            cropped = (
                page.crop(
                    (
                        float(mismatched_image["x0"]),
                        float(mismatched_image["top"]),
                        float(mismatched_image["x1"]),
                        float(mismatched_image["bottom"]),
                    )
                )
                .to_image(resolution=180, antialias=True)
                .original
            )
        except (KeyError, TypeError, ValueError):
            cropped = None
        if cropped is not None and _has_meaningful_visual_content(cropped):
            candidates.append((cropped, (-90, 90, 0, 180)))
    candidates.append((full_page_image, _candidate_rotation_angles(page)))
    return candidates


def _dominant_orientation_mismatched_image(page: Any) -> dict[str, Any] | None:
    if not page.images:
        return None
    dominant = max(
        page.images,
        key=lambda image: float(image.get("width", 0)) * float(image.get("height", 0)),
    )
    source_size = dominant.get("srcsize")
    if not isinstance(source_size, tuple) or len(source_size) != 2:
        return None
    source_landscape = float(source_size[0]) > float(source_size[1])
    displayed_landscape = float(dominant.get("width", 0)) > float(dominant.get("height", 0))
    return dominant if source_landscape != displayed_landscape else None


def _has_meaningful_visual_content(image: Any) -> bool:
    grayscale = image.convert("L")
    statistics = ImageStat.Stat(grayscale)
    standard_deviation = float(statistics.stddev[0])
    histogram = grayscale.histogram()
    dark_pixels = sum(histogram[:200])
    total_pixels = max(sum(histogram), 1)
    return standard_deviation >= 10 and dark_pixels / total_pixels >= 0.003


def _candidate_rotation_angles(page: Any) -> tuple[int, ...]:
    if _dominant_orientation_mismatched_image(page) is not None:
        return (-90, 90, 0, 180)
    return (0, 180, -90, 90)


def _contains_markdown_table(markdown: str) -> bool:
    return bool(re.search(r"^\s*\|?.*\|.*\n\s*\|?\s*:?-{3,}", markdown, re.MULTILINE))


def _letter_count(text: str) -> int:
    return sum(character.isalpha() for character in text)


def _ocr_markdown_score(markdown: str) -> int:
    table_bonus = 10_000 if _contains_markdown_table(markdown) else 0
    suspicious_penalty = 25 * len(re.findall(r"(?<=[^\W\d_])[$\"]|[$\"](?=[^\W\d_])", markdown))
    return table_bonus + _letter_count(markdown) - suspicious_penalty


def _ocr_conversion_error(exc: Exception) -> ConversionError:
    detail = str(exc).casefold()
    if "not valid" in detail or "invalid" in detail or "malformed" in detail:
        return ConversionError(
            "El motor OCR local no pudo abrir este PDF. "
            "El documento puede usar una estructura interna incompatible."
        )
    return ConversionError("El OCR local no pudo procesar las páginas seleccionadas.")


def _page_ranges(page_numbers: set[int]) -> list[tuple[int, int]]:
    ordered = sorted(page for page in page_numbers if page >= 1)
    if not ordered:
        return []

    ranges: list[tuple[int, int]] = []
    first = previous = ordered[0]
    for page_number in ordered[1:]:
        batch_size = previous - first + 1
        if page_number == previous + 1 and batch_size < _MAX_PAGES_PER_BATCH:
            previous = page_number
            continue
        ranges.append((first, previous))
        first = previous = page_number
    ranges.append((first, previous))
    return ranges


def _worker_page_batches(page_numbers: set[int]) -> list[set[int]]:
    """Group bounded OCR ranges without rebuilding the model for every gap."""

    batches: list[set[int]] = []
    current: set[int] = set()
    for first_page, last_page in _page_ranges(page_numbers):
        page_range = set(range(first_page, last_page + 1)) & page_numbers
        if current and len(current) + len(page_range) > _MAX_PAGES_PER_WORKER:
            batches.append(current)
            current = set()
        current.update(page_range)
    if current:
        batches.append(current)
    return batches


def _clean_ocr_markdown(markdown: str) -> str:
    markdown = _INVALID_XML_CONTROL_PATTERN.sub(" ", markdown)
    cleaned_lines = [
        _collapse_heading_repetitions(_normalize_spaced_heading_markers(line.strip()))
        for line in _IMAGE_PLACEHOLDER_PATTERN.sub("", markdown).splitlines()
    ]
    while cleaned_lines and not cleaned_lines[0]:
        cleaned_lines.pop(0)
    while cleaned_lines and not cleaned_lines[-1]:
        cleaned_lines.pop()
    if len(cleaned_lines) >= 2 and _STANDALONE_PAGE_NUMBER_PATTERN.fullmatch(cleaned_lines[0]):
        cleaned_lines.pop(0)
    nonempty_lines = [line for line in cleaned_lines if line]
    if len(nonempty_lines) <= 8 and any(line.startswith("#") for line in nonempty_lines):
        cleaned_lines = [
            line for line in cleaned_lines if not _STANDALONE_PAGE_NUMBER_PATTERN.fullmatch(line)
        ]

    compacted: list[str] = []
    for line in cleaned_lines:
        if not line and (not compacted or not compacted[-1]):
            continue
        compacted.append(line)
    return _separate_ocr_table_captions("\n".join(compacted).strip())


def _normalize_spaced_heading_markers(line: str) -> str:
    """Repair OCR output that separates every marker in an ATX heading."""

    match = _SPACED_HEADING_MARKERS_PATTERN.fullmatch(line)
    if match is None:
        return line
    level = match.group("markers").count("#")
    return f"{'#' * level} {match.group('body')}"


def _separate_ocr_table_captions(markdown: str) -> str:
    """Detach a recognized table caption merged into an OCR header row."""

    lines = markdown.splitlines()
    repaired: list[str] = []
    index = 0
    while index < len(lines):
        if index + 1 >= len(lines) or _TABLE_DIVIDER_PATTERN.fullmatch(lines[index + 1]) is None:
            repaired.append(lines[index])
            index += 1
            continue
        header_cells = _markdown_row_cells(lines[index])
        divider_cells = _markdown_row_cells(lines[index + 1])
        if (
            len(header_cells) != len(divider_cells) + 1
            or not header_cells
            or _TABLE_CAPTION_PATTERN.match(header_cells[0]) is None
        ):
            repaired.append(lines[index])
            index += 1
            continue
        repaired.extend(
            (
                header_cells[0],
                "",
                _markdown_table_row(header_cells[1:]),
                lines[index + 1],
            )
        )
        index += 2
    return "\n".join(repaired)


def _markdown_row_cells(line: str) -> tuple[str, ...]:
    content = line.strip()
    if content.startswith("|"):
        content = content[1:]
    if re.search(r"(?<!\\)\|\s*$", content):
        content = re.sub(r"(?<!\\)\|\s*$", "", content)
    return tuple(cell.strip() for cell in _UNESCAPED_PIPE_PATTERN.split(content))


def _markdown_table_row(cells: tuple[str, ...]) -> str:
    return "| " + " | ".join(cells) + " |"


def _collapse_heading_repetitions(line: str) -> str:
    match = _HEADING_PATTERN.match(line)
    if match is None:
        return line

    prefix, text = match.groups()
    tokens = text.split()
    collapsed: list[str] = []
    index = 0
    while index < len(tokens):
        matched = False
        maximum_size = min(6, (len(tokens) - index) // 3)
        for size in range(maximum_size, 0, -1):
            group = tokens[index : index + size]
            repetitions = 1
            while tokens[index + repetitions * size : index + (repetitions + 1) * size] == group:
                repetitions += 1
            if repetitions >= 3:
                collapsed.extend(group)
                index += repetitions * size
                matched = True
                break
        if not matched:
            collapsed.append(tokens[index])
            index += 1
    return f"{prefix}{' '.join(collapsed)}"
