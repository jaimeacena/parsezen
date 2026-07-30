"""Conservative local PDF extraction with structure, links, and selective OCR."""

from __future__ import annotations

import inspect
import json
import logging
import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath
from statistics import median
from typing import Any, TypedDict
from urllib.parse import quote, unquote, urlsplit

import pdfplumber
from pdfplumber.utils.exceptions import MalformedPDFException, PdfminerException

from parsezen.cancellation import CancellationToken, check_cancelled
from parsezen.document_model import RESOURCE_REFERENCE_PREFIX
from parsezen.errors import ConversionError
from parsezen.ocr_conversion import convert_pdf_pages_with_ocr

LOGGER = logging.getLogger(__name__)

_WHITESPACE_PATTERN = re.compile(r"[\t \u00a0]+")
_PAGE_NUMBER_PATTERN = re.compile(r"(?:\d+|[ivxlcdm]+)", re.IGNORECASE)
_RUNNING_FOOTER_PATTERN = re.compile(
    r"(?:\d+\s+[^\d]{2,40}|[^\d]{2,40}\s+\d+)",
    re.IGNORECASE,
)
_LETTER_PATTERN = re.compile(r"[^\W\d_]", re.UNICODE)
_BULLET_PATTERN = re.compile(r"^[•●◦▪‣⁃]\s*")
_SECTION_HEADING_PATTERN = re.compile(
    r"^(?:chapter|cap[ií]tulo|part|parte|day|d[ií]a)\s+[\divxlcdm]+[:.]?$",
    re.IGNORECASE,
)
_SPACED_WORD_PATTERN = re.compile(
    r"(?<!\w)(?:[^\W\d_]\s+){4,}[^\W\d_](?!\w)",
    re.UNICODE,
)
_ADJACENT_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(<([^>]+)>\)\s+\[([^\]]+)\]\(<\2>\)")
_MAX_HEADING_LENGTH = 120
_DUPLICATE_OVERLAP_RATIO = 0.55
_DEDUPLICATION_GRID_SIZE = 16.0
_MAX_CHARACTER_GRID_CELLS = 1_024
_PDF_PAGE_CHECKPOINT_VERSION = 5
_MAX_PDF_PAGE_CHECKPOINT_BYTES = 8 * 1024 * 1024
_MAX_PDF_PAGE_LINES = 50_000
_MAX_PDF_LINE_CHARACTERS = 100_000
_MAX_PDF_LINE_LINKS = 10_000
_GRAPHIC_WARNING_LETTER_LIMIT = 40
_MIN_USABLE_NATIVE_LETTERS = 10
_LOW_NATIVE_QUALITY_THRESHOLD = 0.48
_VERY_LOW_NATIVE_QUALITY_THRESHOLD = 0.28
_MIN_OCR_REPLACEMENT_QUALITY = 0.50
_MIN_OCR_IMAGE_AREA_RATIO = 0.05
_FULL_PAGE_IMAGE_AREA_RATIO = 0.85
_MIN_EXPORTED_IMAGE_AREA_RATIO = 0.015
_MAX_EXPORTED_IMAGE_AREA_RATIO = 0.75
_MAX_EXPORTED_PDF_IMAGES = 500
_MAX_EXPORTED_IMAGE_BYTES = 12 * 1024 * 1024
_FULL_PAGE_EXPORT_LETTER_LIMIT = 100
_MIN_COLUMN_LINES = 3
_MIN_VECTOR_CURVES = 3
_MAX_CLUSTERED_VECTOR_CURVES = 500
_VECTOR_CLUSTER_GAP = 12.0
_MARKDOWN_TABLE_PATTERN = re.compile(r"^\s*\|?.*\|.*\n\s*\|?\s*:?-{3,}", re.MULTILINE)
_PDF_WARNING_PAGE_PATTERN = re.compile(
    r"^>\s*\*\*Aviso(?: OCR| de conversión) \(página (\d+)\):\*\*",
    re.MULTILINE,
)
_PDF_WARNING_MESSAGE_PATTERN = re.compile(
    r"^>\s*\*\*Aviso(?: OCR| de conversión) \(página \d+\):\*\*\s*"
)
_PDF_PAGE_MARKER_PATTERN = re.compile(r"<!--\s*PZDOC PDF PAGE \d+\s*-->", re.IGNORECASE)


def strip_pdf_page_markers(markdown: str) -> str:
    """Remove private page anchors before publishing user-visible text."""
    return _PDF_PAGE_MARKER_PATTERN.sub("", markdown)


@dataclass(frozen=True, slots=True)
class _PdfLink:
    target: str
    x0: float
    x1: float
    top: float
    bottom: float


@dataclass(frozen=True, slots=True)
class _PdfCharacter:
    """Only the character fields needed after pdfplumber releases a page."""

    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    size: float
    upright: bool


@dataclass(frozen=True, slots=True)
class _PdfLine:
    page_number: int
    page_width: float
    page_height: float
    text: str
    chars: tuple[_PdfCharacter, ...]
    x0: float
    x1: float
    top: float
    bottom: float
    font_size: float
    bold: bool
    links: tuple[_PdfLink, ...]
    soft_hyphen_end: bool
    hard_hyphen_end: bool
    rotated: bool

    @property
    def centered(self) -> bool:
        line_center = (self.x0 + self.x1) / 2
        return abs(line_center - self.page_width / 2) <= self.page_width * 0.09


@dataclass(frozen=True, slots=True)
class _PdfPage:
    number: int
    lines: tuple[_PdfLine, ...]
    has_images: bool
    image_area_ratios: tuple[float, ...]
    has_table: bool
    image_orientation_mismatch: bool


@dataclass(slots=True)
class _MarkdownBlock:
    kind: str
    text: str
    page_number: int
    level: int | None = None
    source_line: _PdfLine | None = None


@dataclass(frozen=True, slots=True)
class PdfPageRange:
    """One inclusive, one-based page range requested for a PDF."""

    first_page: int
    last_page: int


class PdfProgressPhase(StrEnum):
    """Real, measurable phases of one PDF conversion."""

    EXTRACTING = "extracting"
    OCR = "ocr"
    IMAGES = "images"
    STRUCTURING = "structuring"


PdfProgressCallback = Callable[[PdfProgressPhase, int, int], None]


@dataclass(frozen=True, slots=True)
class PdfReviewIssue:
    """One PDF page whose converted content needs a human comparison."""

    page_number: int
    message: str
    markdown: str
    identifier: str = ""
    blocking: bool = False
    target_marker: str = ""


@dataclass(frozen=True, slots=True)
class PdfQualityReport:
    """Small, in-memory summary used by the result review UI."""

    processed_pages: tuple[int, ...]
    ocr_pages: tuple[int, ...]
    issues: tuple[PdfReviewIssue, ...]
    ocr_replaced_pages: tuple[int, ...] = ()
    low_confidence_pages: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class PdfEmbeddedResource:
    """One meaningful PDF image rendered for portable Markdown or EPUB output."""

    relative_path: PurePosixPath
    content: bytes
    media_type: str
    page_number: int


@dataclass(frozen=True, slots=True)
class PdfConversionResult:
    """Structured Markdown and the images explicitly selected for preservation."""

    markdown: str
    resources: tuple[PdfEmbeddedResource, ...] = ()
    omitted_images: int = 0


@dataclass(frozen=True, slots=True)
class _PdfOcrPlan:
    page_numbers: set[int]
    force_full_page_numbers: set[int]
    required_page_numbers: set[int]


class _OcrCheckpointArguments(TypedDict, total=False):
    on_page_result: Callable[[int, str], None]


def extract_pdf_warning_pages(markdown: str) -> tuple[int, ...]:
    """Return sorted PDF page numbers explicitly marked for human review."""
    return tuple(
        sorted({int(match.group(1)) for match in _PDF_WARNING_PAGE_PATTERN.finditer(markdown)})
    )


def resolve_pdf_page_range(source_path: Path, requested: PdfPageRange) -> PdfPageRange:
    """Clamp a valid requested range to the real final page of a local PDF."""
    _validate_pdf_header(source_path)
    _validate_page_range_values(requested)
    try:
        with pdfplumber.open(source_path, unicode_norm="NFC") as pdf:
            return _resolve_page_range(len(pdf.pages), requested)
    except (MalformedPDFException, PdfminerException, OSError, ValueError) as exc:
        raise ConversionError(f"No se pudo abrir el PDF {source_path.name}.") from exc


def render_pdf_page_cover(source_path: Path, page_number: int = 1) -> bytes:
    """Render one real PDF page as a bounded JPEG suitable for an EPUB cover."""
    _validate_pdf_header(source_path)
    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
        raise ConversionError("La página elegida como portada no es válida.")
    try:
        with pdfplumber.open(source_path, unicode_norm="NFC") as pdf:
            if page_number > len(pdf.pages):
                raise ConversionError(
                    f"El PDF no contiene la página {page_number} elegida como portada."
                )
            page = pdf.pages[page_number - 1]
            try:
                x0, top, x1, bottom = (float(value) for value in page.bbox)
                content = _render_pdf_image(page, (x0, top, x1, bottom))
                if len(content) > _MAX_EXPORTED_IMAGE_BYTES:
                    raise ConversionError(
                        "La página elegida como portada genera una imagen excesiva."
                    )
                return content
            finally:
                page.close()
    except ConversionError:
        raise
    except (MalformedPDFException, PdfminerException, OSError, TypeError, ValueError) as exc:
        raise ConversionError(
            f"No se pudo preparar la primera página de {source_path.name} como portada."
        ) from exc


def convert_pdf(
    source_path: Path,
    on_ocr_start: Callable[[], None] | None = None,
    on_quality_report: Callable[[PdfQualityReport], None] | None = None,
    cancellation: CancellationToken | None = None,
    page_range: PdfPageRange | None = None,
    force_ocr: bool = False,
    on_progress: PdfProgressCallback | None = None,
    load_ocr_checkpoint: Callable[[int], str | None] | None = None,
    save_ocr_checkpoint: Callable[[int, str], bool] | None = None,
    load_page_checkpoint: Callable[[int], str | None] | None = None,
    save_page_checkpoint: Callable[[int, str], bool] | None = None,
) -> str:
    """Return structured Markdown from a local PDF, using selective OCR when needed."""
    return convert_pdf_document(
        source_path,
        on_ocr_start=on_ocr_start,
        on_quality_report=on_quality_report,
        cancellation=cancellation,
        page_range=page_range,
        force_ocr=force_ocr,
        on_progress=on_progress,
        load_ocr_checkpoint=load_ocr_checkpoint,
        save_ocr_checkpoint=save_ocr_checkpoint,
        load_page_checkpoint=load_page_checkpoint,
        save_page_checkpoint=save_page_checkpoint,
    ).markdown


def convert_pdf_document(
    source_path: Path,
    on_ocr_start: Callable[[], None] | None = None,
    on_quality_report: Callable[[PdfQualityReport], None] | None = None,
    cancellation: CancellationToken | None = None,
    page_range: PdfPageRange | None = None,
    force_ocr: bool = False,
    on_progress: PdfProgressCallback | None = None,
    load_ocr_checkpoint: Callable[[int], str | None] | None = None,
    save_ocr_checkpoint: Callable[[int, str], bool] | None = None,
    load_page_checkpoint: Callable[[int], str | None] | None = None,
    save_page_checkpoint: Callable[[int, str], bool] | None = None,
    *,
    include_images: bool = False,
) -> PdfConversionResult:
    """Return PDF Markdown with optional, meaningful raster resources."""
    check_cancelled(cancellation)
    _validate_pdf_header(source_path)
    try:
        pages = _extract_pages(
            source_path,
            cancellation,
            page_range,
            on_progress,
            load_page_checkpoint,
            save_page_checkpoint,
        )
    except (MalformedPDFException, PdfminerException, OSError, ValueError) as exc:
        raise ConversionError(f"No se pudo abrir el PDF {source_path.name}.") from exc

    lines = [line for page in pages for line in page.lines]
    ocr_plan = _build_ocr_plan(pages, force_ocr)
    ocr_pages, ocr_failed_pages = _run_planned_ocr(
        source_path,
        ocr_plan,
        on_ocr_start,
        cancellation,
        on_progress,
        load_ocr_checkpoint,
        save_ocr_checkpoint,
    )
    check_cancelled(cancellation)
    empty_result = _empty_pdf_result(
        pages,
        lines,
        ocr_plan,
        ocr_pages,
        force_ocr,
        on_quality_report,
    )
    if empty_result is not None:
        return PdfConversionResult(empty_result)

    page_images: dict[int, tuple[PdfEmbeddedResource, ...]] = {}
    omitted_images = 0
    if include_images:
        page_images, omitted_images = _extract_embedded_images(
            source_path,
            pages,
            ocr_pages,
            cancellation,
            page_range,
            on_progress,
        )

    body_size = _dominant_body_size(lines)
    heading_sizes = _heading_size_levels(lines, body_size)
    repeated_margins = _repeated_margin_lines(lines, len(pages))
    referenced_pages = _referenced_pages(lines)
    markdown, review_issues = _render_document(
        pages,
        body_size,
        heading_sizes,
        repeated_margins,
        referenced_pages,
        ocr_pages,
        ocr_failed_pages,
        page_images,
        on_progress,
    )
    markdown = markdown.strip()
    check_cancelled(cancellation)
    if not _PDF_PAGE_MARKER_PATTERN.sub("", markdown).strip():
        raise ConversionError("No se pudo extraer contenido textual útil del PDF.")
    _notify_pdf_quality(
        on_quality_report,
        pages,
        ocr_plan.page_numbers,
        review_issues,
        ocr_pages,
    )
    return PdfConversionResult(
        f"{markdown}\n",
        tuple(resource for page in page_images.values() for resource in page),
        omitted_images,
    )


def _build_ocr_plan(pages: list[_PdfPage], force_ocr: bool) -> _PdfOcrPlan:
    page_numbers = {page.number for page in pages} if force_ocr else _pages_requiring_ocr(pages)
    force_full_page_numbers = {
        page.number
        for page in pages
        if (
            _has_suspicious_glyph_encoding(page)
            or page.image_orientation_mismatch
            or _native_page_quality(page) < _VERY_LOW_NATIVE_QUALITY_THRESHOLD
        )
    }
    if force_ocr:
        force_full_page_numbers.update(page_numbers)
    required_page_numbers = {
        page.number
        for page in pages
        if (_page_letter_count(page) < _MIN_USABLE_NATIVE_LETTERS and page.has_images)
        or _native_page_quality(page) < 0.16
    }
    return _PdfOcrPlan(page_numbers, force_full_page_numbers, required_page_numbers)


def _run_planned_ocr(
    source_path: Path,
    plan: _PdfOcrPlan,
    on_ocr_start: Callable[[], None] | None,
    cancellation: CancellationToken | None,
    on_progress: PdfProgressCallback | None,
    load_checkpoint: Callable[[int], str | None] | None,
    save_checkpoint: Callable[[int, str], bool] | None,
) -> tuple[dict[int, str], set[int]]:
    if not plan.page_numbers:
        return {}, set()
    check_cancelled(cancellation)
    cached_pages: dict[int, str] = {}
    if load_checkpoint is not None:
        for page_number in sorted(plan.page_numbers):
            cached = load_checkpoint(page_number)
            if cached is not None and "\0" not in cached:
                cached_pages[page_number] = cached
    pending_pages = plan.page_numbers - cached_pages.keys()
    if not pending_pages:
        if on_progress is not None:
            on_progress(PdfProgressPhase.OCR, len(cached_pages), len(plan.page_numbers))
        return cached_pages, set()
    if on_ocr_start is not None:
        on_ocr_start()
    progress = (
        (
            lambda current, _total: on_progress(
                PdfProgressPhase.OCR,
                len(cached_pages) + current,
                len(plan.page_numbers),
            )
        )
        if on_progress is not None
        else None
    )
    if on_progress is not None and cached_pages:
        on_progress(PdfProgressPhase.OCR, len(cached_pages), len(plan.page_numbers))

    def persist_page(page_number: int, markdown: str) -> None:
        if save_checkpoint is not None:
            save_checkpoint(page_number, markdown)

    checkpoint_arguments: _OcrCheckpointArguments = {}
    ocr_parameters = inspect.signature(convert_pdf_pages_with_ocr).parameters
    if save_checkpoint is not None and "on_page_result" in ocr_parameters:
        checkpoint_arguments["on_page_result"] = persist_page
    force_full_page_numbers = (
        plan.force_full_page_numbers & pending_pages
        if "force_full_page_numbers" in ocr_parameters
        else set()
    )

    try:
        if cancellation is None:
            if force_full_page_numbers:
                if progress is None:
                    ocr_pages = convert_pdf_pages_with_ocr(
                        source_path,
                        pending_pages,
                        force_full_page_numbers=force_full_page_numbers,
                        **checkpoint_arguments,
                    )
                else:
                    ocr_pages = convert_pdf_pages_with_ocr(
                        source_path,
                        pending_pages,
                        force_full_page_numbers=force_full_page_numbers,
                        on_progress=progress,
                        **checkpoint_arguments,
                    )
            else:
                if progress is None:
                    ocr_pages = convert_pdf_pages_with_ocr(
                        source_path,
                        pending_pages,
                        **checkpoint_arguments,
                    )
                else:
                    ocr_pages = convert_pdf_pages_with_ocr(
                        source_path,
                        pending_pages,
                        on_progress=progress,
                        **checkpoint_arguments,
                    )
        elif force_full_page_numbers:
            if progress is None:
                ocr_pages = convert_pdf_pages_with_ocr(
                    source_path,
                    pending_pages,
                    cancellation,
                    force_full_page_numbers=force_full_page_numbers,
                    **checkpoint_arguments,
                )
            else:
                ocr_pages = convert_pdf_pages_with_ocr(
                    source_path,
                    pending_pages,
                    cancellation,
                    force_full_page_numbers=force_full_page_numbers,
                    on_progress=progress,
                    **checkpoint_arguments,
                )
        elif progress is None:
            ocr_pages = convert_pdf_pages_with_ocr(
                source_path,
                pending_pages,
                cancellation,
                **checkpoint_arguments,
            )
        else:
            ocr_pages = convert_pdf_pages_with_ocr(
                source_path,
                pending_pages,
                cancellation,
                on_progress=progress,
                **checkpoint_arguments,
            )
    except ConversionError:
        if plan.required_page_numbers:
            raise
        return {}, set(plan.page_numbers)
    ocr_pages = {**cached_pages, **ocr_pages}
    return ocr_pages, set(plan.page_numbers) - set(ocr_pages)


def _empty_pdf_result(
    pages: list[_PdfPage],
    lines: list[_PdfLine],
    plan: _PdfOcrPlan,
    ocr_pages: dict[int, str],
    force_ocr: bool,
    on_quality_report: Callable[[PdfQualityReport], None] | None,
) -> str | None:
    has_native_text = any(_LETTER_PATTERN.search(line.text) and not line.rotated for line in lines)
    has_ocr_text = any(_LETTER_PATTERN.search(markdown) for markdown in ocr_pages.values())
    if has_native_text or has_ocr_text:
        return None
    image_pages = tuple(page for page in pages if page.has_images)
    if not image_pages:
        raise ConversionError("El PDF no contiene texto ni imágenes reconocibles.")
    if not force_ocr:
        raise ConversionError("El OCR local no pudo reconocer texto útil en este PDF escaneado.")

    page_warnings = tuple(
        (
            f"> **Aviso OCR (página {page.number}):** no se encontró texto legible "
            "en este segundo análisis. Puede ser una página decorativa o una imagen "
            "sin texto; compárala con el PDF original."
        )
        for page in image_pages
    )
    issues = tuple(
        PdfReviewIssue(
            page_number=page.number,
            message=_warning_message(warning),
            markdown="",
            identifier=_pdf_issue_identifier(page.number, "", warning),
            blocking=True,
            target_marker=_pdf_page_marker(page.number),
        )
        for page, warning in zip(image_pages, page_warnings, strict=True)
    )
    _notify_pdf_quality(on_quality_report, pages, plan.page_numbers, issues)
    # Continue through the normal renderer. When images are requested it can
    # still produce a faithful image-only document; otherwise the normal empty
    # content guard will explain that no useful text was found. Diagnostic copy
    # never becomes part of the reader-visible document.
    return None


def _notify_pdf_quality(
    on_quality_report: Callable[[PdfQualityReport], None] | None,
    pages: list[_PdfPage],
    ocr_page_numbers: set[int],
    issues: tuple[PdfReviewIssue, ...],
    ocr_markdown: dict[int, str] | None = None,
) -> None:
    if on_quality_report is None:
        return
    on_quality_report(
        PdfQualityReport(
            processed_pages=tuple(page.number for page in pages),
            ocr_pages=tuple(sorted(ocr_page_numbers)),
            issues=issues,
            ocr_replaced_pages=tuple(
                page.number
                for page in pages
                if ocr_markdown is not None
                and page.number in ocr_markdown
                and _should_replace_with_ocr(page, ocr_markdown[page.number])
            ),
            low_confidence_pages=tuple(
                page.number
                for page in pages
                if _native_page_quality(page) < _LOW_NATIVE_QUALITY_THRESHOLD
            ),
        )
    )


def _validate_pdf_header(source_path: Path) -> None:
    try:
        with source_path.open("rb") as source_file:
            header = source_file.read(5)
    except OSError as exc:
        raise ConversionError(f"No se pudo leer el PDF {source_path.name}.") from exc
    if header != b"%PDF-":
        raise ConversionError(f"{source_path.name} no es un documento PDF válido.")


def _extract_pages(
    source_path: Path,
    cancellation: CancellationToken | None = None,
    page_range: PdfPageRange | None = None,
    on_progress: PdfProgressCallback | None = None,
    load_checkpoint: Callable[[int], str | None] | None = None,
    save_checkpoint: Callable[[int, str], bool] | None = None,
) -> list[_PdfPage]:
    with pdfplumber.open(source_path, unicode_norm="NFC") as pdf:
        resolved_range = (
            _resolve_page_range(len(pdf.pages), page_range)
            if page_range is not None
            else PdfPageRange(1, len(pdf.pages))
        )
        selected_pages = pdf.pages[resolved_range.first_page - 1 : resolved_range.last_page]
        page_ids = {
            page_id: page_number
            for page_number, page in enumerate(
                selected_pages,
                start=resolved_range.first_page,
            )
            if isinstance((page_id := page.page_obj.pageid), int)
        }
        pages: list[_PdfPage] = []
        total_pages = len(selected_pages)
        for current, (page_number, page) in enumerate(
            zip(
                range(resolved_range.first_page, resolved_range.last_page + 1),
                selected_pages,
                strict=True,
            ),
            start=1,
        ):
            check_cancelled(cancellation)
            if load_checkpoint is not None:
                cached_page = _deserialize_page_checkpoint(
                    load_checkpoint(page_number),
                    page_number,
                )
                if cached_page is not None:
                    pages.append(cached_page)
                    page.close()
                    if on_progress is not None:
                        on_progress(PdfProgressPhase.EXTRACTING, current, total_pages)
                    continue
            links = _extract_links(page.annots, page_ids)
            filtered_page = _deduplicated_page(page)
            raw_lines = filtered_page.extract_text_lines(
                strip=True,
                return_chars=True,
            )
            built_lines: list[_PdfLine] = []
            for raw_line in raw_lines:
                line = _build_line(
                    page_number,
                    page.width,
                    page.height,
                    raw_line,
                    links,
                )
                if line is not None:
                    built_lines.extend(_split_wide_line(line))
            lines = tuple(_reading_order_lines(built_lines))
            extracted_page = _PdfPage(
                number=page_number,
                lines=lines,
                has_images=bool(page.images),
                image_area_ratios=_image_area_ratios(page),
                has_table=_has_table_candidate(page),
                image_orientation_mismatch=_image_orientation_mismatch(page),
            )
            pages.append(extracted_page)
            filtered_page.close()
            page.close()
            if save_checkpoint is not None:
                save_checkpoint(page_number, _serialize_page_checkpoint(extracted_page))
            if on_progress is not None:
                on_progress(PdfProgressPhase.EXTRACTING, current, total_pages)
        check_cancelled(cancellation)
        return pages


def _serialize_page_checkpoint(page: _PdfPage) -> str:
    record = {
        "version": _PDF_PAGE_CHECKPOINT_VERSION,
        "page": page.number,
        "has_images": page.has_images,
        "image_area_ratios": list(page.image_area_ratios),
        "has_table": page.has_table,
        "image_orientation_mismatch": page.image_orientation_mismatch,
        "lines": [
            {
                "page_width": line.page_width,
                "page_height": line.page_height,
                "text": line.text,
                "chars": [
                    [
                        character.text,
                        character.x0,
                        character.x1,
                        character.top,
                        character.bottom,
                        character.size,
                        character.upright,
                    ]
                    for character in line.chars
                ],
                "x0": line.x0,
                "x1": line.x1,
                "top": line.top,
                "bottom": line.bottom,
                "font_size": line.font_size,
                "bold": line.bold,
                "links": [
                    [link.target, link.x0, link.x1, link.top, link.bottom] for link in line.links
                ],
                "soft_hyphen_end": line.soft_hyphen_end,
                "hard_hyphen_end": line.hard_hyphen_end,
                "rotated": line.rotated,
            }
            for line in page.lines
        ],
    }
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


def _deserialize_page_checkpoint(payload: str | None, page_number: int) -> _PdfPage | None:
    if payload is None:
        return None
    try:
        payload_size = len(payload.encode("utf-8"))
    except UnicodeError:
        return None
    if payload_size > _MAX_PDF_PAGE_CHECKPOINT_BYTES:
        return None
    try:
        raw = json.loads(payload)
        return _page_from_checkpoint(raw, page_number)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _page_from_checkpoint(raw: object, page_number: int) -> _PdfPage:
    if not isinstance(raw, dict) or set(raw) != {
        "version",
        "page",
        "has_images",
        "image_area_ratios",
        "has_table",
        "image_orientation_mismatch",
        "lines",
    }:
        raise ValueError
    if (
        raw["version"] != _PDF_PAGE_CHECKPOINT_VERSION
        or isinstance(raw["page"], bool)
        or raw["page"] != page_number
        or type(raw["has_images"]) is not bool
        or type(raw["has_table"]) is not bool
        or type(raw["image_orientation_mismatch"]) is not bool
    ):
        raise ValueError
    raw_ratios = raw["image_area_ratios"]
    raw_lines = raw["lines"]
    if (
        not isinstance(raw_ratios, list)
        or len(raw_ratios) > _MAX_PDF_LINE_CHARACTERS
        or not isinstance(raw_lines, list)
        or len(raw_lines) > _MAX_PDF_PAGE_LINES
    ):
        raise ValueError
    ratios = tuple(_checkpoint_float(value, minimum=0, maximum=100) for value in raw_ratios)
    lines = tuple(_line_from_checkpoint(line, page_number) for line in raw_lines)
    return _PdfPage(
        number=page_number,
        lines=lines,
        has_images=raw["has_images"],
        image_area_ratios=ratios,
        has_table=raw["has_table"],
        image_orientation_mismatch=raw["image_orientation_mismatch"],
    )


def _line_from_checkpoint(raw: object, page_number: int) -> _PdfLine:
    if not isinstance(raw, dict) or set(raw) != {
        "page_width",
        "page_height",
        "text",
        "chars",
        "x0",
        "x1",
        "top",
        "bottom",
        "font_size",
        "bold",
        "links",
        "soft_hyphen_end",
        "hard_hyphen_end",
        "rotated",
    }:
        raise ValueError
    text = raw["text"]
    raw_chars = raw["chars"]
    raw_links = raw["links"]
    if (
        not isinstance(text, str)
        or not text
        or "\0" in text
        or not isinstance(raw_chars, list)
        or len(raw_chars) > _MAX_PDF_LINE_CHARACTERS
        or not isinstance(raw_links, list)
        or len(raw_links) > _MAX_PDF_LINE_LINKS
        or type(raw["bold"]) is not bool
        or type(raw["soft_hyphen_end"]) is not bool
        or type(raw["hard_hyphen_end"]) is not bool
        or type(raw["rotated"]) is not bool
    ):
        raise ValueError
    page_width = _checkpoint_float(raw["page_width"], minimum=0.01)
    page_height = _checkpoint_float(raw["page_height"], minimum=0.01)
    return _PdfLine(
        page_number=page_number,
        page_width=page_width,
        page_height=page_height,
        text=text,
        chars=tuple(_character_from_checkpoint(value) for value in raw_chars),
        x0=_checkpoint_float(raw["x0"]),
        x1=_checkpoint_float(raw["x1"]),
        top=_checkpoint_float(raw["top"]),
        bottom=_checkpoint_float(raw["bottom"]),
        font_size=_checkpoint_float(raw["font_size"], minimum=0.01),
        bold=raw["bold"],
        links=tuple(_link_from_checkpoint(value) for value in raw_links),
        soft_hyphen_end=raw["soft_hyphen_end"],
        hard_hyphen_end=raw["hard_hyphen_end"],
        rotated=raw["rotated"],
    )


def _character_from_checkpoint(raw: object) -> _PdfCharacter:
    if not isinstance(raw, list) or len(raw) != 7:
        raise ValueError
    text = raw[0]
    if not isinstance(text, str) or "\0" in text or len(text) > 4_096 or type(raw[6]) is not bool:
        raise ValueError
    return _PdfCharacter(
        text=text,
        x0=_checkpoint_float(raw[1]),
        x1=_checkpoint_float(raw[2]),
        top=_checkpoint_float(raw[3]),
        bottom=_checkpoint_float(raw[4]),
        size=_checkpoint_float(raw[5], minimum=0),
        upright=raw[6],
    )


def _link_from_checkpoint(raw: object) -> _PdfLink:
    if not isinstance(raw, list) or len(raw) != 5:
        raise ValueError
    target = raw[0]
    if not isinstance(target, str) or not target or "\0" in target or len(target) > 32_768:
        raise ValueError
    return _PdfLink(
        target=target,
        x0=_checkpoint_float(raw[1]),
        x1=_checkpoint_float(raw[2]),
        top=_checkpoint_float(raw[3]),
        bottom=_checkpoint_float(raw[4]),
    )


def _checkpoint_float(
    value: object,
    *,
    minimum: float = -10_000_000,
    maximum: float = 10_000_000,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError
    converted = float(value)
    if not math.isfinite(converted) or not minimum <= converted <= maximum:
        raise ValueError
    return converted


def _validate_page_range_values(page_range: PdfPageRange) -> None:
    if (
        isinstance(page_range.first_page, bool)
        or isinstance(page_range.last_page, bool)
        or not isinstance(page_range.first_page, int)
        or not isinstance(page_range.last_page, int)
        or page_range.first_page < 1
        or page_range.last_page < page_range.first_page
    ):
        raise ConversionError("El rango de páginas seleccionado no es válido.")


def _resolve_page_range(page_count: int, requested: PdfPageRange) -> PdfPageRange:
    _validate_page_range_values(requested)
    if page_count < 1:
        raise ConversionError("El PDF no contiene páginas.")
    if requested.first_page > page_count:
        noun = "página" if page_count == 1 else "páginas"
        raise ConversionError(
            f"El PDF tiene {page_count} {noun}; el rango empieza en la "
            f"página {requested.first_page}."
        )
    return PdfPageRange(
        first_page=requested.first_page,
        last_page=min(requested.last_page, page_count),
    )


def _image_area_ratios(page: Any) -> tuple[float, ...]:
    page_area = max(float(page.width) * float(page.height), 1.0)
    page_x0, page_top, page_x1, page_bottom = (float(value) for value in page.bbox)
    ratios: list[float] = []
    for image in page.images:
        try:
            x0 = max(page_x0, float(image["x0"]))
            x1 = min(page_x1, float(image["x1"]))
            top = max(page_top, float(image["top"]))
            bottom = min(page_bottom, float(image["bottom"]))
        except (KeyError, TypeError, ValueError):
            continue
        area = max(0.0, x1 - x0) * max(0.0, bottom - top)
        if area > 0:
            ratios.append(min(area / page_area, 1.0))
    return tuple(ratios)


def _image_orientation_mismatch(page: Any) -> bool:
    if not page.images:
        return False
    dominant = max(
        page.images,
        key=lambda image: float(image.get("width", 0)) * float(image.get("height", 0)),
    )
    source_size = dominant.get("srcsize")
    if not isinstance(source_size, tuple) or len(source_size) != 2:
        return False
    source_landscape = float(source_size[0]) > float(source_size[1])
    displayed_landscape = float(dominant.get("width", 0)) > float(dominant.get("height", 0))
    return source_landscape != displayed_landscape


def _extract_embedded_images(
    source_path: Path,
    pages: list[_PdfPage],
    ocr_pages: dict[int, str],
    cancellation: CancellationToken | None,
    page_range: PdfPageRange | None,
    on_progress: PdfProgressCallback | None,
) -> tuple[dict[int, tuple[PdfEmbeddedResource, ...]], int]:
    """Render meaningful placed images while filtering page scans and backgrounds."""
    page_models = {page.number: page for page in pages}
    selected: dict[int, tuple[PdfEmbeddedResource, ...]] = {}
    seen_content: set[str] = set()
    omitted = 0
    exported = 0
    try:
        with pdfplumber.open(source_path, unicode_norm="NFC") as pdf:
            resolved_range = (
                _resolve_page_range(len(pdf.pages), page_range)
                if page_range is not None
                else PdfPageRange(1, len(pdf.pages))
            )
            pdf_pages = pdf.pages[resolved_range.first_page - 1 : resolved_range.last_page]
            total = len(pdf_pages)
            for current, (page_number, page) in enumerate(
                zip(
                    range(resolved_range.first_page, resolved_range.last_page + 1),
                    pdf_pages,
                    strict=True,
                ),
                start=1,
            ):
                check_cancelled(cancellation)
                model = page_models[page_number]
                candidates = _exportable_image_boxes(page, model, ocr_pages.get(page_number))
                page_resources: list[PdfEmbeddedResource] = []
                for image_index, bbox in enumerate(candidates, start=1):
                    if exported >= _MAX_EXPORTED_PDF_IMAGES:
                        omitted += len(candidates) - image_index + 1
                        break
                    try:
                        content = _render_pdf_image(page, bbox)
                    except (OSError, TypeError, ValueError):
                        omitted += 1
                        LOGGER.warning("pdf_image_export_failed page=%d", page_number)
                        continue
                    if len(content) > _MAX_EXPORTED_IMAGE_BYTES:
                        omitted += 1
                        continue
                    digest = sha256(content).hexdigest()
                    if digest in seen_content:
                        continue
                    seen_content.add(digest)
                    resource = PdfEmbeddedResource(
                        PurePosixPath(f"pdf/page-{page_number:04d}-image-{image_index:02d}.jpg"),
                        content,
                        "image/jpeg",
                        page_number,
                    )
                    page_resources.append(resource)
                    exported += 1
                if page_resources:
                    selected[page_number] = tuple(page_resources)
                page.close()
                if on_progress is not None:
                    on_progress(PdfProgressPhase.IMAGES, current, total)
    except (MalformedPDFException, PdfminerException, OSError, ValueError) as exc:
        raise ConversionError(
            f"No se pudieron conservar las imágenes del PDF {source_path.name}."
        ) from exc
    return selected, omitted


def _exportable_image_boxes(
    page: Any,
    model: _PdfPage,
    ocr_markdown: str | None,
) -> tuple[tuple[float, float, float, float], ...]:
    page_area = max(float(page.width) * float(page.height), 1.0)
    page_x0, page_top, page_x1, page_bottom = (float(value) for value in page.bbox)
    candidates: list[tuple[float, tuple[float, float, float, float]]] = []
    full_page: list[tuple[float, tuple[float, float, float, float]]] = []
    for image in page.images:
        try:
            x0 = max(page_x0, float(image["x0"]))
            x1 = min(page_x1, float(image["x1"]))
            top = max(page_top, float(image["top"]))
            bottom = min(page_bottom, float(image["bottom"]))
        except (KeyError, TypeError, ValueError):
            continue
        width = x1 - x0
        height = bottom - top
        if width < 12 or height < 12:
            continue
        ratio = width * height / page_area
        record = (ratio, (x0, top, x1, bottom))
        if _MIN_EXPORTED_IMAGE_AREA_RATIO <= ratio < _MAX_EXPORTED_IMAGE_AREA_RATIO:
            candidates.append(record)
        elif ratio >= _MAX_EXPORTED_IMAGE_AREA_RATIO:
            full_page.append(record)

    for bbox in _vector_graphic_boxes(page):
        x0, top, x1, bottom = bbox
        ratio = (x1 - x0) * (bottom - top) / page_area
        candidates.append((ratio, bbox))

    # A page-sized image is normally a scan or decorative background. Keep at most one only
    # when the page contains almost no readable content (for example, a cover or plate).
    recognized_letters = _heading_letter_count(ocr_markdown or "")
    useful_letters = max(_page_letter_count(model), recognized_letters)
    if not candidates and full_page and useful_letters < _FULL_PAGE_EXPORT_LETTER_LIMIT:
        candidates.append(max(full_page, key=lambda item: item[0]))
    candidates.sort(key=lambda item: (item[1][1], item[1][0], -item[0]))
    retained: list[tuple[float, float, float, float]] = []
    for _ratio, bbox in candidates:
        if any(_bbox_overlap_ratio(bbox, existing) >= 0.85 for existing in retained):
            continue
        retained.append(bbox)
    return tuple(retained)


def _vector_graphic_boxes(page: Any) -> tuple[tuple[float, float, float, float], ...]:
    """Return conservative crops for clustered curve-based illustrations."""

    page_x0, page_top, page_x1, page_bottom = (float(value) for value in page.bbox)
    page_area = max(float(page.width) * float(page.height), 1.0)
    curves: list[tuple[float, float, float, float]] = []
    for curve in getattr(page, "curves", ()):
        bbox = _curve_bbox(
            curve,
            (page_x0, page_top, page_x1, page_bottom),
        )
        if bbox is None:
            continue
        curves.append(bbox)
    if len(curves) < _MIN_VECTOR_CURVES:
        return ()

    clusters: list[list[tuple[float, float, float, float]]]
    if len(curves) > _MAX_CLUSTERED_VECTOR_CURVES:
        clusters = [curves]
    else:
        clusters = []
        for bbox in sorted(curves, key=lambda item: (item[1], item[0])):
            matching = [
                index
                for index, cluster in enumerate(clusters)
                if _boxes_are_near(_union_bbox(cluster), bbox, _VECTOR_CLUSTER_GAP)
            ]
            if not matching:
                clusters.append([bbox])
                continue
            destination = matching[0]
            clusters[destination].append(bbox)
            for index in reversed(matching[1:]):
                clusters[destination].extend(clusters.pop(index))

    retained: list[tuple[float, float, float, float]] = []
    for cluster in clusters:
        if len(cluster) < _MIN_VECTOR_CURVES:
            continue
        x0, top, x1, bottom = _union_bbox(cluster)
        padding = min(_VECTOR_CLUSTER_GAP / 2, x0 - page_x0, top - page_top)
        x0 = max(page_x0, x0 - padding)
        top = max(page_top, top - padding)
        x1 = min(page_x1, x1 + _VECTOR_CLUSTER_GAP / 2)
        bottom = min(page_bottom, bottom + _VECTOR_CLUSTER_GAP / 2)
        width = x1 - x0
        height = bottom - top
        ratio = width * height / page_area
        if (
            width >= 24
            and height >= 24
            and _MIN_EXPORTED_IMAGE_AREA_RATIO <= ratio < _MAX_EXPORTED_IMAGE_AREA_RATIO
        ):
            retained.append((x0, top, x1, bottom))
    return tuple(retained)


def _curve_bbox(
    curve: dict[str, Any],
    page_bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    points: list[tuple[float, float]] = []
    path = curve.get("path")
    if isinstance(path, (list, tuple)):
        for command in path:
            if not isinstance(command, (list, tuple)):
                continue
            for value in command[1:]:
                if (
                    isinstance(value, (list, tuple))
                    and len(value) == 2
                    and all(isinstance(coordinate, (int, float)) for coordinate in value)
                ):
                    points.append((float(value[0]), float(value[1])))
    if points:
        x0 = min(point[0] for point in points)
        top = min(point[1] for point in points)
        x1 = max(point[0] for point in points)
        bottom = max(point[1] for point in points)
    else:
        try:
            x0 = float(curve["x0"])
            top = float(curve["top"])
            x1 = float(curve["x1"])
            bottom = float(curve["bottom"])
        except (KeyError, TypeError, ValueError):
            return None
    if not all(math.isfinite(value) for value in (x0, top, x1, bottom)):
        return None
    page_x0, page_top, page_x1, page_bottom = page_bbox
    clipped = (
        max(page_x0, x0),
        max(page_top, top),
        min(page_x1, x1),
        min(page_bottom, bottom),
    )
    return clipped if clipped[2] > clipped[0] and clipped[3] > clipped[1] else None


def _union_bbox(
    boxes: list[tuple[float, float, float, float]],
) -> tuple[float, float, float, float]:
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _boxes_are_near(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
    gap: float,
) -> bool:
    return not (
        first[2] + gap < second[0]
        or second[2] + gap < first[0]
        or first[3] + gap < second[1]
        or second[3] + gap < first[1]
    )


def _bbox_overlap_ratio(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    intersection_width = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
    intersection_height = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
    intersection = intersection_width * intersection_height
    first_area = max((first[2] - first[0]) * (first[3] - first[1]), 0.01)
    second_area = max((second[2] - second[0]) * (second[3] - second[1]), 0.01)
    return intersection / min(first_area, second_area)


def _render_pdf_image(
    page: Any,
    bbox: tuple[float, float, float, float],
) -> bytes:
    cropped = page.crop(bbox, strict=True)
    rendered = cropped.to_image(resolution=144, antialias=True)
    image = rendered.original.convert("RGB")
    output = BytesIO()
    image.save(output, format="JPEG", quality=88, optimize=True, progressive=True)
    content = output.getvalue()
    if len(content) <= _MAX_EXPORTED_IMAGE_BYTES:
        return content
    output = BytesIO()
    image.save(output, format="JPEG", quality=74, optimize=True, progressive=True)
    return output.getvalue()


def _has_table_candidate(page: Any) -> bool:
    if len(page.lines) + len(page.rects) < 4:
        return False
    try:
        tables = page.find_tables()
    except (PdfminerException, TypeError, ValueError):
        return False
    for table in tables:
        data = table.extract() or []
        row_count = len(data)
        column_count = max((len(row) for row in data), default=0)
        populated_cells = sum(
            bool(str(cell).strip()) for row in data for cell in row if cell is not None
        )
        if row_count >= 2 and column_count >= 2 and populated_cells >= 4:
            return True
    return False


def _deduplicated_page(page: Any) -> Any:
    characters = list(page.chars)
    retained = _deduplicate_characters(characters)
    filtered_page = page.filter(lambda _item: True)
    filtered_page._objects = {kind: list(items) for kind, items in page.objects.items()}
    filtered_page._objects["char"] = retained
    return filtered_page


def _deduplicate_characters(
    characters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: defaultdict[tuple[object, ...], list[dict[str, Any]]] = defaultdict(list)
    for character in characters:
        groups[
            (
                character.get("upright"),
                character.get("text"),
                character.get("fontname"),
                round(float(character.get("size", 0)), 2),
            )
        ].append(character)

    retained: list[dict[str, Any]] = []
    for group in groups.values():
        clusters: list[list[dict[str, Any]]] = []
        spatial_index: defaultdict[tuple[int, int], set[int]] = defaultdict(set)
        unindexed_clusters: set[int] = set()
        for character in sorted(
            group,
            key=lambda item: (
                float(item.get("doctop", item["top"])),
                float(item["x0"]),
            ),
        ):
            cells = _character_grid_cells(character)
            candidate_indices = (
                set(range(len(clusters)))
                if cells is None
                else {
                    cluster_index for cell in cells for cluster_index in spatial_index.get(cell, ())
                }
                | unindexed_clusters
            )
            matching_cluster_index = next(
                (
                    cluster_index
                    for cluster_index in sorted(candidate_indices)
                    if any(
                        _character_overlap(character, member) >= _DUPLICATE_OVERLAP_RATIO
                        for member in clusters[cluster_index]
                    )
                ),
                None,
            )
            if matching_cluster_index is None:
                clusters.append([character])
                cluster_index = len(clusters) - 1
            else:
                cluster_index = matching_cluster_index
                clusters[cluster_index].append(character)
            if cells is None:
                unindexed_clusters.add(cluster_index)
            else:
                for cell in cells:
                    spatial_index[cell].add(cluster_index)

        for cluster in clusters:
            median_x0 = median(float(character["x0"]) for character in cluster)
            median_top = median(float(character["top"]) for character in cluster)
            retained.append(
                min(
                    cluster,
                    key=lambda character: (
                        abs(float(character["x0"]) - median_x0)
                        + abs(float(character["top"]) - median_top)
                    ),
                )
            )

    source_order = {id(character): index for index, character in enumerate(characters)}
    return sorted(retained, key=lambda character: source_order[id(character)])


def _character_grid_cells(character: dict[str, Any]) -> tuple[tuple[int, int], ...] | None:
    try:
        x0 = float(character["x0"])
        x1 = float(character["x1"])
        top = float(character["top"])
        bottom = float(character["bottom"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (x0, x1, top, bottom)):
        return None
    left = math.floor(min(x0, x1) / _DEDUPLICATION_GRID_SIZE)
    right = math.floor(max(x0, x1) / _DEDUPLICATION_GRID_SIZE)
    first_row = math.floor(min(top, bottom) / _DEDUPLICATION_GRID_SIZE)
    last_row = math.floor(max(top, bottom) / _DEDUPLICATION_GRID_SIZE)
    cell_count = (right - left + 1) * (last_row - first_row + 1)
    if cell_count > _MAX_CHARACTER_GRID_CELLS:
        return None
    return tuple(
        (column, row) for column in range(left, right + 1) for row in range(first_row, last_row + 1)
    )


def _character_overlap(first: dict[str, Any], second: dict[str, Any]) -> float:
    intersection_width = max(
        0.0,
        min(float(first["x1"]), float(second["x1"])) - max(float(first["x0"]), float(second["x0"])),
    )
    intersection_height = max(
        0.0,
        min(float(first["bottom"]), float(second["bottom"]))
        - max(float(first["top"]), float(second["top"])),
    )
    intersection = intersection_width * intersection_height
    first_area = max(
        (float(first["x1"]) - float(first["x0"])) * (float(first["bottom"]) - float(first["top"])),
        0.01,
    )
    second_area = max(
        (float(second["x1"]) - float(second["x0"]))
        * (float(second["bottom"]) - float(second["top"])),
        0.01,
    )
    return intersection / min(first_area, second_area)


def _extract_links(
    annotations: list[dict[str, Any]],
    page_ids: dict[int, int],
) -> tuple[_PdfLink, ...]:
    links: list[_PdfLink] = []
    for annotation in annotations:
        target = _annotation_target(annotation, page_ids)
        if target is None:
            continue
        try:
            links.append(
                _PdfLink(
                    target=target,
                    x0=float(annotation["x0"]),
                    x1=float(annotation["x1"]),
                    top=float(annotation["top"]),
                    bottom=float(annotation["bottom"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(links)


def _annotation_target(
    annotation: dict[str, Any],
    page_ids: dict[int, int],
) -> str | None:
    if uri := _decode_uri(annotation.get("uri")):
        return uri

    data = annotation.get("data")
    if not isinstance(data, dict):
        return None
    destination = data.get("Dest")
    action = data.get("A")
    if isinstance(action, dict):
        if uri := _decode_uri(action.get("URI")):
            return uri
        if destination is None:
            destination = action.get("D")
    if not isinstance(destination, (list, tuple)) or not destination:
        return None

    object_id = getattr(destination[0], "objid", None)
    target_page = page_ids.get(object_id) if isinstance(object_id, int) else None
    return f"#page-{target_page}" if target_page is not None else None


def _decode_uri(value: object) -> str | None:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip() or None
    if isinstance(value, str):
        return value.strip() or None
    return None


def _build_line(
    page_number: int,
    page_width: float,
    page_height: float,
    raw_line: dict[str, Any],
    page_links: tuple[_PdfLink, ...],
) -> _PdfLine | None:
    raw_text = str(raw_line.get("text", "")).strip()
    soft_hyphen_end = raw_text.endswith("\u00ad")
    hard_hyphen_end = raw_text.endswith("-")
    text = _normalize_text(raw_text)
    if not text:
        return None

    raw_chars = tuple(raw_line.get("chars") or ())
    chars = tuple(
        compact for raw_char in raw_chars if (compact := _compact_character(raw_char)) is not None
    )
    sizes = [char.size for char in chars if char.size]
    font_size = median(sizes) if sizes else float(raw_line["bottom"] - raw_line["top"])
    if _looks_artificially_spaced(text):
        text = (
            _reconstruct_character_text(
                chars,
                font_size,
                collapse_tracking=True,
            )
            or text
        )
    text = _repair_artificial_spacing_runs(text)
    weighted_characters = sum(max(len(str(char.get("text", ""))), 1) for char in raw_chars)
    bold_characters = sum(
        max(len(str(char.get("text", ""))), 1)
        for char in raw_chars
        if _is_bold_font(str(char.get("fontname", "")))
    )
    rotated_characters = sum(
        max(len(str(char.get("text", ""))), 1) for char in raw_chars if char.get("upright") is False
    )
    bold = weighted_characters > 0 and bold_characters / weighted_characters >= 0.55
    rotated = weighted_characters > 0 and rotated_characters / weighted_characters >= 0.55
    top = float(raw_line["top"])
    bottom = float(raw_line["bottom"])
    x0 = float(raw_line["x0"])
    x1 = float(raw_line["x1"])
    links = tuple(
        link
        for link in page_links
        if _overlaps(x0, x1, top, bottom, link.x0, link.x1, link.top, link.bottom)
    )
    return _PdfLine(
        page_number=page_number,
        page_width=float(page_width),
        page_height=float(page_height),
        text=text,
        chars=chars,
        x0=x0,
        x1=x1,
        top=top,
        bottom=bottom,
        font_size=font_size,
        bold=bold,
        links=links,
        soft_hyphen_end=soft_hyphen_end,
        hard_hyphen_end=hard_hyphen_end,
        rotated=rotated,
    )


def _looks_artificially_spaced(text: str) -> bool:
    tokens = text.split()
    singleton_run = 0
    longest_run = 0
    singleton_letters = 0
    letter_tokens = 0
    for token in tokens:
        letters = "".join(character for character in token if character.isalpha())
        if letters:
            letter_tokens += 1
        if len(token) == 1 and token.isalpha():
            singleton_run += 1
            singleton_letters += 1
            longest_run = max(longest_run, singleton_run)
        else:
            singleton_run = 0
    return (
        longest_run >= 4
        and singleton_letters >= 4
        and singleton_letters / max(letter_tokens, 1) >= 0.5
    )


def _repair_artificial_spacing_runs(text: str) -> str:
    return _SPACED_WORD_PATTERN.sub(
        lambda match: re.sub(r"\s+", "", match.group()),
        text,
    )


def _reconstruct_character_text(
    characters: tuple[_PdfCharacter, ...],
    fallback_size: float,
    *,
    collapse_tracking: bool,
) -> str:
    ordered = sorted(
        (character for character in characters if character.text),
        key=lambda character: (character.x0, character.top),
    )
    visible = [character for character in ordered if not character.text.isspace()]
    if not visible:
        return ""

    positive_gap_ratios: list[float] = []
    for left_character, right_character in zip(visible, visible[1:], strict=False):
        size = (
            (left_character.size or fallback_size) + (right_character.size or fallback_size)
        ) / 2
        gap = max(0.0, right_character.x0 - left_character.x1)
        if gap > 0:
            positive_gap_ratios.append(gap / max(size, 0.01))
    tracking_ratio = median(positive_gap_ratios) if positive_gap_ratios else 0.0
    word_gap_ratio = max(0.18, min(0.5, tracking_ratio * 1.6)) if collapse_tracking else 0.18

    output: list[str] = []
    previous: _PdfCharacter | None = None
    pending_explicit_space = False
    visible_ids = {id(character) for character in visible}
    for character in ordered:
        if character.text.isspace():
            pending_explicit_space = True
            continue
        if id(character) not in visible_ids:
            continue
        if previous is not None:
            size = ((previous.size or fallback_size) + (character.size or fallback_size)) / 2
            gap_ratio = max(0.0, character.x0 - previous.x1) / max(size, 0.01)
            add_space = gap_ratio > word_gap_ratio or (
                pending_explicit_space and not collapse_tracking
            )
            if add_space and _space_is_allowed(output, character.text):
                output.append(" ")
        output.append(character.text)
        previous = character
        pending_explicit_space = False
    return _normalize_text("".join(output))


def _space_is_allowed(output: list[str], following: str) -> bool:
    if not output or not following:
        return False
    return not output[-1].endswith(tuple("([{¿¡")) and not following.startswith(tuple(".,;:!?)]}…"))


def _split_wide_line(line: _PdfLine) -> tuple[_PdfLine, ...]:
    visible = sorted(
        (character for character in line.chars if character.text and not character.text.isspace()),
        key=lambda character: character.x0,
    )
    if len(visible) < 4:
        return (line,)
    split_threshold = max(line.page_width * 0.1, line.font_size * 5)
    boundaries = [
        (previous.x1 + current.x0) / 2
        for previous, current in zip(visible, visible[1:], strict=False)
        if current.x0 - previous.x1 >= split_threshold
    ]
    if not boundaries:
        return (line,)

    groups: list[list[_PdfCharacter]] = [[] for _ in range(len(boundaries) + 1)]
    for character in line.chars:
        center = (character.x0 + character.x1) / 2
        group_index = sum(center > boundary for boundary in boundaries)
        groups[group_index].append(character)
    if any(sum(character.text.isalpha() for character in group) < 2 for group in groups):
        return (line,)

    parts: list[_PdfLine] = []
    for group in groups:
        compact = tuple(group)
        raw_group_text = "".join(character.text for character in compact).rstrip()
        x0 = min(character.x0 for character in compact)
        x1 = max(character.x1 for character in compact)
        top = min(character.top for character in compact)
        bottom = max(character.bottom for character in compact)
        text = _repair_artificial_spacing_runs(
            _reconstruct_character_text(
                compact,
                line.font_size,
                collapse_tracking=False,
            )
        )
        if not text:
            continue
        parts.append(
            replace(
                line,
                text=text,
                chars=compact,
                x0=x0,
                x1=x1,
                top=top,
                bottom=bottom,
                links=tuple(
                    link
                    for link in line.links
                    if _overlaps(x0, x1, top, bottom, link.x0, link.x1, link.top, link.bottom)
                ),
                soft_hyphen_end=raw_group_text.endswith("\u00ad"),
                hard_hyphen_end=text.endswith("-"),
            )
        )
    return tuple(parts) if len(parts) >= 2 else (line,)


def _reading_order_lines(lines: list[_PdfLine]) -> list[_PdfLine]:
    """Keep full-width separators in place and read strong columns contiguously."""

    if len(lines) < _MIN_COLUMN_LINES * 2:
        return lines
    page_width = max((line.page_width for line in lines), default=0)
    if page_width <= 0:
        return lines
    ordered = sorted(lines, key=lambda line: (line.top, line.x0, line.bottom))
    result: list[_PdfLine] = []
    band: list[_PdfLine] = []
    for line in ordered:
        width = max(0.0, line.x1 - line.x0)
        separator = width >= page_width * 0.58 or (line.centered and width >= page_width * 0.25)
        if separator:
            result.extend(_order_column_band(band, page_width))
            band.clear()
            result.append(line)
        else:
            band.append(line)
    result.extend(_order_column_band(band, page_width))
    return result


def _order_column_band(lines: list[_PdfLine], page_width: float) -> list[_PdfLine]:
    if len(lines) < _MIN_COLUMN_LINES * 2:
        return sorted(lines, key=lambda line: (line.top, line.x0, line.bottom))
    left = [line for line in lines if (line.x0 + line.x1) / 2 <= page_width * 0.47]
    right = [line for line in lines if (line.x0 + line.x1) / 2 >= page_width * 0.53]
    if len(left) < _MIN_COLUMN_LINES or len(right) < _MIN_COLUMN_LINES:
        return sorted(lines, key=lambda line: (line.top, line.x0, line.bottom))
    if set(left).intersection(right):
        return sorted(lines, key=lambda line: (line.top, line.x0, line.bottom))
    left_top, left_bottom = min(line.top for line in left), max(line.bottom for line in left)
    right_top, right_bottom = min(line.top for line in right), max(line.bottom for line in right)
    if min(left_bottom, right_bottom) <= max(left_top, right_top):
        return sorted(lines, key=lambda line: (line.top, line.x0, line.bottom))
    unassigned = [line for line in lines if line not in left and line not in right]
    if unassigned:
        return sorted(lines, key=lambda line: (line.top, line.x0, line.bottom))
    return [
        *sorted(left, key=lambda line: (line.top, line.x0, line.bottom)),
        *sorted(right, key=lambda line: (line.top, line.x0, line.bottom)),
    ]


def _compact_character(raw: dict[str, Any]) -> _PdfCharacter | None:
    try:
        return _PdfCharacter(
            text=str(raw.get("text", "")),
            x0=float(raw["x0"]),
            x1=float(raw["x1"]),
            top=float(raw["top"]),
            bottom=float(raw["bottom"]),
            size=float(raw.get("size", 0)),
            upright=raw.get("upright") is not False,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _is_bold_font(font_name: str) -> bool:
    normalized = font_name.casefold()
    return any(marker in normalized for marker in ("bold", "black", "demi", "semibold"))


def _dominant_body_size(lines: list[_PdfLine]) -> float:
    weights: Counter[float] = Counter()
    for line in lines:
        if _LETTER_PATTERN.search(line.text) and not line.rotated:
            weights[round(line.font_size, 1)] += min(len(line.text), 100)
    if not weights:
        return 10.0
    return weights.most_common(1)[0][0]


def _heading_size_levels(lines: list[_PdfLine], body_size: float) -> dict[float, int]:
    sizes = sorted(
        {
            round(line.font_size, 1)
            for line in lines
            if not line.rotated
            and line.font_size >= body_size * 1.18
            and len(line.text) <= _MAX_HEADING_LENGTH
            and _LETTER_PATTERN.search(line.text)
        },
        reverse=True,
    )
    return {size: min(index, 4) for index, size in enumerate(sizes[:4], start=1)}


def _repeated_margin_lines(lines: list[_PdfLine], page_count: int) -> set[str]:
    occurrences: Counter[str] = Counter()
    for line in lines:
        if line.top <= line.page_height * 0.1 or line.bottom >= line.page_height * 0.84:
            occurrences[_margin_key(line.text)] += 1
    threshold = max(3, round(page_count * 0.3))
    return {text for text, count in occurrences.items() if text and count >= threshold}


def _pages_requiring_ocr(pages: list[_PdfPage]) -> set[int]:
    selected: set[int] = set()
    for page in pages:
        has_discrete_image = any(
            _MIN_OCR_IMAGE_AREA_RATIO <= ratio < _FULL_PAGE_IMAGE_AREA_RATIO
            for ratio in page.image_area_ratios
        )
        has_full_page_image = any(
            ratio >= _FULL_PAGE_IMAGE_AREA_RATIO for ratio in page.image_area_ratios
        )
        lacks_text = _page_letter_count(page) < _GRAPHIC_WARNING_LETTER_LIMIT
        if (
            page.has_table
            or has_discrete_image
            or (has_full_page_image and lacks_text)
            or _has_suspicious_glyph_encoding(page)
            or _native_page_quality(page) < _LOW_NATIVE_QUALITY_THRESHOLD
        ):
            selected.add(page.number)
    return selected


def _page_letter_count(page: _PdfPage) -> int:
    return sum(_heading_letter_count(line.text) for line in page.lines if not line.rotated)


def _referenced_pages(lines: list[_PdfLine]) -> set[int]:
    referenced: set[int] = set()
    for line in lines:
        for link in line.links:
            match = re.fullmatch(r"#page-(\d+)", link.target)
            if match:
                referenced.add(int(match.group(1)))
    return referenced


def _render_document(
    pages: list[_PdfPage],
    body_size: float,
    heading_sizes: dict[float, int],
    repeated_margins: set[str],
    referenced_pages: set[int],
    ocr_pages: dict[int, str],
    ocr_failed_pages: set[int],
    page_images: dict[int, tuple[PdfEmbeddedResource, ...]] | None = None,
    on_progress: PdfProgressCallback | None = None,
) -> tuple[str, tuple[PdfReviewIssue, ...]]:
    blocks: list[_MarkdownBlock] = []
    previous_body_line: _PdfLine | None = None

    total_pages = len(pages)
    for current, page in enumerate(pages, start=1):
        blocks.append(
            _MarkdownBlock(
                kind="provenance",
                text=_pdf_page_marker(page.number),
                page_number=page.number,
            )
        )
        ocr_markdown = ocr_pages.get(page.number)
        if ocr_markdown is not None:
            ocr_markdown = _strip_native_margin_numbers_from_ocr(
                page,
                ocr_markdown,
                body_size,
            )
        if ocr_markdown is not None and _should_replace_with_ocr(page, ocr_markdown):
            if page.number in referenced_pages:
                blocks.append(
                    _MarkdownBlock(
                        kind="raw",
                        text=f'<a id="page-{page.number}"></a>',
                        page_number=page.number,
                    )
                )
            blocks.append(
                _MarkdownBlock(
                    kind="raw",
                    text=_restore_page_links(ocr_markdown, page),
                    page_number=page.number,
                )
            )
            if (
                _page_letter_count(page) < _GRAPHIC_WARNING_LETTER_LIMIT
                and _heading_letter_count(ocr_markdown) < 100
            ):
                blocks.append(
                    _MarkdownBlock(
                        kind="warning",
                        text=(
                            f"> **Aviso OCR (página {page.number}):** el texto procede de una "
                            "página muy gráfica o decorativa; compáralo con el original."
                        ),
                        page_number=page.number,
                    )
                )
            _append_page_images(blocks, page.number, page_images)
            previous_body_line = None
            if on_progress is not None:
                on_progress(PdfProgressPhase.STRUCTURING, current, total_pages)
            continue

        visible_lines: list[_PdfLine] = []
        previous_margin_candidate: _PdfLine | None = None
        skipped_rotated = False
        for line in page.lines:
            if line.rotated:
                skipped_rotated = True
                continue
            if _omit_margin_line(
                line,
                repeated_margins,
                previous_margin_candidate,
                body_size,
            ):
                continue
            visible_lines.append(line)
            previous_margin_candidate = line

        visible_lines = _merge_drop_caps(visible_lines, body_size)
        visible_lines, skipped_vertical = _remove_vertical_stacks(visible_lines, body_size)
        visible_lines, skipped_noise = _remove_decorative_noise(visible_lines, body_size)
        skipped_rotated = skipped_rotated or skipped_vertical or skipped_noise
        if page.number in referenced_pages:
            blocks.append(
                _MarkdownBlock(
                    kind="raw",
                    text=f'<a id="page-{page.number}"></a>',
                    page_number=page.number,
                )
            )
            previous_body_line = None

        toc_page = _is_toc_page(visible_lines)
        previous_in_page: _PdfLine | None = None
        for line in visible_lines:
            gap_before = (
                line.top - previous_in_page.bottom
                if previous_in_page is not None
                else body_size * 2
            )
            level = _heading_level(line, body_size, heading_sizes, gap_before, toc_page)
            if level is not None:
                _append_heading(blocks, line, level, gap_before)
                previous_body_line = None
            elif _BULLET_PATTERN.match(line.text):
                item_text = _BULLET_PATTERN.sub("", _apply_links(line), count=1).strip()
                blocks.append(
                    _MarkdownBlock(
                        kind="list",
                        text=f"- {item_text}",
                        page_number=page.number,
                        source_line=line,
                    )
                )
                previous_body_line = None
            elif toc_page:
                rendered = _apply_links(line)
                if line.bold and _heading_letter_count(line.text) >= 4:
                    rendered = f"**{rendered}**"
                blocks.append(
                    _MarkdownBlock(
                        kind="toc",
                        text=rendered,
                        page_number=page.number,
                        source_line=line,
                    )
                )
                previous_body_line = None
            else:
                rendered = _apply_links(line)
                if line.bold and _heading_letter_count(line.text) >= 4:
                    rendered = f"**{rendered}**"
                if (
                    blocks
                    and blocks[-1].kind == "paragraph"
                    and previous_body_line is not None
                    and _should_join_lines(
                        previous_body_line,
                        line,
                        body_size,
                        gap_before,
                    )
                ):
                    blocks[-1].text = _join_line_text(
                        blocks[-1].text,
                        rendered,
                        previous_body_line,
                        line,
                    )
                    blocks[-1].source_line = line
                else:
                    blocks.append(
                        _MarkdownBlock(
                            kind="paragraph",
                            text=rendered,
                            page_number=page.number,
                            source_line=line,
                        )
                    )
                previous_body_line = line
            previous_in_page = line

        if _append_unplaced_page_links(blocks, visible_lines, page.number):
            previous_body_line = None
        warning = _page_conversion_warning(page, visible_lines, skipped_rotated, ocr_markdown)
        if warning is not None:
            blocks.append(_MarkdownBlock(kind="warning", text=warning, page_number=page.number))
            previous_body_line = None
        if page.number in ocr_failed_pages:
            blocks.append(
                _MarkdownBlock(
                    kind="warning",
                    text=(
                        f"> **Aviso OCR (página {page.number}):** no se pudieron analizar los "
                        "elementos rasterizados de esta página; el texto seleccionable sí se ha "
                        "conservado."
                    ),
                    page_number=page.number,
                )
            )
            previous_body_line = None
        if ocr_markdown is not None:
            additions = _ocr_additions(page, ocr_markdown)
            if additions:
                blocks.append(
                    _MarkdownBlock(
                        kind="raw",
                        text=additions,
                        page_number=page.number,
                    )
                )
                previous_body_line = None

        _append_page_images(blocks, page.number, page_images)

        if on_progress is not None:
            on_progress(PdfProgressPhase.STRUCTURING, current, total_pages)

    normalized_blocks = _join_hyphenated_block_continuations(blocks)
    return (
        _normalized_blocks_to_markdown(normalized_blocks),
        _review_issues(normalized_blocks),
    )


def _append_page_images(
    blocks: list[_MarkdownBlock],
    page_number: int,
    page_images: dict[int, tuple[PdfEmbeddedResource, ...]] | None,
) -> None:
    if not page_images:
        return
    for resource in page_images.get(page_number, ()):
        blocks.append(
            _MarkdownBlock(
                kind="raw",
                text=(f"![](<{RESOURCE_REFERENCE_PREFIX}{resource.relative_path.as_posix()}>)"),
                page_number=page_number,
            )
        )


def _should_replace_with_ocr(page: _PdfPage, ocr_markdown: str) -> bool:
    native_letters = _page_letter_count(page)
    ocr_letters = _heading_letter_count(ocr_markdown)
    native_quality = _native_page_quality(page)
    ocr_quality = _text_quality_score(ocr_markdown)
    if page.has_table and _MARKDOWN_TABLE_PATTERN.search(ocr_markdown):
        return ocr_quality >= max(0.42, native_quality - 0.08)
    if native_letters < _MIN_USABLE_NATIVE_LETTERS:
        return ocr_letters >= 10 and ocr_quality >= 0.42
    if _has_suspicious_glyph_encoding(page):
        return (
            ocr_letters >= native_letters * 0.65
            and ocr_quality >= max(_MIN_OCR_REPLACEMENT_QUALITY, native_quality + 0.08)
            and _suspicious_glyph_count(ocr_markdown)
            < _suspicious_glyph_count(_native_page_text(page))
        )
    if native_quality < _LOW_NATIVE_QUALITY_THRESHOLD:
        return ocr_letters >= max(10, native_letters * 0.55) and ocr_quality >= max(
            _MIN_OCR_REPLACEMENT_QUALITY, native_quality + 0.12
        )
    return False


def _has_suspicious_glyph_encoding(page: _PdfPage) -> bool:
    text = " ".join(line.text for line in page.lines if not line.rotated)
    return _suspicious_glyph_count(text) >= 3


def _suspicious_glyph_count(text: str) -> int:
    return len(re.findall(r"(?<=[^\W\d_])[$\"]|[$\"](?=[^\W\d_])", text))


def _native_page_text(page: _PdfPage) -> str:
    return "\n".join(line.text for line in page.lines if not line.rotated)


def _native_page_quality(page: _PdfPage) -> float:
    score = _text_quality_score(_native_page_text(page))
    readable_lines = [
        line for line in page.lines if not line.rotated and _LETTER_PATTERN.search(line.text)
    ]
    if readable_lines:
        fragmented = sum(_heading_letter_count(line.text) <= 2 for line in readable_lines) / len(
            readable_lines
        )
        score -= min(0.30, fragmented * 0.35)
    if page.lines:
        rotated_ratio = sum(line.rotated for line in page.lines) / len(page.lines)
        score -= min(0.25, rotated_ratio * 0.4)
    return max(0.0, min(1.0, score))


def _text_quality_score(text: str) -> float:
    visible = re.sub(r"<!--[\s\S]*?-->|[`#*_|>\[\]()]", " ", text)
    compact = "".join(character for character in visible if not character.isspace())
    letters = sum(character.isalpha() for character in compact)
    if not compact or letters == 0:
        return 0.0
    words = re.findall(r"[^\W\d_]+", visible, re.UNICODE)
    short_ratio = sum(len(word) == 1 for word in words) / len(words) if words else 1.0
    suspicious = (
        visible.count("\ufffd")
        + visible.count("$")
        + _suspicious_glyph_count(visible)
        + len(_SPACED_WORD_PATTERN.findall(visible))
    )
    letter_ratio = letters / len(compact)
    length_confidence = min(1.0, letters / 80)
    score = (
        0.50 * min(1.0, letter_ratio / 0.75)
        + 0.20 * (1.0 - min(1.0, short_ratio * 2.0))
        + 0.15 * length_confidence
        + 0.15 * (1.0 - min(1.0, suspicious / 4))
    )
    return max(0.0, min(1.0, score))


def _strip_native_margin_numbers_from_ocr(
    page: _PdfPage,
    markdown: str,
    body_size: float,
) -> str:
    running_headers = {
        _normalized_margin_text(line.text)
        for line in page.lines
        if _is_top_numbered_running_header(line, body_size)
    }
    margin_numbers = {
        re.sub(r"\s+", "", line.text).casefold()
        for line in page.lines
        if (
            line.top <= line.page_height * 0.1
            or _is_top_outer_folio_line(line)
            or line.bottom >= line.page_height * 0.84
        )
        and _is_page_number(re.sub(r"\s+", "", line.text))
    }
    if not margin_numbers and not running_headers:
        return markdown

    lines = markdown.splitlines()
    nonempty_indices = [index for index, line in enumerate(lines) if line.strip()]
    candidate_indices = set(nonempty_indices[:6]) | set(nonempty_indices[-6:])
    for index in candidate_indices:
        stripped = lines[index].strip()
        if _normalized_margin_text(stripped) in running_headers:
            lines[index] = ""
            continue
        for number in margin_numbers:
            if stripped.casefold() == number:
                lines[index] = ""
                break
            match = re.match(
                rf"^(?P<number>{re.escape(number)})\s+(?P<content>.+)$",
                stripped,
                re.IGNORECASE,
            )
            if match is not None and _LETTER_PATTERN.search(match.group("content")):
                lines[index] = match.group("content")
                break

    compacted: list[str] = []
    for line in lines:
        if not line.strip() and (not compacted or not compacted[-1].strip()):
            continue
        compacted.append(line)
    return "\n".join(compacted).strip()


def _ocr_additions(page: _PdfPage, ocr_markdown: str) -> str:
    native_tokens = _comparison_tokens(" ".join(line.text for line in page.lines))
    additions: list[str] = []
    seen_additions: set[tuple[str, ...]] = set()
    for block in re.split(r"\n\s*\n", ocr_markdown):
        stripped = block.strip()
        if not stripped:
            continue
        block_tokens = _comparison_tokens(stripped)
        alpha_tokens = {token for token in block_tokens if any(char.isalpha() for char in token)}
        is_table = bool(_MARKDOWN_TABLE_PATTERN.search(stripped))
        overlap = block_tokens & native_tokens
        if block_tokens and (
            block_tokens.issubset(native_tokens)
            or (len(block_tokens) >= 4 and len(overlap) / len(block_tokens) >= 0.9)
        ):
            continue
        if not is_table and len(alpha_tokens) < 2:
            continue
        if not is_table and _text_quality_score(stripped) < 0.45:
            continue
        addition_key = tuple(sorted(block_tokens))
        if addition_key in seen_additions:
            continue
        seen_additions.add(addition_key)
        additions.append(stripped)
    return "\n\n".join(additions)


def _comparison_tokens(text: str) -> set[str]:
    without_targets = re.sub(r"\]\((?:<[^>]+>|[^)]+)\)", "]", text)
    without_targets = _normalize_text(without_targets)
    return {token.casefold() for token in re.findall(r"[^\W_]+", without_targets, flags=re.UNICODE)}


def _restore_page_links(markdown: str, page: _PdfPage) -> str:
    restored = markdown
    missing_links: list[tuple[str, str]] = []
    seen_targets: set[str] = set()
    for line in page.lines:
        for link in line.links:
            if link.target in seen_targets or link.target in restored:
                continue
            seen_targets.add(link.target)
            label = _linked_text(line.chars, link)
            replacement = f"[{_escape_link_label(label)}](<{_safe_target(link.target)}>)"
            if label and label in restored:
                restored = restored.replace(label, replacement, 1)
            else:
                missing_links.append((label or _descriptive_link_label(link.target), link.target))
    if missing_links:
        restored = (
            f"{restored.rstrip()}\n\n**Destinos conservados**\n\n"
            f"{_render_conserved_links(missing_links)}"
        )
    return restored


def _append_unplaced_page_links(
    blocks: list[_MarkdownBlock],
    lines: list[_PdfLine],
    page_number: int,
) -> bool:
    placed_targets = {
        link.target for line in lines for link in line.links if _link_has_visible_label(line, link)
    }
    missing: list[tuple[str, str]] = []
    seen_targets = set(placed_targets)
    for line in lines:
        for link in line.links:
            if link.target in seen_targets:
                continue
            seen_targets.add(link.target)
            missing.append((_descriptive_link_label(link.target), link.target))
    if not missing:
        return False
    blocks.append(
        _MarkdownBlock(
            kind="raw",
            text=f"**Destinos conservados**\n\n{_render_conserved_links(missing)}",
            page_number=page_number,
        )
    )
    return True


def _render_conserved_links(links: list[tuple[str, str]]) -> str:
    return "\n".join(
        f"- [{_escape_link_label(label)}](<{_safe_target(target)}>)" for label, target in links
    )


def _descriptive_link_label(target: str) -> str:
    page = re.fullmatch(r"#page-(\d+)", target, re.IGNORECASE)
    if page:
        return f"Página {page.group(1)}"
    try:
        parsed = urlsplit(target)
    except ValueError:
        cleaned = " ".join(unquote(target).split()).strip()
        return f"Destino: {cleaned[:100]}" if cleaned else "Destino conservado"
    if parsed.scheme.casefold() == "mailto":
        address = " ".join(unquote(parsed.path).split())
        return f"Correo: {address[:100]}" if address else "Abrir correo"
    host = " ".join(unquote(parsed.netloc).split()).strip()
    if host:
        return f"Abrir {host[:100]}"
    filename = " ".join(unquote(PurePosixPath(parsed.path).name).split()).strip()
    if filename:
        return f"Abrir {filename[:100]}"
    fragment = " ".join(unquote(parsed.fragment).replace("-", " ").split()).strip()
    if fragment:
        return f"Destino: {fragment[:100]}"
    cleaned = " ".join(unquote(target).split()).strip()
    return f"Destino: {cleaned[:100]}" if cleaned else "Destino conservado"


def _merge_drop_caps(lines: list[_PdfLine], body_size: float) -> list[_PdfLine]:
    merged: list[_PdfLine] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if index + 1 < len(lines) and _is_drop_cap(line, lines[index + 1], body_size):
            following = lines[index + 1]
            merged.append(
                replace(
                    following,
                    text=f"{line.text}{following.text}",
                    chars=line.chars + following.chars,
                    x0=min(line.x0, following.x0),
                    top=min(line.top, following.top),
                    bottom=max(line.bottom, following.bottom),
                    links=tuple(dict.fromkeys(line.links + following.links)),
                )
            )
            index += 2
            continue
        merged.append(line)
        index += 1
    return merged


def _remove_vertical_stacks(
    lines: list[_PdfLine],
    body_size: float,
) -> tuple[list[_PdfLine], bool]:
    retained: list[_PdfLine] = []
    skipped = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if len(line.text) > 2:
            retained.append(line)
            index += 1
            continue

        run = [line]
        next_index = index + 1
        while next_index < len(lines):
            candidate = lines[next_index]
            same_column = abs(candidate.x0 - line.x0) <= max(body_size * 0.4, 3)
            close = candidate.top - run[-1].bottom <= body_size * 1.5
            if len(candidate.text) > 2 or not same_column or not close:
                break
            run.append(candidate)
            next_index += 1
        if len(run) >= 4:
            skipped = True
        else:
            retained.extend(run)
        index = next_index
    return retained, skipped


def _remove_decorative_noise(
    lines: list[_PdfLine],
    body_size: float,
) -> tuple[list[_PdfLine], bool]:
    candidates = [
        (index, line)
        for index, line in enumerate(lines)
        if len(line.text) <= 16 and re.search(r"[<>*^«»]", line.text)
    ]
    clusters: list[list[tuple[int, _PdfLine]]] = []
    for candidate in candidates:
        if not clusters or candidate[1].top - clusters[-1][-1][1].bottom > body_size * 3:
            clusters.append([candidate])
        else:
            clusters[-1].append(candidate)

    noisy_ranges = [
        (
            cluster[0][1].top - body_size,
            cluster[-1][1].bottom + body_size * 2.5,
        )
        for cluster in clusters
        if len(cluster) >= 2
    ]
    if not noisy_ranges:
        return lines, False

    retained = [
        line
        for line in lines
        if not (
            len(line.text) <= 16
            and not line.text.isdecimal()
            and any(start <= line.top and line.bottom <= end for start, end in noisy_ranges)
        )
    ]
    return retained, len(retained) != len(lines)


def _is_drop_cap(first: _PdfLine, second: _PdfLine, body_size: float) -> bool:
    return (
        len(first.text) == 1
        and first.text.isalpha()
        and first.text.isupper()
        and first.font_size >= body_size * 1.7
        and bool(second.text)
        and second.text[0].islower()
        and abs(first.top - second.top) <= body_size * 1.5
        and first.x0 < second.x0
    )


def _append_heading(
    blocks: list[_MarkdownBlock],
    line: _PdfLine,
    level: int,
    gap_before: float,
) -> None:
    text = _display_heading_text(line) if not line.links else _apply_links(line)
    if (
        blocks
        and blocks[-1].kind == "heading"
        and blocks[-1].level == level
        and blocks[-1].page_number == line.page_number
        and blocks[-1].source_line is not None
        and blocks[-1].source_line.centered
        and line.centered
        and gap_before <= max(blocks[-1].source_line.font_size, line.font_size) * 1.1
    ):
        blocks[-1].text = f"{blocks[-1].text} {text}"
        blocks[-1].source_line = line
        return
    blocks.append(
        _MarkdownBlock(
            kind="heading",
            text=text,
            page_number=line.page_number,
            level=level,
            source_line=line,
        )
    )


def _display_heading_text(line: _PdfLine) -> str:
    if any(character.islower() for character in line.text):
        return line.text
    characters = [character for character in line.chars if character.upright and character.text]
    if len(characters) < 2:
        return line.text

    ordered = sorted(characters, key=lambda character: character.x0)
    gap_ratios: list[float] = []
    for previous_character, current in zip(ordered, ordered[1:], strict=False):
        average_size = (
            (current.size or line.font_size) + (previous_character.size or line.font_size)
        ) / 2
        gap = max(0.0, current.x0 - previous_character.x1)
        gap_ratios.append(gap / max(average_size, 0.01))
    word_gap_ratio = max(0.15, median(gap_ratios) * 2.2 if gap_ratios else 0.15)

    output: list[str] = []
    previous: _PdfCharacter | None = None
    for character in ordered:
        text = character.text
        if not text:
            continue
        if previous is not None:
            gap = character.x0 - previous.x1
            average_size = (
                (character.size or line.font_size) + (previous.size or line.font_size)
            ) / 2
            if (
                gap / max(average_size, 0.01) > word_gap_ratio
                and output
                and not output[-1].endswith(" ")
            ):
                output.append(" ")
        if text.isspace():
            if output and not output[-1].endswith(" "):
                output.append(" ")
        else:
            output.append(text)
        previous = character
    return _normalize_text("".join(output)) or line.text


def _should_join_lines(
    previous: _PdfLine,
    current: _PdfLine,
    body_size: float,
    gap_before: float,
) -> bool:
    if previous.soft_hyphen_end:
        return True
    if previous.hard_hyphen_end and _hard_hyphen_wraps_word(previous.text, current.text):
        return True

    same_page = previous.page_number == current.page_number
    if not same_page:
        return False

    if gap_before > max(body_size * 1.15, previous.font_size * 0.95):
        return False
    if abs(previous.font_size - current.font_size) > body_size * 0.35:
        return False
    if current.x0 - previous.x0 > current.page_width * 0.08 and previous.text.rstrip().endswith(
        (".", "!", "?")
    ):
        return False
    return True


def _join_line_text(
    existing: str,
    following: str,
    previous_line: _PdfLine,
    current_line: _PdfLine,
) -> str:
    if previous_line.soft_hyphen_end:
        joined = f"{existing.rstrip()}{following.lstrip()}"
    elif (
        previous_line.hard_hyphen_end
        and _hard_hyphen_wraps_word(previous_line.text, current_line.text)
        and existing.rstrip().endswith("-")
    ):
        base = existing.rstrip()[:-1]
        continuation = following.lstrip()
        if continuation.startswith("**") and "**" in continuation[2:]:
            prefix, separator, fragment = base.rpartition(" ")
            joined = f"{prefix}{separator}**{fragment}{continuation[2:]}"
        else:
            joined = f"{base}{continuation}"
    else:
        joined = f"{existing.rstrip()} {following.lstrip()}"
    return _collapse_adjacent_links(joined)


def _hard_hyphen_wraps_word(previous: str, current: str) -> bool:
    if not current[:1].isalpha():
        return False
    if current[:1].islower():
        return True
    previous_word = previous.rstrip("-").rsplit(maxsplit=1)[-1]
    current_word = current.split(maxsplit=1)[0]
    return previous_word.isupper() and current_word.isupper()


def _collapse_adjacent_links(markdown: str) -> str:
    previous = ""
    collapsed = markdown
    while previous != collapsed:
        previous = collapsed
        collapsed = _ADJACENT_LINK_PATTERN.sub(r"[\1 \3](<\2>)", collapsed)
    return collapsed


def _heading_level(
    line: _PdfLine,
    body_size: float,
    heading_sizes: dict[float, int],
    gap_before: float,
    toc_page: bool,
) -> int | None:
    text = _display_heading_text(line).strip()
    letter_count = _heading_letter_count(text)
    if (
        len(text) > _MAX_HEADING_LENGTH
        or letter_count < 3
        or text.endswith((".", ";"))
        or text.startswith(("-", "–", "—", "―"))
    ):
        return None
    if _SECTION_HEADING_PATTERN.match(text):
        return 2
    if level := heading_sizes.get(round(line.font_size, 1)):
        return level
    if toc_page or letter_count < 4:
        return None

    uppercase = _is_uppercase_text(text)
    separated = gap_before >= max(body_size * 0.55, 3)
    if uppercase and separated and (line.bold or line.centered):
        return 2 if line.centered else 3
    if line.bold and separated and len(text) <= 80:
        return 3
    return None


def _omit_margin_line(
    line: _PdfLine,
    repeated_margins: set[str],
    previous: _PdfLine | None,
    body_size: float,
) -> bool:
    in_top_margin = line.top <= line.page_height * 0.1
    in_top_folio_band = _is_top_outer_folio_line(line)
    in_top_numbered_running_header = _is_top_numbered_running_header(line, body_size)
    in_bottom_margin = line.bottom >= line.page_height * 0.84
    if not (
        in_top_margin or in_top_folio_band or in_top_numbered_running_header or in_bottom_margin
    ):
        return False

    text = line.text.strip()
    if in_top_numbered_running_header:
        return True
    if _margin_key(text) in repeated_margins:
        return True
    page_number_key = re.sub(r"\s+", "", text)
    if (in_top_margin or in_top_folio_band or in_bottom_margin) and _is_page_number(
        page_number_key
    ):
        return True
    gap_before = line.top - previous.bottom if previous is not None else line.page_height
    return bool(
        in_bottom_margin
        and len(text) <= 48
        and gap_before >= body_size * 1.5
        and _RUNNING_FOOTER_PATTERN.fullmatch(text)
    )


def _is_top_outer_folio_line(line: _PdfLine) -> bool:
    """Recognize folios below a generous top inset but outside the main text column."""
    in_folio_band = line.top <= line.page_height * 0.18
    outside_text_column = line.x1 <= line.page_width * 0.35 or line.x0 >= line.page_width * 0.65
    return in_folio_band and outside_text_column


def _is_top_numbered_running_header(line: _PdfLine, body_size: float) -> bool:
    """Recognize a folio joined to a short label in a non-centered running head."""
    if line.top > line.page_height * 0.18 or line.centered:
        return False
    leading_parts = line.text.strip().split(maxsplit=1)
    if (
        len(leading_parts) == 2
        and _is_page_number(leading_parts[0])
        and _SECTION_HEADING_PATTERN.fullmatch(leading_parts[1])
    ):
        return True

    trailing_parts = line.text.strip().rsplit(maxsplit=1)
    label = trailing_parts[0] if len(trailing_parts) == 2 else ""
    return bool(
        len(trailing_parts) == 2
        and _is_page_number(trailing_parts[1])
        and label
        and len(label) <= 48
        and _is_uppercase_text(label)
        and line.x1 >= line.page_width * 0.65
        and line.font_size <= body_size * 0.9
    )


def _normalized_margin_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _page_conversion_warning(
    page: _PdfPage,
    visible_lines: list[_PdfLine],
    skipped_rotated: bool,
    ocr_markdown: str | None,
) -> str | None:
    if ocr_markdown == "" and not skipped_rotated:
        return None
    letter_count = sum(_heading_letter_count(line.text) for line in visible_lines)
    mostly_graphical = page.has_images and letter_count < _GRAPHIC_WARNING_LETTER_LIMIT
    if not (mostly_graphical or skipped_rotated):
        return None
    return (
        f"> **Aviso de conversión (página {page.number}):** esta página contiene principalmente "
        "elementos gráficos o texto girado que no se ha incorporado por completo. "
        "Revisa el PDF original."
    )


def _blocks_to_markdown(blocks: list[_MarkdownBlock]) -> str:
    return _normalized_blocks_to_markdown(_join_hyphenated_block_continuations(blocks))


def _normalized_blocks_to_markdown(blocks: list[_MarkdownBlock]) -> str:
    parts: list[str] = []
    previous_kind: str | None = None
    previous_page_number: int | None = None
    for block in blocks:
        if block.kind == "warning":
            continue
        if block.kind == "heading":
            rendered = f"{'#' * (block.level or 1)} {block.text}"
        else:
            rendered = block.text
        if (
            block.kind in {"list", "toc"}
            and previous_kind == block.kind
            and previous_page_number == block.page_number
            and parts
        ):
            parts[-1] = f"{parts[-1]}\n{rendered}"
        else:
            parts.append(rendered)
        previous_kind = block.kind
        previous_page_number = block.page_number
    markdown = "\n\n".join(part for part in parts if part).strip()
    return _join_page_boundary_hyphenations(markdown)


def _join_page_boundary_hyphenations(markdown: str) -> str:
    pattern = re.compile(
        r"(?P<prefix>[^\W\d_])(?P<hyphen>[-\u00ad])\n\n"
        r"(?P<marker><!--\s*PZDOC PDF PAGE \d+\s*-->)\n\n"
        r"(?P<continuation>[^\W\d_])",
        re.IGNORECASE,
    )

    def join(match: re.Match[str]) -> str:
        continuation = match.group("continuation")
        if not continuation.islower():
            return match.group(0)
        return f"{match.group('prefix')}{match.group('marker')}{continuation}"

    return pattern.sub(join, markdown)


def _review_issues(blocks: list[_MarkdownBlock]) -> tuple[PdfReviewIssue, ...]:
    warning_pages = sorted({block.page_number for block in blocks if block.kind == "warning"})
    issues: list[PdfReviewIssue] = []
    for page_number in warning_pages:
        page_blocks = [block for block in blocks if block.page_number == page_number]
        warning_messages = tuple(
            dict.fromkeys(
                _warning_message(block.text) for block in page_blocks if block.kind == "warning"
            )
        )
        visible_blocks = [
            replace(block) for block in page_blocks if block.kind not in {"warning", "provenance"}
        ]
        visible_markdown = _normalized_blocks_to_markdown(visible_blocks)
        message = " ".join(warning_messages)
        issues.append(
            PdfReviewIssue(
                page_number=page_number,
                message=message,
                markdown=visible_markdown,
                identifier=_pdf_issue_identifier(page_number, visible_markdown, message),
                blocking=not bool(visible_markdown.strip()),
                target_marker=_pdf_page_marker(page_number),
            )
        )
    return tuple(issues)


def _warning_message(warning: str) -> str:
    return _PDF_WARNING_MESSAGE_PATTERN.sub("", warning, count=1).strip()


def _pdf_issue_identifier(page_number: int, markdown: str, message: str) -> str:
    """Return a stable local identifier without exposing document text in logs."""
    payload = f"pdf-page\0{page_number}\0{markdown}\0{message}".encode()
    return sha256(payload).hexdigest()[:24]


def _pdf_page_marker(page_number: int) -> str:
    """Mark a source page without adding reader-visible text to any output."""
    return f"<!-- PZDOC PDF PAGE {page_number} -->"


def _join_hyphenated_block_continuations(
    blocks: list[_MarkdownBlock],
) -> list[_MarkdownBlock]:
    repaired: list[_MarkdownBlock] = []
    for block in blocks:
        if repaired and _hyphenated_blocks_belong_together(repaired[-1], block):
            previous = repaired[-1]
            assert previous.source_line is not None
            assert block.source_line is not None
            previous.text = _join_line_text(
                previous.text,
                block.text,
                previous.source_line,
                block.source_line,
            )
            previous.source_line = block.source_line
            continue
        repaired.append(block)
    return repaired


def _hyphenated_blocks_belong_together(
    previous: _MarkdownBlock,
    current: _MarkdownBlock,
) -> bool:
    previous_line = previous.source_line
    current_line = current.source_line
    if (
        previous.kind != "paragraph"
        or current.kind not in {"paragraph", "heading"}
        or previous_line is None
        or current_line is None
        or previous.page_number != current.page_number
        or not (previous_line.soft_hyphen_end or previous_line.hard_hyphen_end)
    ):
        return False
    if previous_line.soft_hyphen_end:
        return current_line.text[:1].islower()
    return _hard_hyphen_wraps_word(previous_line.text, current_line.text)


def _is_page_number(text: str) -> bool:
    if not _PAGE_NUMBER_PATTERN.fullmatch(text):
        return False
    return not text.isdecimal() or len(text) <= 3


def _apply_links(line: _PdfLine) -> str:
    rendered = line.text
    for link in sorted(line.links, key=lambda item: item.x0, reverse=True):
        label = _linked_text(line.chars, link)
        if label and label in rendered:
            position = (
                rendered.rfind(label) if link.x0 >= line.page_width / 2 else rendered.find(label)
            )
            if position >= 0:
                replacement = f"[{_escape_link_label(label)}](<{_safe_target(link.target)}>)"
                rendered = rendered[:position] + replacement + rendered[position + len(label) :]
                continue
        if link.x0 <= line.x0 + 3 and link.x1 >= line.x1 - 3:
            rendered = f"[{_escape_link_label(rendered)}](<{_safe_target(link.target)}>)"
    return rendered


def _link_has_visible_label(line: _PdfLine, link: _PdfLink) -> bool:
    label = _linked_text(line.chars, link)
    return bool(label and label in line.text) or (link.x0 <= line.x0 + 3 and link.x1 >= line.x1 - 3)


def _linked_text(chars: tuple[_PdfCharacter, ...], link: _PdfLink) -> str:
    selected = [char.text for char in chars if _center_inside(char, link)]
    return _normalize_text("".join(selected))


def _center_inside(char: _PdfCharacter, link: _PdfLink) -> bool:
    center_x = (char.x0 + char.x1) / 2
    center_y = (char.top + char.bottom) / 2
    return link.x0 <= center_x <= link.x1 and link.top <= center_y <= link.bottom


def _overlaps(
    x0: float,
    x1: float,
    top: float,
    bottom: float,
    other_x0: float,
    other_x1: float,
    other_top: float,
    other_bottom: float,
) -> bool:
    return min(x1, other_x1) > max(x0, other_x0) and min(bottom, other_bottom) > max(top, other_top)


def _normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFC", text).replace("\u00ad", "")
    normalized = _repair_suspicious_glyph_encoding(normalized)
    return _WHITESPACE_PATTERN.sub(" ", normalized).strip()


def _repair_suspicious_glyph_encoding(text: str) -> str:
    lowercase_accents = str.maketrans("aeiou", "áéíóú")
    uppercase_accents = str.maketrans("AEIOU", "ÁÉÍÓÚ")

    def accent_vowel(match: re.Match[str]) -> str:
        vowel = match.group(1)
        table = uppercase_accents if vowel.isupper() else lowercase_accents
        return vowel.translate(table)

    repaired = re.sub(r"([aeiouAEIOU])\$(?=[^\W\d_])", accent_vowel, text)

    def repair_word_final_vowel(match: re.Match[str]) -> str:
        word = match.group(1)
        if any(character in "áéíóúÁÉÍÓÚ" for character in word):
            return word
        vowel = word[-1]
        table = uppercase_accents if vowel.isupper() else lowercase_accents
        return f"{word[:-1]}{vowel.translate(table)}"

    repaired = re.sub(
        r"([^\W\d_]*[aeiouAEIOU])\$(?=\s|$|[.,;:!?])",
        repair_word_final_vowel,
        repaired,
    )
    repaired = re.sub(
        r"([bcdfghjklmnpqrstvwxyzBCDFGHJLMNPQRSTVWXYZ])\$\s+(?=[a-záéíóúñ])",
        r"\1",
        repaired,
    )
    repaired = re.sub(
        r"(?i)n\"(?=[a-záéíóú])",
        lambda match: "Ñ" if match.group().isupper() else "ñ",
        repaired,
    )
    repaired = re.sub(r"([AEIOU])K(?=[A-ZÑ]|$)", accent_vowel, repaired)
    return repaired.replace("$", "")


def _margin_key(text: str) -> str:
    return _normalize_text(text).casefold()


def _is_uppercase_text(text: str) -> bool:
    letters = "".join(character for character in text if character.isalpha())
    return bool(letters) and letters == letters.upper()


def _heading_letter_count(text: str) -> int:
    return sum(character.isalpha() for character in text)


def _is_toc_page(lines: list[_PdfLine]) -> bool:
    if not lines:
        return False
    normalized_lines = {_margin_key(line.text) for line in lines}
    if normalized_lines.intersection({"contents", "contenido", "table of contents"}):
        return True
    entry_count = sum(
        bool(re.search(r"\b\d+\s*$", line.text))
        or any(link.target.startswith("#page-") for link in line.links)
        for line in lines
    )
    return entry_count >= 4 and entry_count / len(lines) >= 0.3


def _escape_link_label(label: str) -> str:
    return label.replace("[", "\\[").replace("]", "\\]")


def _safe_target(target: str) -> str:
    return quote(target, safe="/:#?&=@[]!$&'()*+,;~%")
