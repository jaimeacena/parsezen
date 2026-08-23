"""Conservative local PDF extraction with structure, links, and selective OCR."""

from __future__ import annotations

import gc
import inspect
import logging
import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from enum import StrEnum
from hashlib import sha256
from html import escape
from io import BytesIO
from pathlib import Path, PurePosixPath
from statistics import median
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import pdfplumber
from pdfplumber.utils.exceptions import MalformedPDFException, PdfminerException
from PIL import Image, ImageStat

from parsezen.cancellation import CancellationToken, check_cancelled
from parsezen.document_model import RESOURCE_REFERENCE_PREFIX
from parsezen.errors import ConversionError, ImprovementError, LocalModelUnavailableError
from parsezen.ocr_conversion import convert_pdf_pages_with_ocr
from parsezen.pdf_checkpoints import (
    _MAX_PDF_TABLE_CELL_CHARACTERS,
    _MAX_PDF_TABLE_COLUMNS,
    _MAX_PDF_TABLE_ROWS,
    _deserialize_page_checkpoint,
    _serialize_page_checkpoint,
)
from parsezen.pdf_layout import (
    _MarkdownBlock,
    _PdfCharacter,
    _PdfLine,
    _PdfLink,
    _PdfPage,
    _PdfTable,
    _RasterHorizontalRule,
    _TableRendering,
)
from parsezen.translation_quality import markdown_table_shapes
from parsezen.visual_ocr import VisualTextArbiter

LOGGER = logging.getLogger(__name__)

_WHITESPACE_PATTERN = re.compile(r"[\t \u00a0]+")
_PAGE_NUMBER_PATTERN = re.compile(r"(?:\d+|[ivxlcdm]+)", re.IGNORECASE)
_RUNNING_FOOTER_PATTERN = re.compile(
    r"(?:\d+\s+[^\d]{2,40}|[^\d]{2,40}\s+\d+)",
    re.IGNORECASE,
)
_LETTER_PATTERN = re.compile(r"[^\W\d_]", re.UNICODE)
_BULLET_PATTERN = re.compile(r"^[•●◦▪‣⁃]\s*")
_ORDINAL_HEADING_WORDS = (
    r"\d+|[ivxlcdm]+|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez"
)
_CONTAINER_HEADING_PATTERN = re.compile(
    r"^(?:part|parte|book|libro|volume|volumen|section|secci[oó]n|tomo)\s+(?:"
    + _ORDINAL_HEADING_WORDS
    + r")(?:\b|[.:—-])",
    re.IGNORECASE,
)
_CHAPTER_HEADING_PATTERN = re.compile(
    r"^(?:chapter|cap[ií]tulo|chapitre|cap\.)\s+(?:" + _ORDINAL_HEADING_WORDS + r")(?:\b|[.:—-])",
    re.IGNORECASE,
)
_SECTION_HEADING_PATTERN = re.compile(
    r"^(?:(?:chapter|cap[ií]tulo|chapitre|cap\.)|"
    r"(?:part|parte|book|libro|volume|volumen|section|secci[oó]n|tomo))\s+(?:"
    + _ORDINAL_HEADING_WORDS
    + r")(?:\b|[.:—-])",
    re.IGNORECASE,
)
_SPACED_WORD_PATTERN = re.compile(
    r"(?<!\w)(?:[^\W\d_]\s+){4,}[^\W\d_](?!\w)",
    re.UNICODE,
)
_ADJACENT_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(<([^>]+)>\)\s+\[([^\]]+)\]\(<\2>\)")
_MAX_HEADING_LENGTH = 120
_MAX_BODY_SIZE_UPPERCASE_HEADING_WORDS = 10
_DUPLICATE_OVERLAP_RATIO = 0.55
_DEDUPLICATION_GRID_SIZE = 16.0
_MAX_CHARACTER_GRID_CELLS = 1_024
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
_MIN_COMPOSITE_IMAGE_COUNT = 2
_MIN_COMPOSITE_IMAGE_UNION_RATIO = 0.08
_MIN_COMPOSITE_IMAGE_DENSITY = 0.55
_OCR_TITLE_TARGET_LAST_PAGE = 4
_OCR_TITLE_REFERENCE_LAST_PAGE = 12
_OCR_TITLE_CONNECTORS = frozenset(
    {
        "a",
        "an",
        "and",
        "da",
        "das",
        "de",
        "del",
        "della",
        "der",
        "des",
        "die",
        "do",
        "dos",
        "du",
        "e",
        "el",
        "en",
        "et",
        "for",
        "in",
        "la",
        "las",
        "le",
        "les",
        "los",
        "o",
        "of",
        "on",
        "or",
        "para",
        "the",
        "to",
        "und",
        "von",
        "y",
        "zu",
    }
)
_TOC_REFERENCE_PREFIX_PATTERN = re.compile(
    r"^(?P<prefix>(?:(?:table|tabla|cuadro)\s+)?\d+\s*[.\-:)]\s*)(?P<label>.+)$",
    re.IGNORECASE,
)
_TOC_HEADING_PATTERN = re.compile(
    r"^(?:table of contents|contents|content|index|índice|indice|sumario|contenido)$",
    re.IGNORECASE,
)
_TOC_DOTTED_FOLIO_PATTERN = re.compile(
    r"(?P<label>.*[^\W\d_].*?)\s*\.{2,}\s*"
    r"(?P<folio>(?:\d[ \t]*){1,3}|[ivxlcdm]{1,8})\s*$",
    re.IGNORECASE,
)
_MARKDOWN_LINK_DESTINATION_SPLIT_PATTERN = re.compile(r"(\]\(<[^>]*>\))")
_TABLE_CAPTION_LINE_PATTERN = re.compile(
    r"^(?:table|tabla|cuadro)\s+(?:\d+|[ivxlcdm]+)\s*[.\-:]",
    re.IGNORECASE,
)
_ROMAN_HEADING_REFERENCE_PATTERN = re.compile(
    r"^(?P<prefix>.+?)\s+(?P<roman>[IVXLCDM]{1,6})(?=\s*(?::|[-–—]|$))"
)
_FULL_PAGE_EXPORT_LETTER_LIMIT = 300
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
_PDF_EXTRACTION_SHARD_SIZE = 32
_SUSPICIOUS_NUMERIC_GLYPH_PATTERN = re.compile(
    r"(?<!\w)(?:"
    r"(?=[\d$^]{2,6}(?!\w))(?=[\d$^]*[$^])[\d$^]{2,6}"
    r"|(?=[\dA-Za-z]{2,7}(?!\w))(?=[\dA-Za-z]*\d)(?=[\dA-Za-z]*[A-Za-z])"
    r"[\dA-Za-z]{2,7}"
    r")(?!\w)"
)
_SPACED_NUMERIC_YEAR_GLYPH_PATTERN = re.compile(r"^\s*[iIlLoO](?:\s+[iIlLoO]){3}\s*$")
_PUBLICATION_YEAR_PATTERN = re.compile(r"(?<!\d)(?:18|19|20|21)\d{2}(?!\d)")
_PUBLICATION_YEAR_CONTEXT_PATTERN = re.compile(
    r"(?:first\s+published(?:\s+in)?|primera\s+edici[oó]n\s+publicada(?:\s+en)?|"
    r"copyright|©)[^0-9]{0,64}(?P<year>(?:18|19|20|21)\d{2})",
    re.IGNORECASE,
)
_NUMERIC_GLYPH_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "I": ("1",),
    "i": ("1",),
    "l": ("1",),
    "O": ("0",),
    "o": ("0",),
    "H": ("11",),
    "h": ("11",),
    "n": ("11",),
    "S": ("5", "8"),
    "s": ("5", "8"),
    "B": ("8",),
    "b": ("8",),
}
_VISUAL_ATOM_PATTERN = re.compile(
    r"[\d$^][\d$^A-Za-z]{1,6}(?=(?:[.)](?:\s|$)|\s|$))"
    r"|[\d$^\ufffd°]+(?:[.,][\d$^\ufffd°]+)*"
    r"|(?:[^\W\d_]|\ufffd)+(?:[’'\-](?:[^\W\d_]|\ufffd)+)*"
    r"|[^\w\s]",
    re.UNICODE,
)
_MAX_VISUAL_ARBITRATION_REGIONS = 8
_MAX_VISUAL_ARBITRATION_REGIONS_PER_PAGE = 2
_MAX_HIDDEN_TEXT_AUDIT_PAGES = 6
_MAX_VISUAL_CROP_BYTES = 4 * 1024 * 1024


def strip_pdf_page_markers(markdown: str) -> str:
    """Remove private page anchors before publishing user-visible text."""
    return _PDF_PAGE_MARKER_PATTERN.sub("", markdown)


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
PdfVisualArbiterFactory = Callable[[], VisualTextArbiter | None]


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
    ocr_failed_pages: tuple[int, ...] = ()
    required_ocr_failed_pages: tuple[int, ...] = ()
    secondary_native_pages: tuple[int, ...] = ()
    secondary_native_arbitrated_regions: int = 0
    visual_reviewed_regions: int = 0
    visual_arbitrated_regions: int = 0


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


@dataclass(frozen=True, slots=True)
class _PdfVisualDisagreement:
    page_number: int
    line: _PdfLine
    ocr_text: str
    priority: int


def extract_pdf_warning_pages(markdown: str) -> tuple[int, ...]:
    """Return sorted PDF page numbers explicitly marked for human review."""
    return tuple(
        sorted({int(match.group(1)) for match in _PDF_WARNING_PAGE_PATTERN.finditer(markdown)})
    )


def resolve_pdf_page_range(
    source_path: Path,
    requested: PdfPageRange,
    *,
    cancellation: CancellationToken | None = None,
) -> PdfPageRange:
    """Clamp a valid requested range to the real final page of a local PDF."""
    check_cancelled(cancellation)
    _validate_pdf_header(source_path)
    _validate_page_range_values(requested)
    try:
        with pdfplumber.open(source_path, unicode_norm="NFC") as pdf:
            resolved = _resolve_page_range(len(pdf.pages), requested)
            check_cancelled(cancellation)
            return resolved
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
    visual_arbiter_factory: PdfVisualArbiterFactory | None = None,
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
        visual_arbiter_factory=visual_arbiter_factory,
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
    visual_arbiter_factory: PdfVisualArbiterFactory | None = None,
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
    ocr_pages, ocr_failed_pages, required_ocr_failed_pages = _run_planned_ocr(
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
    rendered_ocr_pages = _repair_repeated_front_matter_ocr_titles(
        pages,
        ocr_pages,
        body_size,
    )
    secondary_native_text = _extract_secondary_native_text(
        source_path,
        {
            page.number
            for page in pages
            if page.number in rendered_ocr_pages
            and not _should_replace_with_ocr(page, rendered_ocr_pages[page.number])
        },
        cancellation,
    )
    pages, secondary_native_arbitrated_regions = _reconcile_secondary_native_text(
        pages,
        rendered_ocr_pages,
        secondary_native_text,
    )
    visual_reviewed_regions = 0
    visual_arbitrated_regions = 0
    if visual_arbiter_factory is not None:
        pages, visual_reviewed_regions, visual_arbitrated_regions = (
            _arbitrate_visual_text_disagreements(
                source_path,
                pages,
                rendered_ocr_pages,
                visual_arbiter_factory,
                cancellation,
            )
        )
    markdown, review_issues = _render_document(
        pages,
        body_size,
        heading_sizes,
        repeated_margins,
        referenced_pages,
        rendered_ocr_pages,
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
        rendered_ocr_pages,
        ocr_failed_pages=ocr_failed_pages,
        required_ocr_failed_pages=required_ocr_failed_pages,
        secondary_native_pages=set(secondary_native_text),
        secondary_native_arbitrated_regions=secondary_native_arbitrated_regions,
        visual_reviewed_regions=visual_reviewed_regions,
        visual_arbitrated_regions=visual_arbitrated_regions,
    )
    return PdfConversionResult(
        f"{markdown}\n",
        tuple(resource for page in page_images.values() for resource in page),
        omitted_images,
    )


def _build_ocr_plan(pages: list[_PdfPage], force_ocr: bool) -> _PdfOcrPlan:
    hidden_text_audit_pages = _hidden_text_audit_pages(pages)
    page_numbers = {page.number for page in pages} if force_ocr else _pages_requiring_ocr(pages)
    page_numbers.update(hidden_text_audit_pages)
    force_full_page_numbers = {
        page.number
        for page in pages
        if (
            _has_suspicious_glyph_encoding(page)
            or _has_suspicious_numeric_glyph_encoding(page)
            or page.image_orientation_mismatch
            or (
                bool(_page_letter_count(page) or page.has_images)
                and _native_page_quality(page) < _VERY_LOW_NATIVE_QUALITY_THRESHOLD
            )
            or page.number in hidden_text_audit_pages
        )
    }
    if force_ocr:
        force_full_page_numbers.update(page_numbers)
    required_page_numbers = {
        page.number
        for page in pages
        if (_page_letter_count(page) < _MIN_USABLE_NATIVE_LETTERS and page.has_images)
        or (_page_letter_count(page) > 0 and _native_page_quality(page) < 0.16)
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
) -> tuple[dict[int, str], set[int], set[int]]:
    if not plan.page_numbers:
        return {}, set(), set()
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
        return cached_pages, set(), set()
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

    ocr_arguments: dict[str, Any] = {}
    ocr_parameters = inspect.signature(convert_pdf_pages_with_ocr).parameters
    if save_checkpoint is not None and "on_page_result" in ocr_parameters:
        ocr_arguments["on_page_result"] = persist_page
    force_full_page_numbers = (
        plan.force_full_page_numbers & pending_pages
        if "force_full_page_numbers" in ocr_parameters
        else set()
    )
    if force_full_page_numbers:
        ocr_arguments["force_full_page_numbers"] = force_full_page_numbers
    if progress is not None:
        ocr_arguments["on_progress"] = progress

    try:
        if cancellation is None:
            ocr_pages = convert_pdf_pages_with_ocr(
                source_path,
                pending_pages,
                **ocr_arguments,
            )
        else:
            ocr_pages = convert_pdf_pages_with_ocr(
                source_path,
                pending_pages,
                cancellation,
                **ocr_arguments,
            )
    except ConversionError:
        # A worker can fail after returning valid cached pages. Keep those
        # pages and only classify the unresolved subset as failed. Required
        # pages still preserve the historic blocking behaviour.
        failed_pages = set(plan.page_numbers) - set(cached_pages)
        required_failed_pages = failed_pages & plan.required_page_numbers
        if required_failed_pages:
            raise
        return cached_pages, failed_pages, required_failed_pages
    ocr_pages = {**cached_pages, **ocr_pages}
    failed_pages = set(plan.page_numbers) - set(ocr_pages)
    required_failed_pages = failed_pages & plan.required_page_numbers
    return ocr_pages, failed_pages, required_failed_pages


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
    *,
    ocr_failed_pages: set[int] | None = None,
    required_ocr_failed_pages: set[int] | None = None,
    secondary_native_pages: set[int] | None = None,
    secondary_native_arbitrated_regions: int = 0,
    visual_reviewed_regions: int = 0,
    visual_arbitrated_regions: int = 0,
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
            ocr_failed_pages=tuple(sorted(ocr_failed_pages or set())),
            required_ocr_failed_pages=tuple(sorted(required_ocr_failed_pages or set())),
            secondary_native_pages=tuple(sorted(secondary_native_pages or set())),
            secondary_native_arbitrated_regions=secondary_native_arbitrated_regions,
            visual_reviewed_regions=visual_reviewed_regions,
            visual_arbitrated_regions=visual_arbitrated_regions,
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
        page_ids = {
            page_id: page_number
            for page_number in range(resolved_range.first_page, resolved_range.last_page + 1)
            for page in (pdf.pages[page_number - 1],)
            if isinstance((page_id := page.page_obj.pageid), int)
        }
        pages: list[_PdfPage] = []
        total_pages = resolved_range.last_page - resolved_range.first_page + 1
        for shard_start in range(0, total_pages, _PDF_EXTRACTION_SHARD_SIZE):
            shard_end = min(total_pages, shard_start + _PDF_EXTRACTION_SHARD_SIZE)
            for offset in range(shard_start, shard_end):
                current = offset + 1
                page_number = resolved_range.first_page + offset
                page = pdf.pages[page_number - 1]
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
                tables = _extract_tables(page)
                if not tables and _has_spatial_table_candidate(lines):
                    tables = _extract_spatial_tables(page, lines)
                extracted_page = _PdfPage(
                    number=page_number,
                    lines=lines,
                    has_images=bool(page.images),
                    image_area_ratios=_image_area_ratios(page),
                    has_table=bool(tables),
                    image_orientation_mismatch=_image_orientation_mismatch(page),
                    tables=tables,
                )
                pages.append(extracted_page)
                filtered_page.close()
                page.close()
                if save_checkpoint is not None:
                    save_checkpoint(page_number, _serialize_page_checkpoint(extracted_page))
                if on_progress is not None:
                    on_progress(PdfProgressPhase.EXTRACTING, current, total_pages)
            # pdfplumber's transient character/table objects can be sizeable;
            # release them between shards while retaining only compact models.
            gc.collect()
        check_cancelled(cancellation)
        return pages


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
            total = resolved_range.last_page - resolved_range.first_page + 1
            for current, page_number in enumerate(
                range(resolved_range.first_page, resolved_range.last_page + 1),
                start=1,
            ):
                page = pdf.pages[page_number - 1]
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
                    bbox_area_ratio = (
                        (bbox[2] - bbox[0])
                        * (bbox[3] - bbox[1])
                        / max(float(page.width) * float(page.height), 1.0)
                    )
                    if (
                        bbox_area_ratio >= _MAX_EXPORTED_IMAGE_AREA_RATIO
                        and not _MARKDOWN_TABLE_PATTERN.search(ocr_pages.get(page_number, ""))
                        and _is_nearly_blank_image(content)
                    ):
                        omitted += 1
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

    for table in model.tables:
        if table.rendering is not _TableRendering.STRUCTURED_TEXT:
            continue
        x0, top, x1, bottom = table.bbox
        ratio = (x1 - x0) * (bottom - top) / page_area
        if _MIN_EXPORTED_IMAGE_AREA_RATIO <= ratio < _MAX_EXPORTED_IMAGE_AREA_RATIO:
            candidates.append((ratio, table.bbox))

    # A page-sized image is normally a scan or decorative background. Keep at most one only
    # when text remains sparse enough for the raster to carry substantial visual information
    # (for example, a cover, illustrated plate or hybrid notice), or when another fidelity
    # signal makes the raster necessary. Dense text-layer scans remain reflowable-only.
    recognized_letters = _heading_letter_count(ocr_markdown or "")
    useful_letters = max(_page_letter_count(model), recognized_letters)
    ocr_contains_table = bool(_MARKDOWN_TABLE_PATTERN.search(ocr_markdown or ""))
    ocr_is_toc = _is_toc_markdown(ocr_markdown or "")
    native_is_toc = _is_toc_page(list(model.lines))
    fragmented_graphic_text = _fragmented_graphic_text_should_stay_in_image(
        model,
        ocr_markdown,
    )
    if fragmented_graphic_text and full_page:
        # On charts, diagrams and decorated plates, dozens of tiny labels can look
        # superficially OCR-like while losing all spatial meaning when reflowed.
        # The page raster is the canonical representation in that case.
        candidates = [max(full_page, key=lambda item: item[0])]
    elif (
        not candidates
        and full_page
        and not native_is_toc
        and not ocr_is_toc
        and (
            useful_letters < _FULL_PAGE_EXPORT_LETTER_LIMIT
            or model.image_orientation_mismatch
            or ocr_contains_table
        )
    ):
        candidates.append(max(full_page, key=lambda item: item[0]))
    composite = _composite_image_candidate(candidates, page_area)
    if composite is not None:
        candidates = [composite]
    candidates.sort(key=lambda item: (item[1][1], item[1][0], -item[0]))
    retained: list[tuple[float, float, float, float]] = []
    for _ratio, bbox in candidates:
        if any(_bbox_overlap_ratio(bbox, existing) >= 0.85 for existing in retained):
            continue
        retained.append(bbox)
    return tuple(retained)


def _composite_image_candidate(
    candidates: list[tuple[float, tuple[float, float, float, float]]],
    page_area: float,
) -> tuple[float, tuple[float, float, float, float]] | None:
    """Keep a dense PDF mosaic in its original spatial arrangement as one crop."""

    if len(candidates) < _MIN_COMPOSITE_IMAGE_COUNT:
        return None
    boxes = [bbox for _ratio, bbox in candidates]
    union = (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )
    union_area = max((union[2] - union[0]) * (union[3] - union[1]), 1.0)
    union_ratio = min(union_area / max(page_area, 1.0), 1.0)
    placed_area = sum(max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1]) for box in boxes)
    density = min(placed_area / union_area, 1.0)
    if union_ratio < _MIN_COMPOSITE_IMAGE_UNION_RATIO or density < _MIN_COMPOSITE_IMAGE_DENSITY:
        return None
    return union_ratio, union


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


def _is_nearly_blank_image(content: bytes) -> bool:
    """Reject page backgrounds with no meaningful dark or contrasting visual content."""

    try:
        with Image.open(BytesIO(content)) as image:
            grayscale = image.convert("L")
            grayscale.thumbnail((256, 256))
            histogram = grayscale.histogram()
            total = max(sum(histogram), 1)
            dark_ratio = sum(histogram[:210]) / total
            deviation = ImageStat.Stat(grayscale).stddev[0]
    except (OSError, TypeError, ValueError):
        return False
    return deviation < 10.0 and dark_ratio < 0.002


def _has_table_candidate(page: Any) -> bool:
    return bool(_extract_tables(page))


def _extract_tables(page: Any) -> tuple[_PdfTable, ...]:
    """Extract bounded tables and choose the least lossy portable representation."""

    if len(page.lines) + len(page.rects) < 4:
        return ()
    try:
        tables = page.find_tables()
    except (PdfminerException, TypeError, ValueError):
        return ()
    extracted: list[_PdfTable] = []
    for table in tables:
        data = table.extract() or []
        rows = tuple(tuple(_normalize_table_cell(cell) for cell in row) for row in data)
        column_count = max((len(row) for row in rows), default=0)
        populated_cells = sum(bool(cell) for row in rows for cell in row)
        if (
            len(rows) < 2
            or len(rows) > _MAX_PDF_TABLE_ROWS
            or column_count < 2
            or column_count > _MAX_PDF_TABLE_COLUMNS
            or populated_cells < 4
            or any(len(cell) > _MAX_PDF_TABLE_CELL_CHARACTERS for row in rows for cell in row)
        ):
            continue
        try:
            bbox = tuple(float(value) for value in table.bbox)
        except (TypeError, ValueError):
            continue
        if (
            len(bbox) != 4
            or not all(math.isfinite(value) for value in bbox)
            or bbox[0] < 0
            or bbox[1] < 0
            or bbox[0] >= bbox[2]
            or bbox[1] >= bbox[3]
            or bbox[2] > float(page.width)
            or bbox[3] > float(page.height)
        ):
            continue
        normalized_rows = tuple(row + ("",) * (column_count - len(row)) for row in rows)
        rendering = _table_rendering(normalized_rows, column_count)
        extracted.append(
            _PdfTable(
                (bbox[0], bbox[1], bbox[2], bbox[3]),
                normalized_rows,
                rendering,
            )
        )
    return tuple(extracted)


def _normalize_table_cell(value: object) -> str:
    if value is None:
        return ""
    normalized = str(value).replace("\r", "").replace("\u00ad\n", "").replace("\u00ad", "")
    return re.sub(r"[ \t]+", " ", normalized).strip()


def _render_table_cell(value: str) -> str:
    source_lines = value.split("\n")
    normalized_lines: list[str] = []
    for line in source_lines:
        if not normalized_lines:
            normalized_lines.append(line)
        elif normalized_lines[-1].endswith("-") and _hard_hyphen_wraps_word(
            normalized_lines[-1], line
        ):
            normalized_lines[-1] = f"{normalized_lines[-1][:-1]}{line.lstrip()}"
        else:
            normalized_lines.append(line)
    return "\n".join(normalized_lines)


def _table_rendering(
    rows: tuple[tuple[str, ...], ...],
    column_count: int,
) -> _TableRendering:
    longest = max((len(cell) for row in rows for cell in row), default=0)
    multiline = any("\n" in cell for row in rows for cell in row)
    if len(rows) > 80 or column_count > 16 or longest > 2_000:
        return _TableRendering.STRUCTURED_TEXT
    if column_count <= 8 and longest <= 220 and not multiline:
        return _TableRendering.MARKDOWN
    return _TableRendering.HTML


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
    italic_characters = sum(
        max(len(str(char.get("text", ""))), 1)
        for char in raw_chars
        if _is_italic_font(str(char.get("fontname", "")))
    )
    rotated_characters = sum(
        max(len(str(char.get("text", ""))), 1) for char in raw_chars if char.get("upright") is False
    )
    bold = weighted_characters > 0 and bold_characters / weighted_characters >= 0.55
    italic = weighted_characters > 0 and italic_characters / weighted_characters >= 0.55
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
        italic=italic,
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
    split_threshold = max(line.page_width * 0.045, line.font_size * 3)
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
    if any(not any(character.text.isalnum() for character in group) for group in groups):
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
    font_sizes = [line.font_size for line in lines if line.font_size > 0]
    typical_font_size = median(font_sizes) if font_sizes else 10.0
    for line in ordered:
        width = max(0.0, line.x1 - line.x0)
        separator = width >= page_width * 0.58 or (
            line.centered
            and width >= page_width * 0.25
            and (line.bold or line.font_size >= typical_font_size * 1.15)
        )
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
    ordered = sorted(lines, key=lambda line: (line.x0, line.top, line.bottom))
    groups: list[list[_PdfLine]] = [[ordered[0]]]
    minimum_column_gap = page_width * 0.12
    for line in ordered[1:]:
        if line.x0 - groups[-1][-1].x0 >= minimum_column_gap:
            groups.append([line])
        else:
            groups[-1].append(line)
    if not 2 <= len(groups) <= 4 or any(len(group) < _MIN_COLUMN_LINES for group in groups):
        return sorted(lines, key=lambda line: (line.top, line.x0, line.bottom))

    for group, following in zip(groups, groups[1:], strict=False):
        next_column_start = min(line.x0 for line in following)
        crossing_lines = sum(line.x1 + page_width * 0.01 > next_column_start for line in group)
        if crossing_lines > max(1, round(len(group) * 0.08)):
            return sorted(lines, key=lambda line: (line.top, line.x0, line.bottom))
    vertical_spans = [
        (min(line.top for line in group), max(line.bottom for line in group)) for group in groups
    ]
    if min(bottom for _top, bottom in vertical_spans) <= max(
        top for top, _bottom in vertical_spans
    ):
        return sorted(lines, key=lambda line: (line.top, line.x0, line.bottom))
    return [
        line
        for group in groups
        for line in sorted(group, key=lambda item: (item.top, item.x0, item.bottom))
    ]


def _reconcile_suspicious_toc_numbers(
    lines: list[_PdfLine],
    ocr_markdown: str | None,
) -> list[_PdfLine]:
    """Decode broken numeric glyphs only with independent or sequential evidence.

    The selectable layer remains authoritative for every letter and for layout. A suspicious
    token changes only when local OCR or neighbouring clean folios leave one numeric reading.
    """

    ocr_numbers = set(re.findall(r"(?<!\d)\d{1,6}(?!\d)", ocr_markdown or ""))
    native_folios = tuple(
        (line, int(compact))
        for line in lines
        for compact in (re.sub(r"\s+", "", line.text),)
        if re.fullmatch(r"\d{1,6}", compact)
    )
    has_trailing_native_folios = any(
        re.search(r"(?<!\w)\d{1,4}\s*$", line.text) is not None for line in lines
    )
    if not ocr_numbers and not native_folios and not has_trailing_native_folios:
        return lines

    ocr_lines = tuple(line for line in (ocr_markdown or "").splitlines() if line.strip())

    def lexical_key(value: str) -> str:
        words = re.findall(r"[^\W\d_]{2,}", value.casefold(), re.UNICODE)
        return " ".join(words)

    def line_ocr_numbers(source_line: str) -> set[str]:
        source_key = lexical_key(source_line)
        if not source_key:
            return ocr_numbers
        ranked = sorted(
            (
                SequenceMatcher(None, source_key, lexical_key(candidate), autojunk=False).ratio(),
                candidate,
            )
            for candidate in ocr_lines
            if lexical_key(candidate)
        )
        if not ranked or ranked[-1][0] < 0.72:
            return ocr_numbers
        best_score = ranked[-1][0]
        best_lines = [candidate for score, candidate in ranked if score >= best_score - 0.02]
        return set(re.findall(r"(?<!\d)\d{1,6}(?!\d)", "\n".join(best_lines))) or ocr_numbers

    def ocr_evidence_text(source_line: _PdfLine) -> str:
        """Attach a detached folio to its visual label before matching OCR text."""

        if lexical_key(source_line.text):
            return source_line.text
        row_labels = [
            candidate
            for candidate in lines
            if candidate is not source_line
            and candidate.x0 < source_line.x0
            and _heading_letter_count(candidate.text) >= 2
            and _toc_lines_share_row(candidate, source_line)
        ]
        if not row_labels:
            return source_line.text
        label = min(
            row_labels,
            key=lambda candidate: (
                abs((candidate.top + candidate.bottom) - (source_line.top + source_line.bottom)),
                -candidate.x1,
            ),
        )
        return f"{label.text} {source_line.text}"

    def native_sequence_numbers(source_line: _PdfLine) -> set[str]:
        """Constrain one detached broken folio by its neighbours in the same column."""

        token = re.sub(r"\s+", "", source_line.text)
        if _SUSPICIOUS_NUMERIC_GLYPH_PATTERN.fullmatch(token) is None:
            return set()
        column_tolerance = source_line.page_width * 0.08
        preceding = [
            (line, value)
            for line, value in native_folios
            if line.top < source_line.top - 0.5
            and abs(line.x0 - source_line.x0) <= column_tolerance
        ]
        following = [
            (line, value)
            for line, value in native_folios
            if line.top > source_line.top + 0.5
            and abs(line.x0 - source_line.x0) <= column_tolerance
        ]
        if not preceding or not following:
            return set()
        lower = max(preceding, key=lambda item: item[0].top)[1]
        upper = min(following, key=lambda item: item[0].top)[1]
        if lower > upper:
            return set()
        return {
            candidate
            for candidate in _numeric_glyph_candidates(token)
            if lower <= int(candidate) <= upper
        }

    def reconcile_token(match: re.Match[str], confirmed_numbers: set[str]) -> str:
        token = match.group(0)
        candidates = _numeric_glyph_candidates(token)
        if not candidates:
            return token
        # A trailing dollar sign can be a real currency marker.  Without a following ordinal
        # separator or another digit it remains visible and is reported for review.
        if "$" in token and match.end() == len(match.string):
            return token
        confirmed = candidates & confirmed_numbers
        return next(iter(confirmed)) if len(confirmed) == 1 else token

    reconciled: list[_PdfLine] = []
    for line_index, line in enumerate(lines):
        native_sequence = native_sequence_numbers(line)
        ocr_confirmed_numbers = line_ocr_numbers(ocr_evidence_text(line))
        native_ocr_consensus = native_sequence & ocr_confirmed_numbers
        confirmed_numbers = native_ocr_consensus or native_sequence or ocr_confirmed_numbers

        def reconcile_with_order(
            match: re.Match[str],
            confirmed: set[str] = confirmed_numbers,
            current_line_index: int = line_index,
        ) -> str:
            independently_confirmed = reconcile_token(match, confirmed)
            if independently_confirmed != match.group(0):
                return independently_confirmed
            ordered = _ordered_toc_folio_candidates(lines, current_line_index, match)
            return reconcile_token(match, ordered)

        text = _SUSPICIOUS_NUMERIC_GLYPH_PATTERN.sub(reconcile_with_order, line.text)
        reconciled.append(replace(line, text=text) if text != line.text else line)
    return reconciled


def _reconcile_toc_numbers_with_native_priority(
    lines: list[_PdfLine],
    ocr_markdown: str | None,
) -> list[_PdfLine]:
    """Prefer a unique native folio sequence over conflicting whole-page OCR."""

    native_reconciled = _reconcile_suspicious_toc_numbers(lines, None)
    if not ocr_markdown:
        return native_reconciled
    ocr_reconciled = _reconcile_suspicious_toc_numbers(lines, ocr_markdown)
    return [
        native_candidate if native_candidate.text != source.text else ocr_candidate
        for source, native_candidate, ocr_candidate in zip(
            lines,
            native_reconciled,
            ocr_reconciled,
            strict=True,
        )
    ]


def _reconcile_spaced_numeric_year(
    lines: list[_PdfLine],
    ocr_markdown: str | None,
    publication_years: set[str] | None = None,
) -> list[_PdfLine]:
    """Restore an obfuscated year only when independent publication evidence is unique."""

    local_years = set(_PUBLICATION_YEAR_PATTERN.findall(ocr_markdown or ""))
    years = local_years if len(local_years) == 1 else publication_years or set()
    if len(years) != 1:
        return lines
    replacement = next(iter(years))
    return [
        replace(line, text=replacement)
        if _SPACED_NUMERIC_YEAR_GLYPH_PATTERN.fullmatch(line.text)
        else line
        for line in lines
    ]


def _publication_year_evidence(
    pages: list[_PdfPage],
    ocr_pages: dict[int, str],
) -> set[str]:
    """Collect explicit publication/copyright years without treating arbitrary dates as evidence."""

    sources = [" ".join(line.text for line in page.lines if not line.rotated) for page in pages]
    sources.extend(ocr_pages.values())
    return {
        match.group("year")
        for source in sources
        for match in _PUBLICATION_YEAR_CONTEXT_PATTERN.finditer(source)
    }


def _numeric_glyph_candidates(token: str) -> set[str]:
    """Expand common broken-font number shapes without selecting one by itself."""

    candidates = {""}
    has_uncertain_shape = False
    for character in token:
        replacements: tuple[str, ...]
        if character.isdigit():
            replacements = (character,)
        elif character in _NUMERIC_GLYPH_EXPANSIONS:
            replacements = _NUMERIC_GLYPH_EXPANSIONS[character]
            has_uncertain_shape = True
        elif character in "$^" or character.isalpha():
            replacements = tuple("0123456789")
            has_uncertain_shape = True
        else:
            return set()
        candidates = {
            f"{prefix}{replacement}" for prefix in candidates for replacement in replacements
        }
        if len(candidates) > 1_000:
            return set()
    return {
        candidate
        for candidate in candidates
        if has_uncertain_shape
        and candidate.isdecimal()
        and 1 <= len(candidate) <= 4
        and not candidate.startswith("0")
        and 1 <= int(candidate) <= 9_999
    }


def _ordered_toc_folio_candidates(
    lines: list[_PdfLine],
    line_index: int,
    match: re.Match[str],
) -> set[str]:
    """Use neighbouring clean TOC folios to select a broken trailing number."""

    if match.end() != len(match.string):
        return set()
    candidates = _numeric_glyph_candidates(match.group(0))
    if not candidates:
        return set()

    def trailing_folio(line: _PdfLine) -> int | None:
        found = re.search(r"(?<!\w)(\d{1,4})\s*$", line.text)
        return int(found.group(1)) if found is not None else None

    lower = next(
        (
            value
            for candidate_line in reversed(lines[:line_index])
            for value in (trailing_folio(candidate_line),)
            if value is not None
        ),
        None,
    )
    upper = next(
        (
            value
            for candidate_line in lines[line_index + 1 :]
            for value in (trailing_folio(candidate_line),)
            if value is not None
        ),
        None,
    )
    if lower is not None and upper is not None and lower <= upper:
        return {candidate for candidate in candidates if lower <= int(candidate) <= upper}
    neighbour = lower if lower is not None else upper
    if neighbour is None:
        return set()
    return {candidate for candidate in candidates if int(candidate) == neighbour}


def _normalize_toc_entry_rows(lines: list[_PdfLine]) -> list[_PdfLine]:
    """Pair a detached page-number column with its TOC entries by geometry."""

    if len(lines) < 6:
        return lines
    page_width = max((line.page_width for line in lines), default=0.0)
    if page_width <= 0:
        return lines
    number_indices = [
        index
        for index, line in enumerate(lines)
        if not line.rotated
        and line.x0 >= page_width * 0.35
        and _is_toc_folio(re.sub(r"\s+", "", line.text))
    ]
    if len(number_indices) < 3:
        return lines

    entry_to_number: dict[int, int] = {}
    used_entries: set[int] = set()
    for number_index in number_indices:
        number = lines[number_index]
        candidates = [
            (index, line)
            for index, line in enumerate(lines)
            if index not in used_entries
            and index not in number_indices
            and not line.rotated
            and _heading_letter_count(line.text) >= 2
            and line.x0 < number.x0
            and line.x1 <= number.x0 + max(2.0, line.font_size * 0.4)
            and number.x0 - line.x0 >= page_width * 0.18
            and _toc_lines_share_row(line, number)
        ]
        if not candidates:
            continue
        entry_index, _entry = min(
            candidates,
            key=lambda item: (
                abs((item[1].top + item[1].bottom) - (number.top + number.bottom)),
                -item[1].x0,
                -item[1].x1,
            ),
        )
        entry_to_number[entry_index] = number_index
        used_entries.add(entry_index)

    if len(entry_to_number) < 3 or len(entry_to_number) / len(number_indices) < 0.75:
        return lines

    paired_number_indices = set(entry_to_number.values())
    normalized: list[_PdfLine] = []
    for index, line in enumerate(lines):
        if index in paired_number_indices:
            continue
        matched_number_index = entry_to_number.get(index)
        if matched_number_index is None:
            normalized.append(line)
            continue
        normalized.append(_merge_toc_entry_number_line(line, lines[matched_number_index]))
    # Keep the extractor's already validated column order. Only remove the
    # detached folio column; sorting by row here would interleave a genuine
    # two-column contents page.
    return normalized


def _reconcile_toc_spacing_from_ocr(
    lines: list[_PdfLine],
    ocr_markdown: str | None,
) -> list[_PdfLine]:
    """Restore only missing word boundaries corroborated by the local OCR layer."""

    ocr_lines = _visible_ocr_lines(ocr_markdown or "")
    if not ocr_lines:
        return lines

    def compact_with_boundaries(text: str) -> tuple[str, set[int]]:
        compact: list[str] = []
        boundaries: set[int] = set()
        pending_space = False
        for character in text.strip():
            if character.isspace():
                pending_space = bool(compact)
                continue
            if pending_space:
                boundaries.add(len(compact))
            compact.append(character)
            pending_space = False
        return "".join(compact), boundaries

    reconciled: list[_PdfLine] = []
    for line in lines:
        match = _best_matching_text_line(line.text, ocr_lines)
        if match is None or match[0] < 0.86:
            reconciled.append(line)
            continue
        candidate = _aligned_visual_candidate(line.text, match[1])
        native_compact, native_boundaries = compact_with_boundaries(line.text)
        ocr_compact, ocr_boundaries = compact_with_boundaries(candidate)
        if (
            len(native_compact) != len(ocr_compact)
            or _levenshtein_distance(native_compact.casefold(), ocr_compact.casefold()) > 2
        ):
            reconciled.append(line)
            continue
        added_boundaries = {
            position
            for position in ocr_boundaries - native_boundaries
            if 0 < position < len(native_compact)
            and native_compact[position - 1].isalpha()
            and native_compact[position].isalpha()
        }
        if not added_boundaries:
            reconciled.append(line)
            continue
        all_boundaries = native_boundaries | added_boundaries
        text = "".join(
            f" {character}" if index in all_boundaries else character
            for index, character in enumerate(native_compact)
        )
        reconciled.append(replace(line, text=text))
    return reconciled


def _toc_lines_share_row(entry: _PdfLine, number: _PdfLine) -> bool:
    entry_height = max(0.1, entry.bottom - entry.top)
    number_height = max(0.1, number.bottom - number.top)
    shorter_height = min(entry_height, number_height)
    overlap = min(entry.bottom, number.bottom) - max(entry.top, number.top)
    entry_center = (entry.top + entry.bottom) / 2
    number_center = (number.top + number.bottom) / 2
    return overlap >= shorter_height * 0.55 or abs(entry_center - number_center) <= max(
        1.5,
        shorter_height * 0.35,
    )


def _merge_toc_entry_number_line(entry: _PdfLine, number: _PdfLine) -> _PdfLine:
    merged_x0 = min(entry.x0, number.x0)
    merged_x1 = max(entry.x1, number.x1)
    merged_top = min(entry.top, number.top)
    merged_bottom = max(entry.bottom, number.bottom)
    links = tuple(
        dict.fromkeys(
            sorted(
                (*entry.links, *number.links),
                key=lambda link: (link.x0, link.top, link.x1, link.bottom, link.target),
            )
        )
    )
    targets = {link.target for link in links}
    if len(targets) == 1:
        links = (
            _PdfLink(
                target=next(iter(targets)),
                x0=merged_x0,
                x1=merged_x1,
                top=merged_top,
                bottom=merged_bottom,
            ),
        )
    return replace(
        entry,
        text=f"{entry.text.rstrip()} {number.text.strip()}",
        chars=tuple(sorted((*entry.chars, *number.chars), key=lambda char: (char.x0, char.top))),
        x0=merged_x0,
        x1=merged_x1,
        top=merged_top,
        bottom=merged_bottom,
        links=links,
        soft_hyphen_end=False,
        hard_hyphen_end=False,
    )


def _native_toc_heading_references(
    pages: list[_PdfPage],
    body_size: float,
    heading_sizes: dict[float, int],
    repeated_margins: set[str],
) -> tuple[dict[str, frozenset[str]], dict[str, frozenset[str]]]:
    """Index reliable native headings without using TOC text as its own donor."""

    exact: defaultdict[str, set[str]] = defaultdict(set)
    romans: defaultdict[str, set[str]] = defaultdict(set)
    for page in pages:
        if _is_toc_page(list(page.lines)):
            continue
        previous: _PdfLine | None = None
        for line in page.lines:
            if line.rotated or _omit_margin_line(
                line,
                repeated_margins,
                previous,
                body_size,
            ):
                continue
            gap_before = line.top - previous.bottom if previous is not None else body_size * 2
            text = _display_heading_text(line).strip()
            heading = (
                _heading_level(
                    line,
                    body_size,
                    heading_sizes,
                    gap_before,
                    False,
                )
                is not None
            )
            table_caption = bool(
                re.match(r"^(?:table|tabla|cuadro)\s+\d+\s*[.\-:]", text, re.IGNORECASE)
            )
            if heading or table_caption:
                _prefix, label = _split_toc_reference_label(text)
                key = _toc_reference_key(label)
                if len(key) >= 8:
                    exact[key].add(label)
                roman_match = _ROMAN_HEADING_REFERENCE_PATTERN.match(label)
                if roman_match is not None:
                    prefix_key = _toc_reference_key(roman_match.group("prefix"))
                    if len(prefix_key) >= 3:
                        romans[prefix_key].add(roman_match.group("roman"))
            previous = line
    return (
        {key: frozenset(values) for key, values in exact.items()},
        {key: frozenset(values) for key, values in romans.items()},
    )


def _repair_toc_entries_from_native_headings(
    lines: list[_PdfLine],
    exact_references: dict[str, frozenset[str]],
    roman_references: dict[str, frozenset[str]],
) -> list[_PdfLine]:
    """Repair only spacing/case or 1/I glyphs confirmed by body headings."""

    repaired: list[_PdfLine] = []
    for line in lines:
        entry_parts = _split_toc_entry_text(line.text)
        if entry_parts is None:
            repaired.append(line)
            continue
        entry, folio = entry_parts
        enumeration, label = _split_toc_reference_label(entry)
        candidates = exact_references.get(_toc_reference_key(label), frozenset())
        if len(candidates) == 1:
            label = next(iter(candidates))

        ordinal = re.match(r"^(?P<prefix>.+[^\W\d_])\s+(?P<ones>1{1,3})$", label)
        if ordinal is not None:
            roman = "I" * len(ordinal.group("ones"))
            prefix = ordinal.group("prefix")
            if roman in roman_references.get(_toc_reference_key(prefix), frozenset()):
                label = f"{prefix} {roman}"

        separator = " ........ " if _TOC_DOTTED_FOLIO_PATTERN.fullmatch(line.text.strip()) else " "
        text = f"{enumeration}{label}{separator}{folio}"
        repaired.append(replace(line, text=text) if text != line.text else line)
    return repaired


def _split_toc_reference_label(text: str) -> tuple[str, str]:
    match = _TOC_REFERENCE_PREFIX_PATTERN.match(text.strip())
    if match is None:
        return "", text.strip()
    return match.group("prefix"), match.group("label").strip()


def _toc_reference_key(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character) and character.isalnum()
    )


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


def _is_italic_font(font_name: str) -> bool:
    normalized = font_name.casefold()
    return any(marker in normalized for marker in ("italic", "oblique", "slanted"))


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
    del page_count
    pages_by_line: defaultdict[str, set[int]] = defaultdict(set)
    for line in lines:
        if line.top <= line.page_height * 0.1 or line.bottom >= line.page_height * 0.84:
            key = _margin_key(line.text)
            if key:
                pages_by_line[key].add(line.page_number)
    return {text for text, pages in pages_by_line.items() if len(pages) >= 3}


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
            or _has_suspicious_numeric_glyph_encoding(page)
            or (
                bool(_page_letter_count(page) or page.has_images)
                and _native_page_quality(page) < _LOW_NATIVE_QUALITY_THRESHOLD
            )
        ):
            selected.add(page.number)
    return selected


def _hidden_text_audit_pages(pages: list[_PdfPage]) -> set[int]:
    """Spend a bounded OCR budget on risky scanned pages with a useful hidden text layer.

    A full-page raster plus selectable text commonly means that an earlier OCR engine created the
    PDF. The selectable layer remains authoritative, but contents pages, tables and glyph-heavy
    reference pages receive one independent full-page reading so later reconciliation has actual
    evidence instead of trusting a plausible-looking hidden typo.
    """

    ranked: list[tuple[int, int]] = []
    for page in pages:
        if not any(ratio >= _FULL_PAGE_IMAGE_AREA_RATIO for ratio in page.image_area_ratios):
            continue
        if _page_letter_count(page) < _GRAPHIC_WARNING_LETTER_LIMIT:
            continue
        lines = [line for line in page.lines if not line.rotated]
        toc_page = _is_toc_page(lines)
        trailing_numbers = sum(bool(re.search(r"\b\d{1,6}\s*$", line.text)) for line in lines)
        formula_signals = sum(bool(re.search(r"[=±×÷∑√^]|\d[$^]\d", line.text)) for line in lines)
        score = (
            (120 if toc_page else 0)
            + (90 if page.has_table else 0)
            + min(30, trailing_numbers * 2)
            + min(30, formula_signals * 5)
            + (50 if _has_suspicious_glyph_encoding(page) else 0)
            + (50 if _has_suspicious_numeric_glyph_encoding(page) else 0)
        )
        if score >= 20:
            ranked.append((score, page.number))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    budget = min(
        _MAX_HIDDEN_TEXT_AUDIT_PAGES,
        max(1, math.ceil(len(pages) * 0.25)),
    )
    return {page_number for _score, page_number in ranked[:budget]}


def _extract_secondary_native_text(
    source_path: Path,
    page_numbers: set[int],
    cancellation: CancellationToken | None,
) -> dict[int, str]:
    """Read only uncertain pages through PDFium as a second native-text implementation."""

    if not page_numbers:
        return {}
    try:
        import pypdfium2

        document = pypdfium2.PdfDocument(source_path)
    except (ImportError, OSError, RuntimeError, ValueError):
        LOGGER.warning("pdf_secondary_native_unavailable pages=%d", len(page_numbers))
        return {}
    extracted: dict[int, str] = {}
    try:
        for page_number in sorted(page_numbers):
            check_cancelled(cancellation)
            if page_number < 1 or page_number > len(document):
                continue
            page = document[page_number - 1]
            text_page = None
            try:
                text_page = page.get_textpage()
                text = text_page.get_text_bounded()
                if isinstance(text, str) and text.strip() and "\0" not in text:
                    extracted[page_number] = unicodedata.normalize("NFC", text)
            except (OSError, RuntimeError, TypeError, ValueError):
                continue
            finally:
                if text_page is not None:
                    text_page.close()
                page.close()
    finally:
        document.close()
    LOGGER.info("pdf_secondary_native_completed pages=%d", len(extracted))
    return extracted


def _reconcile_secondary_native_text(
    pages: list[_PdfPage],
    ocr_pages: dict[int, str],
    secondary_pages: dict[int, str],
) -> tuple[list[_PdfPage], int]:
    """Accept an OCR spelling only when independent PDFium text gives the same reading."""

    replacements: dict[tuple[int, float, float, str], str] = {}
    for page in pages:
        secondary = secondary_pages.get(page.number)
        ocr = ocr_pages.get(page.number)
        if secondary is None or ocr is None:
            continue
        ocr_lines = _visible_ocr_lines(ocr)
        secondary_lines = _visible_ocr_lines(secondary)
        for line in page.lines:
            if line.rotated:
                continue
            ocr_match = _best_matching_text_line(line.text, ocr_lines)
            if ocr_match is None:
                continue
            _ocr_similarity, ocr_text = ocr_match
            ocr_text = _aligned_visual_candidate(line.text, ocr_text)
            _ocr_similarity = SequenceMatcher(
                None,
                line.text.casefold(),
                ocr_text.casefold(),
                autojunk=False,
            ).ratio()
            if _visual_disagreement_priority(line.text, ocr_text, _ocr_similarity) is None:
                continue
            secondary_match = _best_matching_text_line(ocr_text, secondary_lines)
            if secondary_match is None or secondary_match[0] < 0.92:
                continue
            secondary_text = secondary_match[1]
            if _comparison_line_key(secondary_text) != _comparison_line_key(ocr_text):
                continue
            accepted = _validated_visual_reading(line.text, ocr_text, ocr_text)
            if accepted is not None and accepted != line.text:
                replacements[_visual_line_key(line)] = accepted
    if not replacements:
        return pages, 0
    reconciled = [
        replace(
            page,
            lines=tuple(
                replace(line, text=replacements[_visual_line_key(line)])
                if _visual_line_key(line) in replacements
                else line
                for line in page.lines
            ),
        )
        for page in pages
    ]
    LOGGER.info("pdf_secondary_native_arbitration_completed accepted=%d", len(replacements))
    return reconciled, len(replacements)


def _arbitrate_visual_text_disagreements(
    source_path: Path,
    pages: list[_PdfPage],
    ocr_pages: dict[int, str],
    arbiter_factory: PdfVisualArbiterFactory,
    cancellation: CancellationToken | None,
) -> tuple[list[_PdfPage], int, int]:
    disagreements = _visual_text_disagreements(pages, ocr_pages)
    if not disagreements:
        return pages, 0, 0
    arbiter = arbiter_factory()
    if arbiter is None:
        return pages, 0, 0

    selected: list[_PdfVisualDisagreement] = []
    per_page: Counter[int] = Counter()
    for disagreement in sorted(
        disagreements,
        key=lambda item: (-item.priority, item.page_number, item.line.top, item.line.x0),
    ):
        if per_page[disagreement.page_number] >= _MAX_VISUAL_ARBITRATION_REGIONS_PER_PAGE:
            continue
        selected.append(disagreement)
        per_page[disagreement.page_number] += 1
        if len(selected) >= _MAX_VISUAL_ARBITRATION_REGIONS:
            break
    if not selected:
        return pages, 0, 0

    replacements: dict[tuple[int, float, float, str], str] = {}
    reviewed = 0
    try:
        with pdfplumber.open(source_path, unicode_norm="NFC") as pdf:
            for disagreement in selected:
                check_cancelled(cancellation)
                document_page = pdf.pages[disagreement.page_number - 1]
                try:
                    crop = _render_visual_text_crop(document_page, disagreement.line)
                    proposed = arbiter(
                        crop,
                        disagreement.line.text,
                        disagreement.ocr_text,
                        cancellation,
                    )
                    reviewed += 1
                except LocalModelUnavailableError:
                    LOGGER.warning("pdf_visual_arbiter_unavailable reviewed=%d", reviewed)
                    break
                except (ImprovementError, OSError, ValueError):
                    LOGGER.warning("pdf_visual_region_skipped reviewed=%d", reviewed)
                    continue
                finally:
                    document_page.close()
                if proposed is None:
                    continue
                accepted = _validated_visual_reading(
                    disagreement.line.text,
                    disagreement.ocr_text,
                    proposed,
                )
                if accepted is None or accepted == disagreement.line.text:
                    continue
                replacements[_visual_line_key(disagreement.line)] = accepted
    except (MalformedPDFException, PdfminerException, OSError, ValueError):
        LOGGER.warning("pdf_visual_arbiter_render_failed reviewed=%d", reviewed)
        return pages, reviewed, 0

    if not replacements:
        return pages, reviewed, 0
    reconciled: list[_PdfPage] = []
    for page in pages:
        page_lines = tuple(
            replace(line, text=replacements[_visual_line_key(line)])
            if _visual_line_key(line) in replacements
            else line
            for line in page.lines
        )
        reconciled.append(replace(page, lines=page_lines) if page_lines != page.lines else page)
    LOGGER.info(
        "pdf_visual_arbitration_completed reviewed=%d accepted=%d",
        reviewed,
        len(replacements),
    )
    return reconciled, reviewed, len(replacements)


def _visual_text_disagreements(
    pages: list[_PdfPage],
    ocr_pages: dict[int, str],
) -> tuple[_PdfVisualDisagreement, ...]:
    disagreements: list[_PdfVisualDisagreement] = []
    for page in pages:
        ocr_markdown = ocr_pages.get(page.number)
        if ocr_markdown is None or _should_replace_with_ocr(page, ocr_markdown):
            continue
        ocr_lines = _visible_ocr_lines(ocr_markdown)
        if not ocr_lines:
            continue
        for line in page.lines:
            if line.rotated or not (3 <= len(line.text) <= 300):
                continue
            native_self_suspicion = bool(
                "\ufffd" in line.text or _SUSPICIOUS_NUMERIC_GLYPH_PATTERN.search(line.text)
            )
            match = _best_matching_text_line(line.text, ocr_lines)
            # A contents folio is often a detached native line but part of the OCR row.
            # Keep a deliberately low preliminary threshold, then validate the aligned
            # candidate with the strict same-atom guard below.
            if match is None or match[0] < 0.60:
                if not native_self_suspicion:
                    continue
                similarity, candidate = 1.0, line.text
            else:
                similarity, candidate = match
            candidate = _aligned_visual_candidate(line.text, candidate)
            similarity = SequenceMatcher(
                None,
                line.text.casefold(),
                candidate.casefold(),
                autojunk=False,
            ).ratio()
            if similarity < 0.74:
                if not native_self_suspicion:
                    continue
                similarity, candidate = 1.0, line.text
            priority = _visual_disagreement_priority(line.text, candidate, similarity)
            if priority is None and native_self_suspicion:
                similarity, candidate = 1.0, line.text
                priority = _visual_disagreement_priority(line.text, candidate, similarity)
            if priority is None:
                continue
            disagreements.append(_PdfVisualDisagreement(page.number, line, candidate, priority))
    return tuple(disagreements)


def _best_matching_text_line(
    source: str,
    candidates: tuple[str, ...],
) -> tuple[float, str] | None:
    ranked = sorted(
        (
            (
                SequenceMatcher(
                    None,
                    source.casefold(),
                    candidate.casefold(),
                    autojunk=False,
                ).ratio(),
                candidate,
            )
            for candidate in candidates
        ),
        reverse=True,
    )
    return ranked[0] if ranked else None


def _comparison_line_key(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).casefold().split())


def _aligned_visual_candidate(native: str, candidate: str) -> str:
    native_atoms = _visual_atoms(native)
    candidate_atoms = _visual_atoms(candidate)
    if (
        len(candidate_atoms) == len(native_atoms) + 1
        and candidate_atoms[-1].isdigit()
        and not any(atom.isdigit() for atom in native_atoms)
    ):
        return re.sub(r"\s+\d{1,6}\s*$", "", candidate).strip()
    return candidate


def _visible_ocr_lines(markdown: str) -> tuple[str, ...]:
    lines: list[str] = []
    for raw_line in markdown.splitlines():
        stripped = raw_line.strip()
        if not stripped or re.fullmatch(r"\|?[\s:|-]+\|?", stripped):
            continue
        from_table = "|" in stripped and stripped.startswith("|")
        if from_table:
            stripped = " ".join(
                cell.strip() for cell in stripped.strip("|").split("|") if cell.strip()
            )
        stripped = re.sub(r"^[#>*+\-]+\s*", "", stripped)
        if not from_table:
            stripped = re.sub(r"^\d+[.)]\s+", "", stripped)
        stripped = re.sub(r"[*_`]+", "", stripped)
        stripped = re.sub(r"!?\[([^\]]*)\]\((?:<[^>]+>|[^)]+)\)", r"\1", stripped)
        stripped = re.sub(r"<[^>]+>", " ", stripped)
        stripped = " ".join(stripped.split())
        if stripped:
            lines.append(stripped)
    return tuple(dict.fromkeys(lines))


def _visual_atoms(text: str) -> tuple[str, ...]:
    return tuple(_VISUAL_ATOM_PATTERN.findall(unicodedata.normalize("NFC", text)))


def _visual_disagreement_priority(
    native: str,
    ocr: str,
    similarity: float,
) -> int | None:
    native_atoms = _visual_atoms(native)
    ocr_atoms = _visual_atoms(ocr)
    if not native_atoms or len(native_atoms) != len(ocr_atoms):
        return None
    different = [
        index
        for index, (native_atom, ocr_atom) in enumerate(zip(native_atoms, ocr_atoms, strict=True))
        if native_atom != ocr_atom
    ]
    if not different:
        if native == ocr and "\ufffd" in native:
            return 125 + round(similarity * 10)
        if native == ocr and any(_is_mixed_visual_glyph(atom) for atom in native_atoms):
            return 118 + round(similarity * 10)
        return None
    if len(different) > 2:
        return None
    if any("\ufffd" in atom for atom in (*native_atoms, *ocr_atoms)):
        return 120 + round(similarity * 10)
    if any(
        character in "æÆœŒﬁﬂ"
        for index in different
        for character in native_atoms[index] + ocr_atoms[index]
    ):
        # Rare printed ligatures are a high-value visual question: text layers often
        # map them to a visually similar ASCII sequence while OCR may preserve them.
        return 115 + round(similarity * 10)
    if any(
        re.search(r"[\d$^]*[$^][\d$^]*", native_atoms[index] + ocr_atoms[index])
        for index in different
    ):
        return 110 + round(similarity * 10)
    changed_pairs = [(native_atoms[index], ocr_atoms[index]) for index in different]
    if all(
        (_is_mixed_visual_glyph(left) and (right.isalpha() or right.isdigit()))
        or (_is_mixed_visual_glyph(right) and (left.isalpha() or left.isdigit()))
        for left, right in changed_pairs
    ):
        return 116 + round(similarity * 10)
    if all(
        2 <= max(len(left), len(right)) <= 6
        and any(character.isdigit() for character in left)
        and any(character.isdigit() for character in right)
        and (left.isdigit() or right.isdigit())
        and _levenshtein_distance(left.casefold(), right.casefold()) <= 2
        for left, right in changed_pairs
    ):
        return 112 + round(similarity * 10)
    if all(
        max(len(left), len(right)) >= 5
        and _levenshtein_distance(left.casefold(), right.casefold()) <= 2
        for left, right in changed_pairs
    ):
        title_case = any(left[:1].isupper() or right[:1].isupper() for left, right in changed_pairs)
        return (70 if title_case else 50) + round(similarity * 10)
    return None


def _validated_visual_reading(native: str, ocr: str, proposed: str) -> str | None:
    if any(character in proposed for character in "\r\n\0") or len(proposed) > 300:
        return None
    native_atoms = _visual_atoms(native)
    ocr_atoms = _visual_atoms(ocr)
    proposed_atoms = _visual_atoms(proposed)
    if (
        not native_atoms
        or len(native_atoms) != len(ocr_atoms)
        or len(native_atoms) != len(proposed_atoms)
    ):
        return None
    disagreement = False
    for native_atom, ocr_atom, proposed_atom in zip(
        native_atoms,
        ocr_atoms,
        proposed_atoms,
        strict=True,
    ):
        if native_atom == ocr_atom:
            if proposed_atom != native_atom:
                replacement_resolution = (
                    "\ufffd" in native_atom
                    and _levenshtein_distance(native_atom, proposed_atom) <= 2
                )
                if not replacement_resolution and not _plausible_mixed_visual_resolution(
                    native_atom,
                    proposed_atom,
                ):
                    return None
                disagreement = True
            continue
        disagreement = True
        numeric = any(character.isdigit() for character in native_atom + ocr_atom)
        if numeric:
            if proposed_atom not in {
                native_atom,
                ocr_atom,
            } and not _plausible_mixed_visual_resolution(native_atom, proposed_atom):
                return None
            continue
        if (
            min(
                _levenshtein_distance(proposed_atom.casefold(), native_atom.casefold()),
                _levenshtein_distance(proposed_atom.casefold(), ocr_atom.casefold()),
            )
            > 2
        ):
            return None
        if (
            proposed_atom not in {native_atom, ocr_atom}
            and "\ufffd" not in native_atom + ocr_atom
            and not any(character in proposed_atom for character in "æÆœŒﬁﬂ")
        ):
            return None
    if not disagreement:
        return None
    if SequenceMatcher(None, native.casefold(), proposed.casefold(), autojunk=False).ratio() < 0.80:
        return None
    return proposed


def _is_mixed_visual_glyph(atom: str) -> bool:
    if re.fullmatch(r"\d+(?:st|nd|rd|th)", atom, re.IGNORECASE):
        return False
    return (
        2 <= len(atom) <= 7
        and any(character.isdigit() for character in atom)
        and any(character.isalpha() for character in atom)
    )


def _plausible_mixed_visual_resolution(source: str, proposed: str) -> bool:
    if not _is_mixed_visual_glyph(source) or not (proposed.isalpha() or proposed.isdigit()):
        return False
    return (
        abs(len(source) - len(proposed)) <= 1
        and _levenshtein_distance(
            source.casefold(),
            proposed.casefold(),
        )
        <= 3
    )


def _render_visual_text_crop(page: Any, line: _PdfLine) -> bytes:
    page_x0, page_top, page_x1, page_bottom = (float(value) for value in page.bbox)
    line_height = max(line.bottom - line.top, 2.0)
    bbox = (
        max(page_x0, line.x0 - max(8.0, line_height * 1.4)),
        max(page_top, line.top - max(4.0, line_height * 0.8)),
        min(page_x1, line.x1 + max(8.0, line_height * 1.4)),
        min(page_bottom, line.bottom + max(4.0, line_height * 0.8)),
    )
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        raise ValueError("invalid visual crop")
    image = page.crop(bbox, strict=True).to_image(resolution=288, antialias=True).original
    image = image.convert("RGB")
    if image.width * image.height > 4_000_000:
        image.thumbnail((2_800, 1_400))
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    content = output.getvalue()
    if len(content) > _MAX_VISUAL_CROP_BYTES:
        output = BytesIO()
        image.save(output, format="JPEG", quality=88, optimize=True, progressive=True)
        content = output.getvalue()
    if len(content) > _MAX_VISUAL_CROP_BYTES:
        raise ValueError("visual crop too large")
    return content


def _visual_line_key(line: _PdfLine) -> tuple[int, float, float, str]:
    return (line.page_number, line.top, line.x0, line.text)


def _levenshtein_distance(left: str, right: str) -> int:
    if left == right:
        return 0
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for left_index, left_character in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_character in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_character != right_character),
                )
            )
        previous = current
    return previous[-1]


def _has_spatial_table_candidate(lines: tuple[_PdfLine, ...]) -> bool:
    """Cheaply preselect pages whose text may form a table without vector rules."""

    useful = tuple(
        line
        for line in lines
        if not line.rotated
        and line.x1 - line.x0 >= 2
        and any(not character.isspace() for character in line.text)
    )
    if len(useful) < 8:
        return False
    page_width = max((line.page_width for line in useful), default=0)
    if page_width <= 0:
        return False
    side_by_side = _side_by_side_line_indexes(useful, page_width)
    return len(side_by_side) >= 6 and len(side_by_side) / len(useful) >= 0.20


def _side_by_side_line_indexes(
    lines: tuple[_PdfLine, ...],
    page_width: float,
) -> set[int]:
    """Return lines participating in repeated horizontal cell relationships."""

    side_by_side: set[int] = set()
    minimum_gutter = page_width * 0.025
    for left_index, left in enumerate(lines):
        for right_index in range(left_index + 1, len(lines)):
            right = lines[right_index]
            vertical_overlap = min(left.bottom, right.bottom) - max(left.top, right.top)
            minimum_height = max(1.0, min(left.bottom - left.top, right.bottom - right.top))
            horizontally_separated = (
                left.x1 + minimum_gutter <= right.x0 or right.x1 + minimum_gutter <= left.x0
            )
            if vertical_overlap >= minimum_height * 0.35 and horizontally_separated:
                side_by_side.update((left_index, right_index))
    return side_by_side


def _extract_spatial_tables(page: Any, lines: tuple[_PdfLine, ...]) -> tuple[_PdfTable, ...]:
    """Recover a raster-ruled table only when every visible character is conserved."""

    useful = tuple(
        line
        for line in lines
        if not line.rotated
        and line.x1 - line.x0 >= 2
        and any(not character.isspace() for character in line.text)
    )
    if len(useful) < 8:
        return ()
    page_width = max((line.page_width for line in useful), default=0)
    page_height = max((line.page_height for line in useful), default=0)
    if page_width <= 0 or page_height <= 0:
        return ()
    rule_groups = _spatial_table_rule_groups(_raster_horizontal_rules(page), useful)
    tables: list[_PdfTable] = []
    previous_bottom = 0.0
    for original_rules in rule_groups:
        rules = original_rules
        region_top = max(previous_bottom, rules[0].top - page_height * 0.10)
        region_bottom = rules[-1].top + 1.0
        region_lines = tuple(
            line for line in useful if region_top <= (line.top + line.bottom) / 2 <= region_bottom
        )
        previous_bottom = rules[-1].top
        has_caption = any(
            _TABLE_CAPTION_LINE_PATTERN.match(line.text.strip()) for line in region_lines
        )
        if len(rules) == 2 and has_caption:
            completed_rules = _complete_sparse_table_rules(rules, region_lines, page_width)
            if completed_rules is not None:
                rules = completed_rules
        if len(rules) < 3:
            continue
        side_by_side = _side_by_side_line_indexes(region_lines, page_width)
        minimum_side_by_side_lines = 4 if has_caption else 6
        if len(side_by_side) < minimum_side_by_side_lines:
            continue
        boundaries = _spatial_table_boundaries(
            region_lines,
            side_by_side,
            page_width,
            page_height,
            rules,
            allow_singleton_columns=has_caption and len(rules) <= 3,
        )
        if boundaries is None:
            continue
        vertical, horizontal = boundaries
        try:
            found = page.find_tables(
                {
                    "vertical_strategy": "explicit",
                    "horizontal_strategy": "explicit",
                    "explicit_vertical_lines": vertical,
                    "explicit_horizontal_lines": horizontal,
                    "snap_tolerance": 2,
                    "join_tolerance": 2,
                    "intersection_tolerance": 5,
                    "text_tolerance": 3,
                }
            )
        except (PdfminerException, TypeError, ValueError):
            continue
        if len(found) != 1:
            continue
        table = found[0]
        data = table.extract() or []
        rows = tuple(tuple(_normalize_table_cell(cell) for cell in row) for row in data)
        column_count = max((len(row) for row in rows), default=0)
        populated = sum(bool(cell) for row in rows for cell in row)
        minimum_rows = 2 if has_caption else 4
        if (
            not minimum_rows <= len(rows) <= _MAX_PDF_TABLE_ROWS
            or not 2 <= column_count <= 8
            or populated / (len(rows) * column_count) < 0.60
            or any(len(row) != column_count for row in rows)
            or any(len(cell) > _MAX_PDF_TABLE_CELL_CHARACTERS for row in rows for cell in row)
        ):
            continue
        try:
            bbox = tuple(float(value) for value in table.bbox)
        except (TypeError, ValueError):
            continue
        if (
            len(bbox) != 4
            or not all(math.isfinite(value) for value in bbox)
            or bbox[0] < 0
            or bbox[1] < 0
            or bbox[0] >= bbox[2]
            or bbox[1] >= bbox[3]
            or bbox[2] > page_width
            or bbox[3] > page_height
            or not _table_has_exact_character_coverage(page, bbox, rows)
        ):
            continue
        model = _PdfTable((bbox[0], bbox[1], bbox[2], bbox[3]), (), _TableRendering.HTML)
        if any(line.links and _line_inside_table(line, model) for line in lines):
            continue
        tables.append(_PdfTable(model.bbox, rows, _table_rendering(rows, column_count)))
    return tuple(tables)


def _complete_sparse_table_rules(
    rules: tuple[_RasterHorizontalRule, ...],
    lines: tuple[_PdfLine, ...],
    page_width: float,
) -> tuple[_RasterHorizontalRule, ...] | None:
    """Infer missing inner rules for a short, explicitly captioned table fragment."""

    if len(rules) != 2:
        return None
    side_by_side = _side_by_side_line_indexes(lines, page_width)
    aligned = sorted((lines[index] for index in side_by_side), key=lambda line: line.x0)
    if len(aligned) < 6:
        return None
    tolerance = page_width * 0.045
    first_column = [aligned[0]]
    for line in aligned[1:]:
        if line.x0 - median(item.x0 for item in first_column) > tolerance:
            break
        first_column.append(line)
    row_lines = sorted(
        (
            line
            for line in first_column
            if rules[0].top < (line.top + line.bottom) / 2 < rules[-1].top
        ),
        key=lambda line: line.top,
    )
    starts: list[float] = []
    for line in row_lines:
        if not starts or line.top - starts[-1] > max(2.0, line.font_size * 0.45):
            starts.append(line.top)
    if not 3 <= len(starts) <= _MAX_PDF_TABLE_ROWS:
        return None
    boundaries: list[float] = []
    for current, following in zip(starts, starts[1:], strict=False):
        row_tolerance = max(
            4.0,
            median(line.font_size for line in row_lines) * 0.75,
        )
        next_top = min(
            (
                line.top
                for line in lines
                if following - row_tolerance <= line.top <= following + row_tolerance
            ),
            default=following,
        )
        previous_bottom = max(
            (
                line.bottom
                for line in lines
                if line.top >= current - row_tolerance and line.top < next_top - 1.0
            ),
            default=current,
        )
        if previous_bottom >= next_top:
            return None
        boundaries.append(float((previous_bottom + next_top) / 2))
    x0 = float(median(rule.x0 for rule in rules))
    x1 = float(median(rule.x1 for rule in rules))
    return (
        rules[0],
        *(_RasterHorizontalRule(x0, top, x1) for top in boundaries),
        rules[-1],
    )


def _spatial_table_rule_groups(
    rules: tuple[_RasterHorizontalRule, ...],
    lines: tuple[_PdfLine, ...],
) -> tuple[tuple[_RasterHorizontalRule, ...], ...]:
    """Split consecutive ruled tables only at an explicit caption between their rule sets."""

    if not rules:
        return ()
    split_after: set[int] = set()
    for index, (upper, lower) in enumerate(zip(rules, rules[1:], strict=False)):
        if index + 1 < 3 or len(rules) - index - 1 < 2:
            continue
        if any(
            upper.top < (line.top + line.bottom) / 2 < lower.top
            and _TABLE_CAPTION_LINE_PATTERN.match(line.text.strip())
            for line in lines
        ):
            split_after.add(index)
    groups: list[tuple[_RasterHorizontalRule, ...]] = []
    start = 0
    for index in sorted(split_after):
        groups.append(rules[start : index + 1])
        start = index + 1
    groups.append(rules[start:])
    return tuple(groups)


def _raster_horizontal_rules(page: Any) -> tuple[_RasterHorizontalRule, ...]:
    """Locate long horizontal rules that exist only in the rendered page image."""

    try:
        image = page.to_image(resolution=144, antialias=True).original.convert("L")
    except (OSError, TypeError, ValueError):
        return ()
    width, height = image.size
    if width <= 0 or height <= 0:
        return ()
    data = image.tobytes()
    grouped: list[list[tuple[int, tuple[int, int]]]] = []
    current: list[tuple[int, tuple[int, int]]] = []
    for y in range(height + 1):
        span = _long_dark_horizontal_span(data, width, height, y) if y < height else None
        if span is not None and y / height < 0.97:
            current.append((y, span))
        elif current:
            grouped.append(current)
            current = []
    scale_x = float(page.width) / width
    scale_y = float(page.height) / height
    return tuple(
        _RasterHorizontalRule(
            float(median(span[0] for _y, span in group)) * scale_x,
            float(median(y for y, _span in group)) * scale_y,
            float(median(span[1] for _y, span in group)) * scale_x,
        )
        for group in grouped
    )


def _long_dark_horizontal_span(
    data: bytes,
    width: int,
    height: int,
    y: int,
) -> tuple[int, int] | None:
    dark_x = bytearray(width)
    for scan_y in range(max(0, y - 2), min(height, y + 3)):
        row = data[scan_y * width : (scan_y + 1) * width]
        for x, value in enumerate(row):
            if value < 200:
                dark_x[x] = 1
    best: tuple[int, int] | None = None
    start: int | None = None
    previous = -1
    gap = 0
    for x, is_dark in enumerate(dark_x):
        if is_dark:
            if start is None or gap > 3:
                if start is not None and (best is None or previous + 1 - start > best[1] - best[0]):
                    best = (start, previous + 1)
                start = x
            previous = x
            gap = 0
        elif start is not None:
            gap += 1
    if start is not None and (best is None or previous + 1 - start > best[1] - best[0]):
        best = (start, previous + 1)
    if best is None or best[1] - best[0] < width * 0.70:
        return None
    return best


def _spatial_table_boundaries(
    lines: tuple[_PdfLine, ...],
    side_by_side: set[int],
    page_width: float,
    page_height: float,
    rules: tuple[_RasterHorizontalRule, ...],
    *,
    allow_singleton_columns: bool = False,
) -> tuple[tuple[float, ...], tuple[float, ...]] | None:
    if len(rules) < 3:
        return None
    tolerance = page_width * 0.045
    aligned = sorted((lines[index] for index in side_by_side), key=lambda line: line.x0)
    clusters: list[list[_PdfLine]] = []
    for line in aligned:
        if not clusters or line.x0 - median(item.x0 for item in clusters[-1]) > tolerance:
            clusters.append([line])
        else:
            clusters[-1].append(line)
    clusters = [cluster for cluster in clusters if len(cluster) >= 2 or allow_singleton_columns]
    if not 2 <= len(clusters) <= 8:
        return None
    anchors = tuple(float(median(line.x0 for line in cluster)) for cluster in clusters)
    internal_boundaries: list[float] = []
    minimum_gutter = 0.01
    for left_cluster, right_cluster in zip(clusters, clusters[1:], strict=False):
        right_edge = min(line.x0 for line in right_cluster)
        character_edges = [
            character.x1
            for line in aligned
            for character in line.chars
            if character.x0 < right_edge and character.x1 <= right_edge
        ]
        left_edge = (
            max(character_edges) if character_edges else max(line.x1 for line in left_cluster)
        )
        if right_edge - left_edge < minimum_gutter:
            return None
        internal_boundaries.append(float((left_edge + right_edge) / 2))
    table_row_lines = tuple(
        line for line in lines if rules[0].top < (line.top + line.bottom) / 2 < rules[-1].top
    )
    outer_left = min(
        float(median(rule.x0 for rule in rules)),
        min((line.x0 for line in table_row_lines), default=page_width),
    )
    outer_right = max(
        float(median(rule.x1 for rule in rules)),
        max((line.x1 for line in table_row_lines), default=0.0),
    )
    vertical = (
        outer_left,
        *internal_boundaries,
        outer_right,
    )
    if any(left >= right for left, right in zip(vertical, vertical[1:], strict=False)):
        return None
    horizontal = [rule.top for rule in rules]
    first = horizontal[0]
    near_above = tuple(
        line
        for line in lines
        if not line.rotated
        and line.bottom <= first + 1
        and line.top >= max(0.0, first - page_height * 0.10)
        and _LETTER_PATTERN.search(line.text)
    )
    aligned_columns = {
        min(range(len(anchors)), key=lambda index: abs(line.x0 - anchors[index]))
        for line in near_above
        if min(abs(line.x0 - anchor) for anchor in anchors) <= page_width * 0.025
    }
    if len(_side_by_side_line_indexes(near_above, page_width)) >= 2 and len(aligned_columns) >= 2:
        horizontal.insert(0, min(line.top for line in near_above))
    if len(horizontal) < 3 or any(
        top >= bottom for top, bottom in zip(horizontal, horizontal[1:], strict=False)
    ):
        return None
    return vertical, tuple(horizontal)


def _table_has_exact_character_coverage(
    page: Any,
    bbox: tuple[float, float, float, float],
    rows: tuple[tuple[str, ...], ...],
) -> bool:
    x0, top, x1, bottom = bbox
    source = "".join(
        str(character.get("text", ""))
        for character in page.chars
        if x0 <= (float(character["x0"]) + float(character["x1"])) / 2 < x1
        and top <= (float(character["top"]) + float(character["bottom"])) / 2 < bottom
    )
    extracted = "".join(cell for row in rows for cell in row)
    return _significant_character_counts(source) == _significant_character_counts(extracted)


def _significant_character_counts(value: str) -> Counter[str]:
    return Counter(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if unicodedata.category(character)[0] in {"L", "N", "P", "S"}
    )


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


def _repair_repeated_front_matter_ocr_titles(
    pages: list[_PdfPage],
    ocr_pages: dict[int, str],
    body_size: float,
) -> dict[int, str]:
    """Trust one unique, prominent native title over a short OCR insertion."""

    rendered = dict(ocr_pages)
    if not rendered or not pages:
        return rendered

    reliable_native_tokens: set[str] = set()
    references: list[tuple[int, str, tuple[str, ...], float, float]] = []
    for page in pages:
        page_quality = _native_page_quality(page)
        if page_quality >= 0.75:
            for line in page.lines:
                if not line.rotated:
                    reliable_native_tokens.update(_title_word_tokens(line.text))
        if (
            not 2 <= page.number <= _OCR_TITLE_REFERENCE_LAST_PAGE
            or page_quality < 0.75
            or _is_toc_page(list(page.lines))
        ):
            continue
        for line in page.lines:
            reference_text = _normalize_text(line.text)
            reference_tokens = _title_word_tokens(reference_text)
            if not _is_reliable_native_title_line(
                page,
                line,
                reference_text,
                reference_tokens,
                body_size,
            ):
                continue
            references.append(
                (
                    page.number,
                    reference_text,
                    reference_tokens,
                    page_quality,
                    line.font_size / max(body_size, 0.01),
                )
            )
    if not references:
        return rendered

    pages_by_number = {page.number: page for page in pages}
    for page_number, markdown in ocr_pages.items():
        target_page = pages_by_number.get(page_number)
        if (
            target_page is None
            or page_number > _OCR_TITLE_TARGET_LAST_PAGE
            or not _should_replace_with_ocr(target_page, markdown)
            or not (
                _page_letter_count(target_page) < _GRAPHIC_WARNING_LETTER_LIMIT
                or _native_page_quality(target_page) < _LOW_NATIVE_QUALITY_THRESHOLD
                or max(target_page.image_area_ratios, default=0.0) >= _FULL_PAGE_IMAGE_AREA_RATIO
            )
        ):
            continue

        lines = markdown.splitlines(keepends=True)
        candidate_indices = [index for index, line in enumerate(lines) if line.strip()][:4]
        for index in candidate_indices:
            parts = _markdown_title_line_parts(lines[index])
            if parts is None:
                continue
            prefix, target_text, suffix = parts
            target_tokens = _title_word_tokens(target_text)
            if not 5 <= len(target_tokens) <= 20 or not 20 <= len(target_text) <= 160:
                continue

            candidates: list[tuple[tuple[str, ...], str, float, float]] = []
            for reference_page, reference_text, reference_tokens, quality, prominence in references:
                if reference_page == page_number:
                    continue
                target_counts = Counter(target_tokens)
                reference_counts = Counter(reference_tokens)
                extras = tuple((target_counts - reference_counts).elements())
                missing = tuple((reference_counts - target_counts).elements())
                if not 1 <= len(extras) <= 2 or len(missing) > 2:
                    continue
                substantive_extras = [
                    token for token in extras if token not in _OCR_TITLE_CONNECTORS
                ]
                if (
                    len(substantive_extras) != 1
                    or len(substantive_extras[0]) > 5
                    or re.fullmatch(r"[ivxlcdm]+", substantive_extras[0], re.IGNORECASE)
                    or substantive_extras[0] in reliable_native_tokens
                    or any(token not in _OCR_TITLE_CONNECTORS for token in missing)
                ):
                    continue
                if _content_number_tokens(target_text) != _content_number_tokens(reference_text):
                    continue
                target_unique = set(target_tokens)
                reference_unique = set(reference_tokens)
                unique_coverage = len(target_unique & reference_unique) / max(
                    1, len(target_unique | reference_unique)
                )
                sequence_similarity = SequenceMatcher(
                    None,
                    reference_tokens,
                    target_tokens,
                    autojunk=False,
                ).ratio()
                if unique_coverage < 0.85 or sequence_similarity < 0.87:
                    continue
                candidates.append((reference_tokens, reference_text, quality, prominence))
            candidate_groups = {candidate[0] for candidate in candidates}
            if len(candidate_groups) != 1:
                continue
            chosen = max(candidates, key=lambda candidate: (candidate[2], candidate[3]))
            lines[index] = f"{prefix}{chosen[1]}{suffix}"
            rendered[page_number] = "".join(lines)
            break
    return rendered


def _is_reliable_native_title_line(
    page: _PdfPage,
    line: _PdfLine,
    text: str,
    tokens: tuple[str, ...],
    body_size: float,
) -> bool:
    if (
        line.rotated
        or line.links
        or line.top <= line.page_height * 0.10
        or line.bottom >= line.page_height * 0.84
        or any(_line_inside_table(line, table) for table in page.tables)
        or not 5 <= len(tokens) <= 18
        or not 20 <= len(text) <= 160
        or _text_quality_score(text) < 0.75
        or _suspicious_glyph_count(text)
        or "\ufffd" in text
        or any(
            ord(character) not in {0x9, 0xA, 0xD}
            and (ord(character) < 0x20 or 0xD800 <= ord(character) <= 0xDFFF)
            for character in text
        )
    ):
        return False
    size_ratio = line.font_size / max(body_size, 0.01)
    return (
        size_ratio >= 1.45
        or (line.centered and size_ratio >= 0.95)
        or (line.bold and size_ratio >= 1.05)
    )


def _title_word_tokens(text: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKD", _normalize_text(text).casefold())
    without_accents = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    return tuple(re.findall(r"[^\W\d_]+", without_accents, flags=re.UNICODE))


def _markdown_title_line_parts(line: str) -> tuple[str, str, str] | None:
    ending_match = re.search(r"(?:\r\n|\r|\n)$", line)
    ending = ending_match.group(0) if ending_match is not None else ""
    content = line[: -len(ending)] if ending else line
    stripped = content.strip()
    if not stripped or any(marker in stripped for marker in ("|", "![", "<", ">")):
        return None

    start = content.index(stripped)
    end = start + len(stripped)
    prefix = content[:start]
    suffix = f"{content[end:]}{ending}"
    visible = stripped
    heading = re.match(r"#{1,6}[ \t]+", visible)
    if heading is not None:
        prefix += heading.group(0)
        visible = visible[heading.end() :]
    for marker in ("***", "___", "**", "__", "*", "_", "`"):
        if (
            visible.startswith(marker)
            and visible.endswith(marker)
            and len(visible) > len(marker) * 2
        ):
            prefix += marker
            suffix = f"{marker}{suffix}"
            visible = visible[len(marker) : -len(marker)].strip()
            break
    if not visible or any(marker in visible for marker in ("[", "]", "`")):
        return None
    return prefix, visible, suffix


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
    toc_heading_references, toc_roman_references = _native_toc_heading_references(
        pages,
        body_size,
        heading_sizes,
        repeated_margins,
    )
    publication_years = _publication_year_evidence(pages, ocr_pages)

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
            if _is_toc_page(list(page.lines)):
                ocr_markdown = _repair_toc_ocr_numeric_glyphs(
                    page,
                    ocr_markdown,
                )
        if _fragmented_graphic_text_should_stay_in_image(page, ocr_markdown):
            if page.number in referenced_pages:
                blocks.append(
                    _MarkdownBlock(
                        kind="raw",
                        text=f'<a id="page-{page.number}"></a>',
                        page_number=page.number,
                    )
                )
            _append_page_images(blocks, page.number, page_images)
            LOGGER.info(
                "pdf_fragmented_graphic_text_preserved_as_image page=%d native_letters=%d "
                "ocr_letters=%d",
                page.number,
                _page_letter_count(page),
                _heading_letter_count(ocr_markdown or ""),
            )
            previous_body_line = None
            if on_progress is not None:
                on_progress(PdfProgressPhase.STRUCTURING, current, total_pages)
            continue
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

        candidate_lines: list[_PdfLine] = []
        skipped_rotated = False
        for line in page.lines:
            if line.rotated:
                skipped_rotated = True
                continue
            candidate_lines.append(line)

        candidate_lines = _reconcile_spaced_numeric_year(
            candidate_lines,
            ocr_markdown,
            publication_years,
        )
        toc_page = _is_toc_page(candidate_lines)
        if toc_page:
            candidate_lines = _reconcile_toc_numbers_with_native_priority(
                candidate_lines,
                ocr_markdown,
            )
        if toc_page and not page.has_table:
            candidate_lines = _normalize_toc_entry_rows(candidate_lines)
            candidate_lines = _reconcile_toc_spacing_from_ocr(
                candidate_lines,
                ocr_markdown,
            )
            candidate_lines = _repair_toc_entries_from_native_headings(
                candidate_lines,
                toc_heading_references,
                toc_roman_references,
            )
        if toc_page:
            # Spacing recovery may borrow a complete OCR row. Reassert the
            # already-proven native folio sequence after that textual merge.
            candidate_lines = _reconcile_toc_numbers_with_native_priority(
                candidate_lines,
                None,
            )

        visible_lines: list[_PdfLine] = []
        previous_margin_candidate: _PdfLine | None = None
        for line in candidate_lines:
            if _omit_margin_line(
                line,
                repeated_margins,
                previous_margin_candidate,
                body_size,
                toc_page=toc_page,
            ):
                continue
            visible_lines.append(line)
            previous_margin_candidate = line

        visible_lines = _merge_drop_caps(visible_lines, body_size)
        visible_lines, skipped_vertical = _remove_vertical_stacks(visible_lines, body_size)
        visible_lines, skipped_noise = _remove_decorative_noise(visible_lines, body_size)
        visible_lines = [
            line
            for line in visible_lines
            if not any(_line_inside_table(line, table) for table in page.tables)
        ]
        if toc_page:
            # Contents pages frequently encode all-caps labels without explicit
            # spaces even though the glyph geometry still contains clear word
            # gaps. Apply the same conservative reconstruction already used for
            # headings before classifying and rendering TOC rows.
            visible_lines = [
                replace(line, text=_display_heading_text(line), chars=()) for line in visible_lines
            ]
            visible_lines = _reconcile_toc_numbers_with_native_priority(
                visible_lines,
                None,
            )
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

        previous_in_page: _PdfLine | None = None
        pending_tables = list(sorted(page.tables, key=lambda table: table.bbox[1]))
        toc_entry_lines = [
            line for line in visible_lines if _split_toc_entry_text(line.text) is not None
        ]
        toc_left = min((line.x0 for line in toc_entry_lines), default=0.0)
        for line in visible_lines:
            while pending_tables and pending_tables[0].bbox[1] <= line.top:
                _append_pdf_table(blocks, page.number, pending_tables.pop(0))
                previous_body_line = None
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
                item_text = _apply_source_emphasis(line, item_text)
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
                entry = _split_toc_entry_text(line.text)
                if entry is not None:
                    label, folio = entry
                    if line.links:
                        label = _apply_links(replace(line, text=label))
                    blocks.append(
                        _MarkdownBlock(
                            kind="toc-entry",
                            text=label,
                            page_number=page.number,
                            source_line=line,
                            toc_folio=folio,
                            toc_level=_toc_indent_level(line, toc_left, body_size),
                        )
                    )
                else:
                    rendered = _apply_source_emphasis(line, _apply_links(line))
                    indent_level = _toc_indent_level(line, toc_left, body_size)
                    previous_block = blocks[-1] if blocks else None
                    if (
                        previous_block is not None
                        and previous_block.kind == "toc-entry"
                        and previous_block.page_number == page.number
                        and (line.italic or indent_level > previous_block.toc_level)
                    ):
                        blocks.append(
                            _MarkdownBlock(
                                kind="toc-entry",
                                text=_apply_links(line),
                                page_number=page.number,
                                source_line=line,
                                toc_folio=None,
                                toc_level=max(1, indent_level),
                            )
                        )
                    else:
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
                rendered = _apply_source_emphasis(line, _apply_links(line))
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

        for table in pending_tables:
            _append_pdf_table(blocks, page.number, table)
            previous_body_line = None

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

    normalized_blocks = _conservative_container_hierarchy(
        _join_hyphenated_block_continuations(blocks),
    )
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


def _conservative_container_hierarchy(blocks: list[_MarkdownBlock]) -> list[_MarkdownBlock]:
    """Nest explicit chapter labels only when a container has two siblings."""

    normalized = [replace(block) for block in blocks]
    containers = [
        index
        for index, block in enumerate(normalized)
        if block.kind == "heading" and _CONTAINER_HEADING_PATTERN.match(block.text.strip())
    ]
    if not containers:
        return normalized
    boundaries = [*containers, len(normalized)]
    for start, end in zip(boundaries[:-1], boundaries[1:], strict=True):
        chapter_positions = [
            index
            for index in range(start + 1, end)
            if normalized[index].kind == "heading"
            and _CHAPTER_HEADING_PATTERN.match(normalized[index].text.strip())
        ]
        if len(chapter_positions) >= 2:
            normalized[start].level = 2
            for index in chapter_positions:
                normalized[index].level = 3
    return normalized


def _line_inside_table(line: _PdfLine, table: _PdfTable) -> bool:
    x0, top, x1, bottom = table.bbox
    center_x = (line.x0 + line.x1) / 2
    center_y = (line.top + line.bottom) / 2
    return x0 <= center_x <= x1 and top <= center_y <= bottom


def _append_pdf_table(
    blocks: list[_MarkdownBlock],
    page_number: int,
    table: _PdfTable,
) -> None:
    if table.rendering is _TableRendering.MARKDOWN:
        markdown = _markdown_table(table.rows)
    elif table.rendering is _TableRendering.HTML:
        markdown = _html_table(table.rows)
    else:
        markdown = _structured_table_text(table.rows)
        blocks.append(
            _MarkdownBlock(
                kind="warning",
                text=(
                    f"> **Aviso de conversión (página {page_number}):** la tabla es demasiado "
                    "compleja para representarla con seguridad. Se ha conservado como texto "
                    "estructurado; compárala con el original."
                ),
                page_number=page_number,
            )
        )
    blocks.append(_MarkdownBlock(kind="raw", text=markdown, page_number=page_number))


def _markdown_table(rows: tuple[tuple[str, ...], ...]) -> str:
    header = tuple(_render_table_cell(cell) for cell in rows[0])

    def row(cells: tuple[str, ...]) -> str:
        return (
            "| " + " | ".join(_render_table_cell(cell).replace("|", "\\|") for cell in cells) + " |"
        )

    return "\n".join(
        (row(header), row(tuple("---" for _ in header)), *(row(item) for item in rows[1:]))
    )


def _html_table(rows: tuple[tuple[str, ...], ...]) -> str:
    header = "".join(f"<th>{escape(_render_table_cell(cell))}</th>" for cell in rows[0])
    body = "".join(
        "<tr>"
        + "".join(
            f"<td>{escape(_render_table_cell(cell)).replace(chr(10), '<br>')}</td>" for cell in row
        )
        + "</tr>"
        for row in rows[1:]
    )
    return f"<table>\n<thead><tr>{header}</tr></thead>\n<tbody>{body}</tbody>\n</table>"


def _structured_table_text(rows: tuple[tuple[str, ...], ...]) -> str:
    headers = tuple(cell or f"Columna {index}" for index, cell in enumerate(rows[0], start=1))
    rendered = ["**Tabla recuperada**"]
    for row_number, row in enumerate(rows[1:], start=1):
        cells = "; ".join(
            f"{header}: {_render_table_cell(value)}"
            for header, value in zip(headers, row, strict=True)
            if value
        )
        rendered.append(f"- Fila {row_number}: {cells or 'sin contenido'}")
    return "\n".join(rendered)


def _should_replace_with_ocr(page: _PdfPage, ocr_markdown: str) -> bool:
    if _fragmented_graphic_text_should_stay_in_image(page, ocr_markdown):
        return False
    native_letters = _page_letter_count(page)
    ocr_letters = _heading_letter_count(ocr_markdown)
    native_quality = _native_page_quality(page)
    ocr_quality = _text_quality_score(ocr_markdown)
    if page.has_table and _MARKDOWN_TABLE_PATTERN.search(ocr_markdown):
        return ocr_quality >= max(0.42, native_quality - 0.08) and _table_ocr_is_faithful(
            page,
            ocr_markdown,
        )
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


def _fragmented_graphic_text_should_stay_in_image(
    page: _PdfPage,
    ocr_markdown: str | None,
) -> bool:
    """Prefer a full-page visual when sparse labels cannot survive text reflow.

    This deliberately requires several independent signals. A short cover title or poem remains
    text; a chart-like page with many one- or two-character fragments remains a faithful image.
    """

    if (
        page.has_table
        or not ocr_markdown
        or not any(ratio >= _FULL_PAGE_IMAGE_AREA_RATIO for ratio in page.image_area_ratios)
        or any(line.links for line in page.lines)
        or _MARKDOWN_TABLE_PATTERN.search(ocr_markdown)
    ):
        return False
    ocr_lines = _visible_ocr_lines(ocr_markdown)
    ocr_letters = _heading_letter_count(ocr_markdown)
    if len(ocr_lines) < 8 or ocr_letters >= 80:
        return False
    lexical_ocr_words = sum(
        sum(character.isalpha() for character in word) >= 3
        for line in ocr_lines
        for word in line.split()
    )
    if ocr_letters / len(ocr_lines) > 4.5 or lexical_ocr_words > 8:
        return False

    native_lines = tuple(
        line for line in page.lines if not line.rotated and _LETTER_PATTERN.search(line.text)
    )
    native_letters = sum(_heading_letter_count(line.text) for line in native_lines)
    if native_letters >= 80:
        return False
    return not native_lines or (
        len(native_lines) >= 4 and native_letters / len(native_lines) <= 6.0
    )


def _table_ocr_is_faithful(page: _PdfPage, ocr_markdown: str) -> bool:
    if page.tables:
        native_shapes = tuple(
            (len(table.rows), len(table.rows[0]))
            for table in page.tables
            if table.rows and table.rows[0]
        )
        ocr_shapes = tuple(
            (rows - 1, columns) for rows, columns in markdown_table_shapes(ocr_markdown)
        )
        if native_shapes != ocr_shapes:
            return False

    native_text = _native_page_text(page)
    native_letters = _heading_letter_count(native_text)
    ocr_letters = _heading_letter_count(ocr_markdown)
    if native_letters < _MIN_USABLE_NATIVE_LETTERS:
        return ocr_letters >= 10
    if ocr_letters < native_letters * 0.65 or ocr_letters > native_letters * 3.2:
        return False

    native_tokens = _comparison_tokens(native_text)
    ocr_tokens = _comparison_tokens(ocr_markdown)
    overlap = native_tokens & ocr_tokens
    if native_tokens and len(overlap) / len(native_tokens) < 0.60:
        return False
    if ocr_tokens and len(overlap) / len(ocr_tokens) < 0.45:
        return False
    return _content_number_tokens(native_text) == _content_number_tokens(ocr_markdown)


def _content_number_tokens(text: str) -> Counter[str]:
    visible_lines = (
        line
        for line in text.splitlines()
        if not _is_page_number(re.sub(r"[^0-9ivxlcdm]", "", line, flags=re.IGNORECASE))
    )
    return Counter(re.findall(r"\d+(?:[.,]\d+)*", "\n".join(visible_lines)))


def _has_suspicious_glyph_encoding(page: _PdfPage) -> bool:
    text = " ".join(line.text for line in page.lines if not line.rotated)
    return _suspicious_glyph_count(text) >= 3


def _has_suspicious_numeric_glyph_encoding(page: _PdfPage) -> bool:
    """Find broken font mappings inside numeric TOC tokens without distrusting prose."""

    return any(
        _SUSPICIOUS_NUMERIC_GLYPH_PATTERN.search(line.text)
        and (
            re.search(r"\d[$^]|[$^]\d", line.text)
            or any(_is_mixed_visual_glyph(atom) for atom in _visual_atoms(line.text))
        )
        for line in page.lines
        if not line.rotated
    )


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
        + len(_SUSPICIOUS_NUMERIC_GLYPH_PATTERN.findall(visible))
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
        table_values = [
            re.sub(r"^[*_`]+|[*_`]+$", "", cell.strip()).strip().casefold()
            for cell in re.split(r"(?<!\\)\|", stripped.strip("|"))
            if cell.strip()
        ]
        if len(table_values) == 1 and table_values[0] in margin_numbers:
            lines[index] = ""
            continue
        visible_number = re.sub(r"^[*_`]+|[*_`]+$", "", stripped).strip().casefold()
        for number in margin_numbers:
            if visible_number == number:
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


def _repair_toc_ocr_numeric_glyphs(page: _PdfPage, markdown: str) -> str:
    """Carry uniquely repaired native TOC folios into an OCR page replacement.

    OCR can win the whole-page quality comparison while still repeating the same
    broken font-shaped number as the selectable layer. Native neighbours provide
    stronger evidence for that one token and must survive the replacement.
    """

    native_lines = list(page.lines)
    reconciled_lines = _reconcile_toc_numbers_with_native_priority(native_lines, markdown)
    replacements: dict[str, str] = {}
    for native, reconciled in zip(native_lines, reconciled_lines, strict=True):
        if native.text == reconciled.text:
            continue
        for match in _SUSPICIOUS_NUMERIC_GLYPH_PATTERN.finditer(native.text):
            prefix = native.text[: match.start()]
            suffix = native.text[match.end() :]
            if not reconciled.text.startswith(prefix) or not reconciled.text.endswith(suffix):
                continue
            end = len(reconciled.text) - len(suffix) if suffix else len(reconciled.text)
            replacement = reconciled.text[len(prefix) : end]
            if replacement.isdecimal():
                replacements[match.group(0)] = replacement

    repaired = markdown
    for source, replacement in sorted(replacements.items(), key=lambda item: -len(item[0])):
        repaired = re.sub(
            rf"(?<!\w){re.escape(source)}(?!\w)",
            replacement,
            repaired,
        )
    return repaired


def _ocr_additions(page: _PdfPage, ocr_markdown: str) -> str:
    if _is_toc_page(list(page.lines)) and _is_toc_markdown(ocr_markdown):
        return ""
    native_text = " ".join(line.text for line in page.lines)
    native_tokens = _comparison_tokens(native_text)
    native_sequence = _comparison_token_sequence(native_text)
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
        ordered_copy = _is_ordered_ocr_copy(
            _comparison_token_sequence(stripped),
            native_sequence,
        )
        if block_tokens and (
            block_tokens.issubset(native_tokens)
            or (len(block_tokens) >= 4 and len(overlap) / len(block_tokens) >= 0.9)
            or ordered_copy
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
    return set(_comparison_token_sequence(text))


def _comparison_token_sequence(text: str) -> tuple[str, ...]:
    without_targets = re.sub(r"\]\((?:<[^>]+>|[^)]+)\)", "]", text)
    without_targets = _normalize_text(without_targets)
    return tuple(
        token.casefold() for token in re.findall(r"[^\W_]+", without_targets, flags=re.UNICODE)
    )


def _is_ordered_ocr_copy(
    ocr_tokens: tuple[str, ...],
    native_tokens: tuple[str, ...],
) -> bool:
    """Recognize a noisy OCR copy without hiding a genuinely missing sentence."""

    if len(ocr_tokens) < 12 or not native_tokens:
        return False
    matcher = SequenceMatcher(None, ocr_tokens, native_tokens, autojunk=False)
    matching_words = sum(size for _left, _right, size in matcher.get_matching_blocks())
    if matching_words / len(ocr_tokens) < 0.90:
        return False
    longest_unmatched_run = max(
        (
            ocr_end - ocr_start
            for tag, ocr_start, ocr_end, _native_start, _native_end in matcher.get_opcodes()
            if tag != "equal"
        ),
        default=0,
    )
    return longest_unmatched_run <= 4


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
            elif not _is_internal_page_target(link.target):
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
            if link.target in seen_targets or _is_internal_page_target(link.target):
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


def _is_internal_page_target(target: str) -> bool:
    return re.fullmatch(r"#page-\d+", target, re.IGNORECASE) is not None


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
    visible = _display_heading_text(line) if not line.links else _apply_links(line)
    text = f"*{visible}*" if line.italic else visible
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


def _apply_source_emphasis(line: _PdfLine, rendered: str) -> str:
    if _heading_letter_count(line.text) < 4:
        return rendered
    if line.bold and line.italic:
        return f"***{rendered}***"
    if line.bold:
        return f"**{rendered}**"
    if line.italic:
        return f"*{rendered}*"
    return rendered


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
        # Reconcile only an unmistakable paragraph continuation at a page
        # boundary. Headings, lists, tables and sentence boundaries remain
        # separate, so a bad guess cannot restructure the document broadly.
        return (
            current.text[:1].islower()
            and previous.text.rstrip()[-1:] not in ".!?;:"
            and previous.bottom >= previous.page_height * 0.70
            and current.top <= current.page_height * 0.25
            and abs(previous.font_size - current.font_size) <= body_size * 0.20
            and abs(current.x0 - previous.x0) <= current.page_width * 0.08
        )

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
    if _TOC_HEADING_PATTERN.fullmatch(text):
        return 2
    if toc_page and _toc_entry_page_number(text) is not None:
        return None
    if _SECTION_HEADING_PATTERN.match(text):
        return 2
    if level := heading_sizes.get(round(line.font_size, 1)):
        return level
    if toc_page or letter_count < 4:
        return None

    uppercase = _is_uppercase_text(text)
    separated = gap_before >= max(body_size * 0.55, 3)
    uppercase_word_count = len(re.findall(r"[^\W\d_]+", text, re.UNICODE))
    if uppercase and uppercase_word_count > _MAX_BODY_SIZE_UPPERCASE_HEADING_WORDS:
        return None
    if (
        uppercase
        and uppercase_word_count <= _MAX_BODY_SIZE_UPPERCASE_HEADING_WORDS
        and separated
        and (line.bold or line.centered)
    ):
        return 2 if line.centered else 3
    if line.bold and separated and len(text) <= 80:
        return 3
    return None


def _omit_margin_line(
    line: _PdfLine,
    repeated_margins: set[str],
    previous: _PdfLine | None,
    body_size: float,
    *,
    toc_page: bool = False,
) -> bool:
    strict_bottom_margin = line.top >= line.page_height * 0.92
    if strict_bottom_margin and _is_page_number(re.sub(r"\s+", "", line.text.strip())):
        # A TOC may legitimately contain detached folios in its body, but a lone
        # number in the physical footer band is still the page's own running folio.
        return True
    if toc_page and _toc_entry_page_number(line.text) is not None:
        return False
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
    if any(_SUSPICIOUS_NUMERIC_GLYPH_PATTERN.search(line.text) for line in visible_lines):
        return (
            f"> **Aviso de conversión (página {page.number}):** la capa de texto contiene "
            "un glifo numérico ambiguo que el OCR local no pudo confirmar. Revisa el PDF original."
        )
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
    normalized_blocks = _conservative_container_hierarchy(
        _join_hyphenated_block_continuations(blocks),
    )
    return _normalized_blocks_to_markdown(normalized_blocks)


def _normalized_blocks_to_markdown(blocks: list[_MarkdownBlock]) -> str:
    parts: list[str] = []
    previous_compact_group: str | None = None
    previous_page_number: int | None = None
    block_index = 0
    while block_index < len(blocks):
        block = blocks[block_index]
        if block.kind == "warning":
            block_index += 1
            continue
        if block.kind == "toc-entry":
            end = block_index + 1
            while end < len(blocks) and blocks[end].kind == "toc-entry":
                end += 1
            parts.append(_toc_entries_html(blocks[block_index:end]))
            previous_compact_group = None
            previous_page_number = block.page_number
            block_index = end
            continue
        if block.kind == "heading":
            rendered = f"{'#' * (block.level or 1)} {block.text}"
        else:
            rendered = block.text
        compact_group = (
            "list"
            if block.kind in {"list", "toc"}
            and re.match(r"^[ \t]{0,3}(?:[-+*]|\d+[.)])[ \t]+", rendered)
            else None
        )
        if (
            compact_group is not None
            and previous_compact_group == compact_group
            and previous_page_number == block.page_number
            and parts
        ):
            parts[-1] = f"{parts[-1]}\n{rendered}"
        else:
            parts.append(rendered)
        previous_compact_group = compact_group
        previous_page_number = block.page_number
        block_index += 1
    markdown = "\n\n".join(part for part in parts if part).strip()
    return _join_page_boundary_hyphenations(markdown)


def _toc_indent_level(line: _PdfLine, left: float, body_size: float) -> int:
    indentation = max(0.0, line.x0 - left)
    unit = max(body_size * 1.8, 10.0)
    return min(2, max(0, round(indentation / unit)))


def _toc_entries_html(blocks: list[_MarkdownBlock]) -> str:
    rows: list[str] = []
    for block in blocks:
        label = _toc_label_xhtml(block.text)
        source_line = block.source_line
        if source_line is not None and source_line.italic:
            label = f"<em>{label}</em>"
        if source_line is not None and source_line.bold:
            label = f"<strong>{label}</strong>"
        rows.append(
            "<tr>"
            f'<td class="toc-label toc-level-{block.toc_level}">{label}</td>'
            f'<td class="toc-folio">{escape(block.toc_folio or "")}</td>'
            "</tr>"
        )
    return (
        '<table class="document-toc">\n'
        '<thead><tr><th class="toc-label">Entrada</th>'
        '<th class="toc-folio">Página</th></tr></thead>\n'
        f"<tbody>{''.join(rows)}</tbody>\n"
        "</table>"
    )


def _toc_label_xhtml(markdown: str) -> str:
    link = re.fullmatch(r"\[([^\]]+)]\(<?(#page-\d{1,6})>?\)", markdown)
    if link is None:
        return escape(markdown)
    return f'<a href="{escape(link.group(2), quote=True)}">{escape(link.group(1))}</a>'


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
            previous.source_pages = tuple(
                dict.fromkeys((*previous.source_pages, *block.source_pages))
            )
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


def _is_toc_folio(text: str) -> bool:
    """Accept long-book folios without treating four-digit years as page margins."""

    return _is_page_number(text) or bool(text.isdecimal() and len(text) <= 4)


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
    # In the affected legacy encoding, ``K`` stands in for an acute accent before
    # a consonant (``PRAKCTICA`` -> ``PRÁCTICA``).  Treating it as a marker before
    # any uppercase letter corrupts ordinary English words such as ``MAKE`` and
    # ``TAKE``.  Ambiguous vowel-to-vowel cases stay native so OCR/visual review can
    # arbitrate them instead of silently deleting a real character.
    repaired = re.sub(
        r"([AEIOU])K(?=[BCDFGHJLMNPQRSTVWXYZÑ]|$)",
        accent_vowel,
        repaired,
    )
    # Some embedded fonts map a digit to ``$``.  Removing every remaining
    # dollar sign used to turn a native TOC label such as ``1$.`` into ``1.``
    # before OCR had a chance to arbitrate it.  Only discard a residue still
    # attached to a natural-language word; keep numeric/currency uses visible.
    return re.sub(r"(?<=[^\W\d_])\$", "", repaired)


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
    if any(_TOC_HEADING_PATTERN.fullmatch(line) for line in normalized_lines):
        return True
    entry_count = sum(
        bool(re.search(r"\b\d+\s*$", line.text))
        or any(link.target.startswith("#page-") for link in line.links)
        for line in lines
    )
    return entry_count >= 4 and entry_count / len(lines) >= 0.3


def _is_toc_markdown(markdown: str) -> bool:
    """Recognize OCR-rendered contents pages without retaining their full-page scan."""

    lines = [
        re.sub(r"^[#>*+\-\s|]+|[|\s]+$", "", line).strip()
        for line in markdown.splitlines()
        if line.strip() and not re.match(r"^\s*\|?\s*:?-{3,}", line)
    ]
    if not lines:
        return False
    if any(_TOC_HEADING_PATTERN.fullmatch(_margin_key(line)) for line in lines):
        return True
    entries = sum(
        bool(re.search(r"\b\d{1,4}\s*\|?\s*$", line))
        for line in markdown.splitlines()
        if line.strip()
    )
    return entries >= 4 and entries / len(lines) >= 0.3


def _toc_entry_page_number(text: str) -> str | None:
    entry = _split_toc_entry_text(text)
    return entry[1] if entry is not None else None


def _split_toc_entry_text(text: str) -> tuple[str, str] | None:
    dotted = _TOC_DOTTED_FOLIO_PATTERN.fullmatch(text.strip())
    if dotted is not None:
        candidate = re.sub(r"\s+", "", dotted.group("folio"))
        if _is_toc_folio(candidate):
            return dotted.group("label").strip(), candidate
        return None
    parts = text.strip().rsplit(maxsplit=1)
    if len(parts) != 2 or _heading_letter_count(parts[0]) < 2:
        return None
    candidate = re.sub(r"\s+", "", parts[1])
    return (parts[0], candidate) if _is_toc_folio(candidate) else None


def _normalize_toc_leaders(markdown: str) -> str:
    parts = _MARKDOWN_LINK_DESTINATION_SPLIT_PATTERN.split(markdown)
    return "".join(
        part if index % 2 else re.sub(r"\s*\.{2,}\s*", " — ", part)
        for index, part in enumerate(parts)
    )


def _escape_link_label(label: str) -> str:
    return label.replace("[", "\\[").replace("]", "\\]")


def _safe_target(target: str) -> str:
    return quote(target, safe="/:#?&=@[]!$&'()*+,;~%")
