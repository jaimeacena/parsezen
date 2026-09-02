"""Conservative local PDF extraction with structure, links, and selective OCR."""

from __future__ import annotations

import gc
import inspect
import logging
import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
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
from parsezen.epub_builder import classify_heading_role
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
    _PdfEmphasisSpan,
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
_ORDERED_LIST_PATTERN = re.compile(
    r"^(?P<number>[1-9]|1\d|20)(?P<marker>[.)])[ \t]+(?P<text>\S.*)$"
)
_ORDERED_LIST_PREFIX_PATTERN = re.compile(r"^(?:[1-9]|1\d|20)[.)][ \t]+")
_ORDERED_LIST_LABEL_PATTERN = re.compile(
    r"^(?P<label>[^\W\d_]+(?:[ \t]+[^\W\d_]+)+)[ \t]*(?P<suffix>:.*)$",
    re.UNICODE,
)
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
_PARTIAL_SPACED_WORD_PATTERN = re.compile(
    r"(?<!\w)[^\W\d_]{2,3}(?:\s+[^\W\d_]){3,}(?!\w)",
    re.UNICODE,
)
_DEGREE_SHAPED_NATIVE_ZERO_PATTERN = re.compile(r"(?<!\d)(?P<degree>[0-2]?\d)0(?=$|[\s.,;:!?)\]])")
_ZODIAC_SIGN_NAMES = frozenset(
    {
        "Acuario",
        "Acquario",
        "Aquarius",
        "Aquário",
        "Aries",
        "Ariete",
        "Áries",
        "Balance",
        "Balança",
        "Bélier",
        "Cancer",
        "Cancro",
        "Cáncer",
        "Capricorn",
        "Capricorne",
        "Capricornio",
        "Capricórnio",
        "Capricorno",
        "Escorpio",
        "Escorpião",
        "Fische",
        "Gemelli",
        "Gémeaux",
        "Géminis",
        "Gemini",
        "Geminis",
        "Gêmeos",
        "Jungfrau",
        "Krebs",
        "Leão",
        "Leo",
        "Leone",
        "Libra",
        "Lion",
        "Löwe",
        "Peixes",
        "Pesci",
        "Pisces",
        "Piscis",
        "Poissons",
        "Sagittaire",
        "Sagitario",
        "Sagitário",
        "Sagittario",
        "Sagittarius",
        "Schütze",
        "Scorpio",
        "Scorpion",
        "Scorpione",
        "Skorpion",
        "Steinbock",
        "Stier",
        "Taureau",
        "Taurus",
        "Toro",
        "Touro",
        "Tauro",
        "Verseau",
        "Vierge",
        "Virgem",
        "Virgo",
        "Waage",
        "Wassermann",
        "Widder",
        "Zwillinge",
    }
)
_ZODIAC_SIGN_PATTERN = (
    "(?:"
    + "|".join(re.escape(name) for name in sorted(_ZODIAC_SIGN_NAMES, key=len, reverse=True))
    + ")"
)
_INVALID_ZODIAC_DEGREE_PATTERN = re.compile(
    rf"(?<!\d)(?P<degree>[3-9]\d)(?P<marker>°|0)(?=\s+{_ZODIAC_SIGN_PATTERN}\b)",
    re.IGNORECASE,
)
_INVALID_ZODIAC_DEGREE_ZERO_PATTERN = re.compile(
    rf"(?<!\d)(?P<degree>[3-9]\d)0(?=\s+{_ZODIAC_SIGN_PATTERN}\b)",
    re.IGNORECASE,
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
_NUMBERED_FIGURE_CAPTION_PATTERN = re.compile(
    r"^(?:figures?|figuras?)\s*[0-9iIlLoO]{1,4}"
    r"(?:\s*(?:[,&+]|\b(?:and|y)\b|[-–—])\s*[0-9iIlLoO]{1,4})*"
    r"\s*(?:[.:)–—-]|$)",
    re.IGNORECASE,
)
_ROMAN_HEADING_REFERENCE_PATTERN = re.compile(
    r"^(?P<prefix>.+?)\s+(?P<roman>[IVXLCDM]{1,6})(?=\s*(?::|[-–—]|$))"
)
_ASTROLOGICAL_SERIES_HEADING_PATTERN = re.compile(
    r"^(?P<planet>Sun|Moon|Mercury|Venus|Mars|Jupiter|Saturn)\s+in\s+"
    r"(?P<sign>Aries|Taurus|Gemini|Cancer|Leo|Virgo|Libra|Scorpio|Sagittarius|"
    r"Capricorn|Aquarius|Pisces)\s+(?P<series>I{1,3}|n|it|ui|in)$",
    re.IGNORECASE,
)
_ASTROLOGICAL_DECAN_TITLE_PATTERN = re.compile(
    rf"^(?P<sign>{_ZODIAC_SIGN_PATTERN})\s+(?P<series>I{{1,3}})\s*[:—-]\s*\S.*$",
    re.IGNORECASE,
)
_BROKEN_ASTROLOGICAL_DECAN_TITLE_PATTERN = re.compile(
    rf"^(?P<sign>{_ZODIAC_SIGN_PATTERN})\s+(?P<series>IE)\s+(?P<subtitle>\S.*)$",
    re.IGNORECASE,
)
_FOOTNOTE_DEFINITION_PATTERN = re.compile(r"^(?P<number>\d{1,3})[ \t]+(?P<body>\S(?:.*\S)?)$")
_INLINE_FOOTNOTE_MARKER_PATTERN = re.compile(
    r"(?P<prefix>[^\s\d])(?P<number>\d{1,3})(?=(?:\s|$|[.,;:!?]))"
)
_SUPERSCRIPT_DIGITS = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
_FULL_PAGE_EXPORT_LETTER_LIMIT = 300
_MIN_COLUMN_LINES = 3
_MIN_VECTOR_CURVES = 2
_MAX_CLUSTERED_VECTOR_CURVES = 500
_VECTOR_CLUSTER_GAP = 12.0
_OPEN_RASTER_TABLE_RULE_WIDTH_RATIO = 0.58
_OPEN_RASTER_TABLE_MIN_AREA_RATIO = 0.08
_OPEN_RASTER_TABLE_MAX_AREA_RATIO = 0.75
_OPEN_RASTER_TABLE_MIN_SIDE_BY_SIDE_LINES = 8
_OPEN_RASTER_TABLE_MIN_SIDE_BY_SIDE_RATIO = 0.40
_MARKDOWN_TABLE_PATTERN = re.compile(r"^\s*\|?.*\|.*\n\s*\|?\s*:?-{3,}", re.MULTILINE)
_PDF_WARNING_PAGE_PATTERN = re.compile(
    r"^>\s*\*\*Aviso(?: OCR| de conversión) \(página (\d+)\):\*\*",
    re.MULTILINE,
)
_PDF_WARNING_MESSAGE_PATTERN = re.compile(
    r"^>\s*\*\*Aviso(?: OCR| de conversión) \(página \d+\):\*\*\s*"
)
_PDF_PAGE_MARKER_PATTERN = re.compile(r"<!--\s*PZDOC PDF PAGE \d+\s*-->", re.IGNORECASE)
_PDF_OUTLINE_MARKER_PATTERN = re.compile(
    r"<!--\s*PZDOC PDF OUTLINE [1-6]\s*-->",
    re.IGNORECASE,
)
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
_MIN_VISUAL_TEXT_FALLBACK_PRIORITY = 100
_SYSTEMIC_SECONDARY_NATIVE_REPAIR_THRESHOLD = 8
_MAX_HIDDEN_TEXT_AUDIT_PAGES = 6
_MAX_VISUAL_CROP_BYTES = 4 * 1024 * 1024
_LITERAL_URL_PATTERN = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)


def strip_pdf_page_markers(markdown: str) -> str:
    """Remove private page anchors before publishing user-visible text."""
    return _PDF_PAGE_MARKER_PATTERN.sub("", markdown)


def strip_pdf_public_markers(markdown: str) -> str:
    """Remove every private PDF marker from reader-visible Markdown."""

    return _PDF_OUTLINE_MARKER_PATTERN.sub("", strip_pdf_page_markers(markdown))


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
    document_consensus_arbitrated_regions: int = 0


@dataclass(frozen=True, slots=True)
class PdfEmbeddedResource:
    """One meaningful PDF image rendered for portable Markdown or EPUB output."""

    relative_path: PurePosixPath
    content: bytes
    media_type: str
    page_number: int
    bbox: tuple[float, float, float, float] | None = None
    visual_authority: bool = False
    visual_text_authority: bool = False


@dataclass(frozen=True, slots=True)
class _PdfOutlineEntry:
    level: int
    title: str
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

    pages, native_degree_repairs = _repair_native_degree_markers(pages)
    pages, native_symbol_repairs = _repair_overlapping_native_symbols(pages)
    pages, repeated_spacing_repairs = _repair_repeated_native_spacing(pages)
    pre_ocr_secondary_native_text = _extract_secondary_native_text(
        source_path,
        {
            page.number
            for page in pages
            if any(
                _suspicious_non_currency_dollar_count(line.text)
                for line in page.lines
                if not line.rotated
            )
        },
        cancellation,
    )
    pages, pre_ocr_secondary_repairs = _reconcile_secondary_dollar_glyphs(
        pages,
        pre_ocr_secondary_native_text,
    )
    systemic_font_mapping = pre_ocr_secondary_repairs >= _SYSTEMIC_SECONDARY_NATIVE_REPAIR_THRESHOLD
    bounded_line_keys = (
        {_visual_line_key(line) for page in pages for line in page.lines if not line.rotated}
        if systemic_font_mapping
        else None
    )
    bounded_secondary_native_text = _extract_secondary_native_line_text(
        source_path,
        pages,
        cancellation,
        line_keys=bounded_line_keys,
    )
    pages, bounded_secondary_repairs = _reconcile_secondary_dollar_glyph_lines(
        pages,
        bounded_secondary_native_text,
    )
    pre_ocr_secondary_repairs += bounded_secondary_repairs
    if systemic_font_mapping:
        pages, systemic_secondary_repairs = _reconcile_systemic_secondary_native_lines(
            pages,
            bounded_secondary_native_text,
        )
        pre_ocr_secondary_repairs += systemic_secondary_repairs
    outline_entries = _extract_pdf_outline(
        source_path,
        page_range,
        cancellation,
    )
    pages, outline_heading_repairs = _reconcile_pdf_outline_headings(
        pages,
        outline_entries,
    )
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

    body_size = _dominant_body_size(lines)
    heading_sizes = _heading_size_levels(lines, body_size)
    repeated_margins = _repeated_margin_lines(lines, len(pages))
    referenced_pages = _referenced_pages(lines)
    rendered_ocr_pages = _repair_repeated_front_matter_ocr_titles(
        pages,
        ocr_pages,
        body_size,
    )
    secondary_page_numbers = {
        page.number
        for page in pages
        if page.number in rendered_ocr_pages
        and not _should_replace_with_ocr(page, rendered_ocr_pages[page.number])
    }
    secondary_native_text = dict(pre_ocr_secondary_native_text)
    secondary_native_text.update(
        _extract_secondary_native_text(
            source_path,
            secondary_page_numbers - set(secondary_native_text),
            cancellation,
        )
    )
    pages, secondary_native_arbitrated_regions = _reconcile_secondary_native_text(
        pages,
        rendered_ocr_pages,
        secondary_native_text,
    )
    remaining_visual_disagreement_keys = {
        _visual_line_key(disagreement.line)
        for disagreement in _visual_text_disagreements(pages, rendered_ocr_pages)
    }
    bounded_secondary_disagreement_text = _extract_secondary_native_line_text(
        source_path,
        pages,
        cancellation,
        line_keys=remaining_visual_disagreement_keys,
    )
    pages, bounded_secondary_arbitrated_regions = _reconcile_secondary_native_text_lines(
        pages,
        rendered_ocr_pages,
        bounded_secondary_disagreement_text,
    )
    secondary_native_arbitrated_regions += bounded_secondary_arbitrated_regions
    secondary_native_arbitrated_regions += pre_ocr_secondary_repairs
    pages, numeric_glyph_arbitrated_regions = _reconcile_suspicious_numbers_from_ocr(
        pages,
        rendered_ocr_pages,
    )
    pages, document_consensus_arbitrated_regions = _reconcile_document_token_consensus(
        pages,
        rendered_ocr_pages,
    )
    document_consensus_arbitrated_regions += (
        numeric_glyph_arbitrated_regions
        + native_degree_repairs
        + native_symbol_repairs
        + repeated_spacing_repairs
        + outline_heading_repairs
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
    page_images: dict[int, tuple[PdfEmbeddedResource, ...]] = {}
    omitted_images = 0
    required_figure_failure_pages: set[int] = set()
    if include_images:
        page_images, omitted_images, required_figure_failure_pages = _extract_embedded_images(
            source_path,
            pages,
            rendered_ocr_pages,
            cancellation,
            page_range,
            on_progress,
        )
    structural_ocr_pages = _ocr_pages_safe_for_structural_rendering(
        pages,
        rendered_ocr_pages,
    )
    markdown, review_issues = _render_document(
        pages,
        body_size,
        heading_sizes,
        repeated_margins,
        referenced_pages,
        structural_ocr_pages,
        ocr_failed_pages,
        page_images=page_images,
        required_figure_failure_pages=required_figure_failure_pages,
        on_progress=on_progress,
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
        document_consensus_arbitrated_regions=document_consensus_arbitrated_regions,
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
    document_consensus_arbitrated_regions: int = 0,
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
            document_consensus_arbitrated_regions=document_consensus_arbitrated_regions,
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
                built_lines = _split_lines_at_established_gutters(built_lines)
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
) -> tuple[dict[int, tuple[PdfEmbeddedResource, ...]], int, set[int]]:
    """Render meaningful placed images while filtering page scans and backgrounds."""
    page_models = {page.number: page for page in pages}
    selected: dict[int, tuple[PdfEmbeddedResource, ...]] = {}
    seen_content: set[str] = set()
    omitted = 0
    exported = 0
    required_figure_failure_pages: set[int] = set()
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
                ocr_markdown = ocr_pages.get(page_number)
                required_figure = _requires_full_page_figure_reference(model, ocr_markdown)
                preserve_cover_visual = len(
                    pdf.pages
                ) > 1 and _sparse_raster_cover_should_stay_in_image(model)
                preserve_final_visual = (
                    len(pdf.pages) > 1
                    and page_number == len(pdf.pages)
                    and _page_letter_count(model) < _MIN_USABLE_NATIVE_LETTERS
                )
                disagreement_boxes = _unresolved_text_visual_boxes(
                    model,
                    ocr_markdown,
                    tuple(float(value) for value in page.bbox),
                )
                preserve_unresolved_page_visual = disagreement_boxes == (
                    tuple(float(value) for value in page.bbox),
                )
                candidates = list(
                    _exportable_image_boxes(
                        page,
                        model,
                        ocr_markdown,
                        preserve_full_page_visual=(
                            preserve_cover_visual
                            or preserve_final_visual
                            or preserve_unresolved_page_visual
                        ),
                    )
                )
                for disagreement_bbox in disagreement_boxes:
                    if not any(
                        _bbox_overlap_ratio(disagreement_bbox, existing) >= 0.85
                        for existing in candidates
                    ):
                        candidates.append(disagreement_bbox)
                candidates.sort(key=lambda bbox: (bbox[1], bbox[0]))
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
                    if digest in seen_content and not required_figure:
                        continue
                    seen_content.add(digest)
                    resource = PdfEmbeddedResource(
                        PurePosixPath(f"pdf/page-{page_number:04d}-image-{image_index:02d}.jpg"),
                        content,
                        "image/jpeg",
                        page_number,
                        bbox,
                        (
                            preserve_cover_visual
                            or preserve_final_visual
                            or preserve_unresolved_page_visual
                        )
                        and bbox_area_ratio >= _MAX_EXPORTED_IMAGE_AREA_RATIO,
                        any(
                            all(
                                abs(left - right) <= 0.01
                                for left, right in zip(
                                    bbox,
                                    disagreement_bbox,
                                    strict=True,
                                )
                            )
                            for disagreement_bbox in disagreement_boxes
                        ),
                    )
                    page_resources.append(resource)
                    exported += 1
                if page_resources:
                    selected[page_number] = tuple(page_resources)
                elif required_figure:
                    required_figure_failure_pages.add(page_number)
                page.close()
                if on_progress is not None:
                    on_progress(PdfProgressPhase.IMAGES, current, total)
    except (MalformedPDFException, PdfminerException, OSError, ValueError) as exc:
        raise ConversionError(
            f"No se pudieron conservar las imágenes del PDF {source_path.name}."
        ) from exc
    return selected, omitted, required_figure_failure_pages


def _exportable_image_boxes(
    page: Any,
    model: _PdfPage,
    ocr_markdown: str | None,
    *,
    preserve_full_page_visual: bool = False,
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

    for bbox in _vector_graphic_boxes(page, model.lines):
        x0, top, x1, bottom = bbox
        ratio = (x1 - x0) * (bottom - top) / page_area
        candidates.append((ratio, bbox))

    for table in model.tables:
        if (
            table.rendering is not _TableRendering.STRUCTURED_TEXT
            and not table.inferred_from_raster
            and not _unresolved_raster_table(model, table, ocr_markdown)
            and not _dense_raster_table_requires_visual(model, table)
        ):
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
    has_reliable_reflow_table = _has_reliable_reflow_table(model, ocr_markdown)
    ocr_is_toc = _is_toc_markdown(ocr_markdown or "")
    native_is_toc = _is_toc_page(list(model.lines))
    required_figure = _requires_full_page_figure_reference(model, ocr_markdown)
    fragmented_graphic_text = _fragmented_graphic_text_should_stay_in_image(
        model,
        ocr_markdown,
    )
    sparse_raster_cover = _sparse_raster_cover_should_stay_in_image(model)
    if fragmented_graphic_text and full_page:
        # On charts, diagrams and decorated plates, dozens of tiny labels can look
        # superficially OCR-like while losing all spatial meaning when reflowed.
        # The page raster is the canonical representation in that case.
        candidates = [max(full_page, key=lambda item: item[0])]
    elif (
        not candidates
        and full_page
        and not native_is_toc
        and (not ocr_is_toc or required_figure)
        and (
            (useful_letters < _FULL_PAGE_EXPORT_LETTER_LIMIT and not has_reliable_reflow_table)
            or model.image_orientation_mismatch
            or (ocr_contains_table and not has_reliable_reflow_table)
            or required_figure
            or preserve_full_page_visual
            or sparse_raster_cover
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


def _requires_full_page_figure_reference(
    model: _PdfPage,
    ocr_markdown: str | None,
) -> bool:
    """Require a visual reference when a dense scan names a numbered figure explicitly."""
    return bool(
        any(ratio >= _MAX_EXPORTED_IMAGE_AREA_RATIO for ratio in model.image_area_ratios)
        and not _is_toc_page(list(model.lines))
        and any(_is_numbered_figure_caption(line) for line in model.lines)
    )


def _is_numbered_figure_caption(line: _PdfLine) -> bool:
    if line.rotated:
        return False
    normalized = _repair_artificial_spacing_runs(_normalize_text(line.text))
    if _NUMBERED_FIGURE_CAPTION_PATTERN.match(normalized):
        return True
    compacted = "".join(character.casefold() for character in line.text if character.isalnum())
    if re.fullmatch(r"(?:figures?|figuras?)\d+", compacted):
        return True
    return bool(
        line.bold
        and _is_uppercase_text(line.text)
        and re.match(r"^(?:figures?|figuras?)\d", compacted)
    )


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


def _vector_graphic_boxes(
    page: Any,
    text_lines: tuple[_PdfLine, ...] = (),
) -> tuple[tuple[float, float, float, float], ...]:
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
        x0, top, x1, bottom = _expand_vector_bbox_with_labels(
            _union_bbox(cluster),
            text_lines,
        )
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


def _expand_vector_bbox_with_labels(
    bbox: tuple[float, float, float, float],
    lines: tuple[_PdfLine, ...],
) -> tuple[float, float, float, float]:
    """Include compact axis/diagram labels without pulling surrounding prose into the crop."""

    x0, top, x1, bottom = bbox
    nearby: list[tuple[float, float, float, float]] = []
    for line in lines:
        stripped = line.text.strip()
        if line.rotated or not stripped or len(stripped) > 8 or _heading_letter_count(stripped) > 1:
            continue
        horizontal_gap = max(x0 - line.x1, line.x0 - x1, 0.0)
        vertical_gap = max(top - line.bottom, line.top - bottom, 0.0)
        if horizontal_gap <= _VECTOR_CLUSTER_GAP * 2 and vertical_gap <= _VECTOR_CLUSTER_GAP * 2:
            nearby.append((line.x0, line.top, line.x1, line.bottom))
    return _union_bbox([bbox, *nearby]) if nearby else bbox


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
        elif _table_cell_line_is_soft_wrap(normalized_lines[-1], line):
            normalized_lines[-1] = f"{normalized_lines[-1].rstrip()} {line.lstrip()}"
        else:
            normalized_lines.append(line)
    return "\n".join(normalized_lines)


_TABLE_CELL_CONTINUATION_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "at",
        "by",
        "con",
        "de",
        "del",
        "el",
        "en",
        "for",
        "from",
        "in",
        "la",
        "of",
        "on",
        "or",
        "para",
        "por",
        "the",
        "to",
        "with",
        "y",
    }
)
_TABLE_CELL_LIST_PREFIX_PATTERN = re.compile(r"^(?:[-*•‣▪◦]|\(?\d{1,3}[.)]|[A-ZÁÉÍÓÚÑ][.)])\s+")


def _table_cell_line_is_soft_wrap(previous: str, current: str) -> bool:
    """Join a visual PDF wrap without collapsing explicit items or sentences."""

    left = previous.rstrip()
    right = current.lstrip()
    if (
        not left
        or not right
        or left[-1] in ".!?;:"
        or _TABLE_CELL_LIST_PREFIX_PATTERN.match(right) is not None
    ):
        return False
    first_letter = next((character for character in right if character.isalpha()), "")
    if first_letter.islower() or left[-1] in ",([{—–":
        return True
    last_word = re.search(r"([^\W\d_]+)[’']?$", left, re.UNICODE)
    return bool(last_word and last_word.group(1).casefold() in _TABLE_CELL_CONTINUATION_WORDS)


def _table_rendering(
    rows: tuple[tuple[str, ...], ...],
    column_count: int,
) -> _TableRendering:
    longest = max((len(cell) for row in rows for cell in row), default=0)
    multiline = any("\n" in cell for row in rows for cell in row)
    if (
        len(rows) > 80
        or column_count > 16
        or longest > 2_000
        or _table_has_sparse_continuation_rows(rows, column_count)
    ):
        return _TableRendering.STRUCTURED_TEXT
    if column_count <= 8 and longest <= 220 and not multiline:
        return _TableRendering.MARKDOWN
    return _TableRendering.HTML


def _table_has_sparse_continuation_rows(
    rows: tuple[tuple[str, ...], ...],
    column_count: int,
) -> bool:
    """Flag grids whose apparent rows probably split a smaller set of visual records."""

    if column_count < 4 or len(rows) < 5:
        return False
    sparse_rows = sum(0 < sum(bool(cell) for cell in row) <= column_count - 2 for row in rows[1:])
    return sparse_rows >= 2


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
        emphasis_spans=_source_emphasis_spans(text, raw_chars),
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


def _repair_native_degree_markers(pages: list[_PdfPage]) -> tuple[list[_PdfPage], int]:
    """Restore a superscript degree glyph misencoded as zero from native geometry."""

    accepted = 0
    repaired_pages: list[_PdfPage] = []
    for page in pages:
        repaired_lines: list[_PdfLine] = []
        for line in page.lines:
            candidates = tuple(
                sorted(
                    (
                        *_DEGREE_SHAPED_NATIVE_ZERO_PATTERN.finditer(line.text),
                        *_INVALID_ZODIAC_DEGREE_ZERO_PATTERN.finditer(line.text),
                    ),
                    key=lambda match: match.start(),
                )
            )
            zero_characters = tuple(character for character in line.chars if character.text == "0")
            visible_sizes = tuple(
                character.size
                for character in line.chars
                if character.text.strip() and character.size > 0
            )
            if not candidates or not visible_sizes:
                repaired_lines.append(line)
                continue
            typical_size = median(visible_sizes)
            superscript_zeroes = tuple(
                character
                for character in zero_characters
                if character.size <= typical_size * 0.80
                and line.bottom - character.bottom >= max(1.0, typical_size * 0.18)
            )
            if not (len(candidates) == len(zero_characters) == len(superscript_zeroes)):
                repaired_lines.append(line)
                continue
            repaired_text = _DEGREE_SHAPED_NATIVE_ZERO_PATTERN.sub(
                lambda match: f"{match.group('degree')}°",
                line.text,
            )
            repaired_text = _INVALID_ZODIAC_DEGREE_ZERO_PATTERN.sub(
                lambda match: f"{match.group('degree')}°",
                repaired_text,
            )
            repaired_lines.append(replace(line, text=repaired_text, chars=()))
            accepted += len(candidates)
        repaired_pages.append(replace(page, lines=tuple(repaired_lines)))
    if accepted:
        LOGGER.info("pdf_native_degree_geometry_completed accepted=%d", accepted)
    return repaired_pages, accepted


def _repair_overlapping_native_symbols(pages: list[_PdfPage]) -> tuple[list[_PdfPage], int]:
    """Collapse a proven overlapping punctuation encoding into its visual glyph."""

    accepted = 0
    repaired_pages: list[_PdfPage] = []
    for page in pages:
        repaired_lines: list[_PdfLine] = []
        for line in page.lines:
            text_pairs = line.text.count("(%")
            text_triplets = line.text.count("(3[")
            if not text_pairs and not text_triplets:
                repaired_lines.append(line)
                continue
            visible = tuple(character for character in line.chars if character.text.strip())
            overlapping_pairs = 0
            for left, right in zip(visible, visible[1:], strict=False):
                if left.text != "(" or right.text != "%":
                    continue
                overlap = max(0.0, min(left.x1, right.x1) - max(left.x0, right.x0))
                smaller_width = min(left.x1 - left.x0, right.x1 - right.x0)
                same_baseline = (
                    abs(left.top - right.top) <= 0.5 and abs(left.bottom - right.bottom) <= 0.5
                )
                similar_size = min(left.size, right.size) >= max(left.size, right.size) * 0.90
                if (
                    smaller_width > 0
                    and overlap / smaller_width >= 0.20
                    and same_baseline
                    and similar_size
                ):
                    overlapping_pairs += 1
            overlapping_triplets = 0
            for left, middle, right in zip(visible, visible[1:], visible[2:], strict=False):
                if (left.text, middle.text, right.text) != ("(", "3", "["):
                    continue
                left_overlap = max(0.0, min(left.x1, middle.x1) - max(left.x0, middle.x0))
                right_overlap = max(
                    0.0,
                    min(middle.x1, right.x1) - max(middle.x0, right.x0),
                )
                left_width = min(left.x1 - left.x0, middle.x1 - middle.x0)
                right_width = min(middle.x1 - middle.x0, right.x1 - right.x0)
                same_baseline = (
                    max(left.top, middle.top, right.top) - min(left.top, middle.top, right.top)
                    <= 0.5
                    and max(left.bottom, middle.bottom, right.bottom)
                    - min(left.bottom, middle.bottom, right.bottom)
                    <= 0.5
                )
                sizes = (left.size, middle.size, right.size)
                similar_size = min(sizes) >= max(sizes) * 0.90
                if (
                    left_width > 0
                    and right_width > 0
                    and left_overlap / left_width >= 0.15
                    and right_overlap / right_width >= 0.15
                    and same_baseline
                    and similar_size
                ):
                    overlapping_triplets += 1
            if overlapping_pairs != text_pairs or overlapping_triplets != text_triplets:
                repaired_lines.append(line)
                continue
            repaired_lines.append(
                replace(
                    line,
                    text=line.text.replace("(%", "&").replace("(3[", "&"),
                    chars=(),
                )
            )
            accepted += text_pairs + text_triplets
        repaired_pages.append(replace(page, lines=tuple(repaired_lines)))
    if accepted:
        LOGGER.info("pdf_native_symbol_geometry_completed accepted=%d", accepted)
    return repaired_pages, accepted


def _repair_repeated_native_spacing(pages: list[_PdfPage]) -> tuple[list[_PdfPage], int]:
    """Use repeated native lines to remove spacing noise without changing characters."""

    candidates: defaultdict[str, list[_PdfLine]] = defaultdict(list)
    for page in pages:
        for line in page.lines:
            key = _spacing_insensitive_line_key(line.text)
            if not line.rotated and len(key) >= 8 and _heading_letter_count(key) >= 6:
                candidates[key].append(line)

    replacements: dict[tuple[int, float, float, str], str] = {}
    for lines in candidates.values():
        if len({line.page_number for line in lines}) < 3:
            continue
        counts = Counter(line.text for line in lines)
        canonical, occurrences = min(
            counts.items(),
            key=lambda item: (
                -item[1],
                len(re.findall(r"\s", item[0])),
                len(item[0]),
                item[0].casefold(),
            ),
        )
        if occurrences < 2:
            continue
        canonical_spaces = len(re.findall(r"\s", canonical))
        for line in lines:
            if (
                line.text != canonical
                and len(re.findall(r"\s", line.text)) >= canonical_spaces + 2
                and _spacing_insensitive_line_key(line.text)
                == _spacing_insensitive_line_key(canonical)
            ):
                replacements[_visual_line_key(line)] = canonical
    if not replacements:
        return pages, 0
    repaired = [_apply_page_text_replacements(page, replacements) for page in pages]
    LOGGER.info("pdf_repeated_spacing_consensus_completed accepted=%d", len(replacements))
    return repaired, len(replacements)


def _spacing_insensitive_line_key(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", "", normalized)


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

    return _split_line_at_boundaries(line, boundaries)


def _split_line_at_boundaries(
    line: _PdfLine,
    boundaries: Iterable[float],
) -> tuple[_PdfLine, ...]:
    resolved_boundaries = tuple(sorted(boundaries))
    if not resolved_boundaries:
        return (line,)

    groups: list[list[_PdfCharacter]] = [[] for _ in range(len(resolved_boundaries) + 1)]
    for character in line.chars:
        center = (character.x0 + character.x1) / 2
        group_index = sum(center > boundary for boundary in resolved_boundaries)
        groups[group_index].append(character)
    if any(
        not any(any(value.isalnum() for value in character.text) for character in group)
        for group in groups
    ):
        return (line,)

    source_segments = _source_text_segments_for_character_groups(line.text, groups)
    parts: list[_PdfLine] = []
    for group, source_segment in zip(groups, source_segments, strict=True):
        compact = tuple(group)
        raw_group_text = "".join(character.text for character in compact).rstrip()
        x0 = min(character.x0 for character in compact)
        x1 = max(character.x1 for character in compact)
        top = min(character.top for character in compact)
        bottom = max(character.bottom for character in compact)
        reconstructed = _reconstruct_character_text(
            compact,
            line.font_size,
            collapse_tracking=False,
        )
        text = _repair_artificial_spacing_runs(source_segment or reconstructed)
        if not text:
            continue
        emphasis_spans = _emphasis_spans_for_segment(line.emphasis_spans, text)
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
                bold=(
                    _emphasis_is_dominant(text, emphasis_spans, bold=True)
                    if line.emphasis_spans
                    else line.bold
                ),
                italic=(
                    _emphasis_is_dominant(text, emphasis_spans, bold=False)
                    if line.emphasis_spans
                    else line.italic
                ),
                emphasis_spans=emphasis_spans,
            )
        )
    return tuple(parts) if len(parts) >= 2 else (line,)


def _source_text_segments_for_character_groups(
    source_text: str,
    groups: list[list[_PdfCharacter]],
) -> tuple[str, ...]:
    """Keep extractor-inferred word spaces when one visual line is split into columns."""
    nonspace_counts = [
        sum(not character.isspace() for item in group for character in item.text)
        for group in groups
    ]
    boundaries: list[int] = []
    source_index = 0
    for target in nonspace_counts[:-1]:
        seen = 0
        while source_index < len(source_text) and seen < target:
            if not source_text[source_index].isspace():
                seen += 1
            source_index += 1
        boundaries.append(source_index)

    segments: list[str] = []
    start = 0
    for end in (*boundaries, len(source_text)):
        segments.append(_normalize_text(source_text[start:end]))
        start = end
    if len(segments) != len(groups):
        return tuple("" for _group in groups)

    for segment, group in zip(segments, groups, strict=True):
        source_key = "".join(character for character in segment if not character.isspace())
        group_key = _normalize_text("".join(character.text for character in group)).replace(" ", "")
        if source_key != group_key:
            return tuple("" for _group in groups)
    return tuple(segments)


def _split_lines_at_established_gutters(lines: list[_PdfLine]) -> list[_PdfLine]:
    """Split a merged row only when neighbouring lines prove a stable two-column gutter."""
    if len(lines) < _MIN_COLUMN_LINES * 2:
        return lines
    page_width = max((line.page_width for line in lines), default=0.0)
    if page_width <= 0:
        return lines

    tolerance = page_width * 0.025
    starts = [
        line.x0
        for line in lines
        if page_width * 0.35 <= line.x0 <= page_width * 0.72
        and line.x1 - line.x0 <= page_width * 0.48
    ]
    gutter_starts: list[float] = []
    for start in starts:
        cluster = [candidate for candidate in starts if abs(candidate - start) <= tolerance]
        if len(cluster) < _MIN_COLUMN_LINES:
            continue
        boundary = median(cluster)
        if not any(abs(boundary - existing) <= tolerance for existing in gutter_starts):
            gutter_starts.append(boundary)

    resolved = list(lines)
    for boundary in sorted(gutter_starts):
        left = [
            line
            for line in resolved
            if line.x0 <= boundary - page_width * 0.12 and line.x1 <= boundary - page_width * 0.012
        ]
        right = [
            line
            for line in resolved
            if abs(line.x0 - boundary) <= tolerance and line.x1 - line.x0 >= page_width * 0.12
        ]
        if len(left) < _MIN_COLUMN_LINES or len(right) < _MIN_COLUMN_LINES:
            continue
        if min(max(line.bottom for line in left), max(line.bottom for line in right)) <= max(
            min(line.top for line in left), min(line.top for line in right)
        ):
            continue

        split: list[_PdfLine] = []
        for line in resolved:
            if line.x0 >= boundary or line.x1 <= boundary:
                split.append(line)
                continue
            visible = sorted(
                (
                    character
                    for character in line.chars
                    if character.text and not character.text.isspace()
                ),
                key=lambda character: character.x0,
            )
            candidates = [
                (previous.x1 + current.x0) / 2
                for previous, current in zip(visible, visible[1:], strict=False)
                if previous.x1 <= boundary <= current.x0
                and current.x0 - previous.x1 >= max(line.font_size * 0.55, 4.0)
            ]
            parts = _split_line_at_boundaries(line, candidates[:1]) if candidates else (line,)
            split.extend(parts)
        resolved = split
    return resolved


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
            result.extend(
                _order_column_band(
                    band,
                    page_width,
                    previous_line=result[-1] if result else None,
                )
            )
            band.clear()
            result.append(line)
        else:
            band.append(line)
    result.extend(
        _order_column_band(
            band,
            page_width,
            previous_line=result[-1] if result else None,
        )
    )
    return result


def _order_column_band(
    lines: list[_PdfLine],
    page_width: float,
    *,
    previous_line: _PdfLine | None = None,
) -> list[_PdfLine]:
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
    continuing_group = _continuing_column_index(groups, previous_line)
    if continuing_group is not None:
        groups = [
            groups[continuing_group],
            *groups[:continuing_group],
            *groups[continuing_group + 1 :],
        ]
    return [
        line
        for group in groups
        for line in sorted(group, key=lambda item: (item.top, item.x0, item.bottom))
    ]


def _continuing_column_index(
    groups: list[list[_PdfLine]],
    previous_line: _PdfLine | None,
) -> int | None:
    if previous_line is None or previous_line.text.rstrip().endswith((".", "!", "?", ":", ";")):
        return None
    candidates: list[int] = []
    for index, group in enumerate(groups):
        first = min(group, key=lambda line: (line.top, line.x0, line.bottom))
        gap = first.top - previous_line.bottom
        if (
            first.text[:1].islower()
            and -first.font_size * 0.5
            <= gap
            <= max(first.font_size, previous_line.font_size) * 1.25
        ):
            candidates.append(index)
    return candidates[0] if len(candidates) == 1 else None


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
    preserved_margin_headings: frozenset[tuple[int, float, float, str]] = frozenset(),
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
                preserved_repeated_headings=preserved_margin_headings,
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


def _normalize_page_footnotes(
    lines: list[_PdfLine],
    body_size: float,
) -> list[_PdfLine]:
    """Expose visually proven footnote markers without guessing from bare digits."""

    definitions: dict[int, re.Match[str]] = {}
    for index, line in enumerate(lines):
        match = _FOOTNOTE_DEFINITION_PATTERN.match(line.text.strip())
        if (
            match is not None
            and line.top >= line.page_height * 0.72
            and line.font_size <= body_size * 0.90
        ):
            definitions[index] = match
    if not definitions:
        return lines

    numbers = {match.group("number") for match in definitions.values()}

    def replace_marker(match: re.Match[str]) -> str:
        number = match.group("number")
        if number not in numbers:
            return match.group(0)
        return f"{match.group('prefix')}{number.translate(_SUPERSCRIPT_DIGITS)}"

    normalized: list[_PdfLine] = []
    for index, line in enumerate(lines):
        definition = definitions.get(index)
        if definition is not None:
            text = f"{definition.group('number')}. {definition.group('body')}"
        else:
            text = _INLINE_FOOTNOTE_MARKER_PATTERN.sub(replace_marker, line.text)
        normalized.append(replace(line, text=text) if text != line.text else line)
    return normalized


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


def _source_emphasis_spans(
    text: str,
    raw_characters: tuple[dict[str, Any], ...],
) -> tuple[_PdfEmphasisSpan, ...]:
    """Map trustworthy per-character font changes onto the normalized line text."""
    target_positions = [index for index, character in enumerate(text) if not character.isspace()]
    source_styles: list[tuple[bool, bool]] = []
    for raw_character in raw_characters:
        raw_text = _normalize_text(str(raw_character.get("text", "")))
        font_name = str(raw_character.get("fontname", ""))
        style = (_is_bold_font(font_name), _is_italic_font(font_name))
        source_styles.extend(style for character in raw_text if not character.isspace())
    if not target_positions or len(source_styles) != len(target_positions):
        return ()

    styles: list[tuple[bool, bool]] = [(False, False)] * len(text)
    for position, style in zip(target_positions, source_styles, strict=True):
        styles[position] = style
    for index, character in enumerate(text):
        if not character.isspace():
            continue
        previous_style = styles[index - 1] if index else (False, False)
        following_style = styles[index + 1] if index + 1 < len(styles) else (False, False)
        if previous_style == following_style:
            styles[index] = previous_style

    spans: list[_PdfEmphasisSpan] = []
    start = 0
    current = styles[0]
    for index, style in enumerate([*styles[1:], (False, False)], start=1):
        if style == current:
            continue
        if any(current):
            left = start
            right = index
            while left < right and text[left].isspace():
                left += 1
            while right > left and text[right - 1].isspace():
                right -= 1
            fragment = text[left:right]
            if sum(character.isalpha() for character in fragment) >= 2:
                spans.append(_PdfEmphasisSpan(fragment, current[0], current[1]))
        start = index
        current = style
    return tuple(spans)


def _emphasis_spans_for_segment(
    spans: tuple[_PdfEmphasisSpan, ...],
    segment: str,
) -> tuple[_PdfEmphasisSpan, ...]:
    resolved: list[_PdfEmphasisSpan] = []
    for span in spans:
        if span.text in segment:
            resolved.append(span)
        elif segment in span.text and sum(character.isalpha() for character in segment) >= 2:
            resolved.append(_PdfEmphasisSpan(segment, span.bold, span.italic))
    return tuple(dict.fromkeys(resolved))


def _emphasis_is_dominant(
    text: str,
    spans: tuple[_PdfEmphasisSpan, ...],
    *,
    bold: bool,
) -> bool:
    letters = sum(character.isalpha() for character in text)
    if not letters:
        return False
    emphasized = sum(
        sum(character.isalpha() for character in span.text)
        for span in spans
        if (span.bold if bold else span.italic)
    )
    return emphasized / letters >= 0.55


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
        in_standard_margin = (
            line.top <= line.page_height * 0.1 or line.bottom >= line.page_height * 0.84
        )
        in_extended_running_header_band = (
            line.top <= line.page_height * 0.18
            and len(line.text.strip()) <= 80
            and _is_uppercase_text(line.text)
        )
        if in_standard_margin or in_extended_running_header_band:
            key = _margin_key(line.text)
            if key:
                pages_by_line[key].add(line.page_number)
    repeated = {text for text, pages in pages_by_line.items() if len(pages) >= 3}
    repeated.update(_confirmed_confusable_margin_folios(lines))
    return repeated


def _confirmed_confusable_margin_folios(lines: list[_PdfLine]) -> set[str]:
    pages_by_offset: defaultdict[int, set[int]] = defaultdict(set)
    ambiguous: list[tuple[_PdfLine, int]] = []
    for line in lines:
        if not _is_top_outer_folio_line(line):
            continue
        compact = re.sub(r"\s+", "", line.text.strip())
        if compact.isdecimal() and len(compact) <= 4:
            pages_by_offset[int(compact) - line.page_number].add(line.page_number)
            continue
        decoded = _confusable_margin_folio_value(compact)
        if decoded is not None:
            ambiguous.append((line, decoded))

    confirmed_offsets = {
        offset for offset, page_numbers in pages_by_offset.items() if len(page_numbers) >= 2
    }
    return {
        _margin_key(line.text)
        for line, decoded in ambiguous
        if decoded - line.page_number in confirmed_offsets
    }


def _confusable_margin_folio_value(text: str) -> int | None:
    if (
        not 2 <= len(text) <= 4
        or not any(character.isdecimal() for character in text)
        or not any(character.isalpha() for character in text)
    ):
        return None
    translated: list[str] = []
    for character in text:
        if character.isdecimal():
            translated.append(character)
        elif character in "iIlL":
            translated.append("1")
        elif character in "oO":
            translated.append("0")
        else:
            return None
    return int("".join(translated))


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


def _extract_secondary_native_line_text(
    source_path: Path,
    pages: list[_PdfPage],
    cancellation: CancellationToken | None,
    *,
    line_keys: set[tuple[int, float, float, str]] | None = None,
) -> dict[tuple[int, float, float, str], str]:
    """Read unresolved font glyphs from PDFium inside each exact native line box."""

    targets = {
        page.number: tuple(
            line
            for line in page.lines
            if not line.rotated
            and (
                _visual_line_key(line) in line_keys
                if line_keys is not None
                else _suspicious_non_currency_dollar_count(line.text) > 0
            )
        )
        for page in pages
    }
    targets = {page_number: lines for page_number, lines in targets.items() if lines}
    if not targets:
        return {}
    try:
        import pypdfium2

        document = pypdfium2.PdfDocument(source_path)
    except (ImportError, OSError, RuntimeError, ValueError):
        LOGGER.warning("pdf_secondary_native_line_unavailable pages=%d", len(targets))
        return {}

    extracted: dict[tuple[int, float, float, str], str] = {}
    try:
        for page_number, lines in sorted(targets.items()):
            check_cancelled(cancellation)
            if page_number < 1 or page_number > len(document):
                continue
            page = document[page_number - 1]
            text_page = None
            try:
                text_page = page.get_textpage()
                page_height = float(page.get_height())
                for line in lines:
                    text = text_page.get_text_bounded(
                        left=max(0.0, line.x0 - 1.0),
                        bottom=max(0.0, page_height - line.bottom - 1.0),
                        right=min(float(page.get_width()), line.x1 + 1.0),
                        top=min(page_height, page_height - line.top + 1.0),
                    )
                    if not isinstance(text, str) or not text.strip() or "\0" in text:
                        continue
                    normalized = _WHITESPACE_PATTERN.sub(
                        " ",
                        unicodedata.normalize("NFC", text),
                    ).strip()
                    if normalized:
                        extracted[_visual_line_key(line)] = normalized
            except (OSError, RuntimeError, TypeError, ValueError):
                continue
            finally:
                if text_page is not None:
                    text_page.close()
                page.close()
    finally:
        document.close()
    LOGGER.info(
        "pdf_secondary_native_line_completed pages=%d lines=%d",
        len(targets),
        len(extracted),
    )
    return extracted


def _extract_pdf_outline(
    source_path: Path,
    page_range: PdfPageRange | None,
    cancellation: CancellationToken | None,
) -> tuple[_PdfOutlineEntry, ...]:
    """Read source bookmarks as local structural evidence without logging their text."""

    extracted: list[_PdfOutlineEntry] = []
    try:
        with pdfplumber.open(source_path, unicode_norm="NFC") as pdf:
            page_by_object_id = {
                getattr(page.page_obj, "pageid", None): index
                for index, page in enumerate(pdf.pages, start=1)
            }
            try:
                records = tuple(pdf.doc.get_outlines())
            except Exception:  # noqa: BLE001 - malformed optional bookmarks are non-fatal
                return ()
            for record in records:
                check_cancelled(cancellation)
                if len(record) < 3 or not isinstance(record[0], int):
                    continue
                level = int(record[0])
                destination = record[2]
                object_id = (
                    getattr(destination[0], "objid", None)
                    if isinstance(destination, (list, tuple)) and destination
                    else None
                )
                page_number = page_by_object_id.get(object_id)
                title = _safe_outline_title(record[1])
                if (
                    title is None
                    or page_number is None
                    or not 1 <= level <= 6
                    or (
                        page_range is not None
                        and not page_range.first_page <= page_number <= page_range.last_page
                    )
                ):
                    continue
                extracted.append(_PdfOutlineEntry(level, title, page_number))
    except (MalformedPDFException, PdfminerException, OSError, TypeError, ValueError):
        return ()
    LOGGER.info("pdf_outline_extracted entries=%d", len(extracted))
    return tuple(extracted)


def _safe_outline_title(value: object) -> str | None:
    title = _WHITESPACE_PATTERN.sub(
        " ",
        unicodedata.normalize("NFC", str(value)),
    ).strip()
    if (
        not 3 <= _heading_letter_count(title)
        or len(title) > _MAX_HEADING_LENGTH
        or any(character in title for character in "\r\n\0")
    ):
        return None
    return title


def _reconcile_pdf_outline_headings(
    pages: list[_PdfPage],
    entries: tuple[_PdfOutlineEntry, ...],
) -> tuple[list[_PdfPage], int]:
    """Promote only bookmark titles that uniquely match text on their target page."""

    entries_by_page: defaultdict[int, list[_PdfOutlineEntry]] = defaultdict(list)
    for entry in entries:
        entries_by_page[entry.page_number].append(entry)
    changed = 0
    reconciled_pages: list[_PdfPage] = []
    for page in pages:
        page_entries = entries_by_page.get(page.number)
        if not page_entries:
            reconciled_pages.append(page)
            continue
        lines = list(page.lines)
        consumed: set[int] = set()
        replacements: dict[int, tuple[_PdfLine, tuple[int, ...]]] = {}
        for entry in page_entries:
            match = _outline_heading_line_match(entry, lines, consumed)
            if match is None:
                continue
            indexes = match
            source_lines = tuple(lines[index] for index in indexes)
            first = source_lines[0]
            merged = replace(
                first,
                text=entry.title,
                chars=(),
                x0=min(line.x0 for line in source_lines),
                x1=max(line.x1 for line in source_lines),
                top=min(line.top for line in source_lines),
                bottom=max(line.bottom for line in source_lines),
                font_size=max(line.font_size for line in source_lines),
                bold=any(line.bold for line in source_lines),
                italic=all(line.italic for line in source_lines),
                emphasis_spans=(),
                links=tuple(dict.fromkeys(link for line in source_lines for link in line.links)),
                soft_hyphen_end=False,
                hard_hyphen_end=False,
                outline_level=min(6, entry.level + 1),
            )
            replacements[indexes[0]] = (merged, indexes)
            consumed.update(indexes)
            changed += 1
        if not replacements:
            reconciled_pages.append(page)
            continue
        rebuilt: list[_PdfLine] = []
        skipped: set[int] = set()
        for index, line in enumerate(lines):
            if index in skipped:
                continue
            replacement = replacements.get(index)
            if replacement is None:
                rebuilt.append(line)
                continue
            merged, indexes = replacement
            rebuilt.append(merged)
            skipped.update(indexes[1:])
        reconciled_pages.append(replace(page, lines=tuple(rebuilt)))
    if changed:
        LOGGER.info("pdf_outline_headings_reconciled accepted=%d", changed)
    return reconciled_pages, changed


def _outline_heading_line_match(
    entry: _PdfOutlineEntry,
    lines: list[_PdfLine],
    consumed: set[int],
) -> tuple[int, ...] | None:
    target_key = _outline_comparison_key(entry.title)
    target_numbers = _content_number_tokens(entry.title)
    if len(target_key) < 4:
        return None
    ranked: list[tuple[float, int, int, tuple[int, ...]]] = []
    for start, first in enumerate(lines):
        if start in consumed or first.rotated:
            continue
        indexes: list[int] = []
        fragments: list[str] = []
        previous: _PdfLine | None = None
        for index in range(start, min(len(lines), start + 3)):
            line = lines[index]
            if index in consumed or line.rotated:
                break
            if previous is not None and line.top - previous.bottom > max(
                first.font_size * 1.8,
                18.0,
            ):
                break
            indexes.append(index)
            fragments.append(line.text)
            candidate = " ".join(fragments)
            candidate_key = _outline_comparison_key(candidate)
            if (
                len(candidate_key) >= 4
                and _content_number_tokens(candidate) == target_numbers
                and 0.70 <= len(candidate_key) / len(target_key) <= 1.30
            ):
                score = SequenceMatcher(
                    None,
                    target_key,
                    candidate_key,
                    autojunk=False,
                ).ratio()
                ranked.append((score, -len(indexes), -start, tuple(indexes)))
            previous = line
    ranked.sort(reverse=True)
    if not ranked or ranked[0][0] < 0.94:
        return None
    if len(ranked) > 1 and ranked[1][0] >= ranked[0][0] - 0.015:
        return None
    return ranked[0][3]


def _outline_comparison_key(value: str) -> str:
    normalized = "".join(
        character
        for character in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(character)
    )
    normalized = normalized.replace("$", "").replace("�", "")
    return re.sub(r"[^\w]+", "", normalized, flags=re.UNICODE)


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
    reconciled = [_apply_page_text_replacements(page, replacements) for page in pages]
    LOGGER.info("pdf_secondary_native_arbitration_completed accepted=%d", len(replacements))
    return reconciled, len(replacements)


def _reconcile_secondary_native_text_lines(
    pages: list[_PdfPage],
    ocr_pages: dict[int, str],
    secondary_lines: dict[tuple[int, float, float, str], str],
) -> tuple[list[_PdfPage], int]:
    """Accept OCR text only when the same line box yields that reading through PDFium."""

    replacements: dict[tuple[int, float, float, str], str] = {}
    for disagreement in _visual_text_disagreements(pages, ocr_pages):
        key = _visual_line_key(disagreement.line)
        secondary = secondary_lines.get(key)
        if secondary is None:
            continue
        secondary = _aligned_visual_candidate(disagreement.line.text, secondary)
        ocr = _aligned_visual_candidate(disagreement.line.text, disagreement.ocr_text)
        if _comparison_line_key(secondary) != _comparison_line_key(ocr):
            continue
        accepted = _validated_visual_reading(
            disagreement.line.text,
            ocr,
            ocr,
        )
        if accepted is not None and accepted != disagreement.line.text:
            replacements[key] = accepted
    if not replacements:
        return pages, 0
    reconciled = [_apply_page_text_replacements(page, replacements) for page in pages]
    LOGGER.info(
        "pdf_secondary_native_line_arbitration_completed accepted=%d",
        len(replacements),
    )
    return reconciled, len(replacements)


def _reconcile_secondary_dollar_glyphs(
    pages: list[_PdfPage],
    secondary_pages: dict[int, str],
) -> tuple[list[_PdfPage], int]:
    """Repair a broken font glyph only when PDFium preserves the same words and numbers."""

    replacements: dict[tuple[int, float, float, str], str] = {}
    for page in pages:
        secondary = secondary_pages.get(page.number)
        if secondary is None:
            continue
        candidates = _visible_ocr_lines(secondary)
        for line in page.lines:
            if line.rotated or _suspicious_non_currency_dollar_count(line.text) == 0:
                continue
            proposed = _secondary_dollar_glyph_candidate(line.text, candidates)
            if proposed is not None:
                replacements[_visual_line_key(line)] = proposed
    if not replacements:
        return pages, 0
    reconciled = [_apply_page_text_replacements(page, replacements) for page in pages]
    LOGGER.info("pdf_secondary_dollar_glyph_repair_completed accepted=%d", len(replacements))
    return reconciled, len(replacements)


def _reconcile_secondary_dollar_glyph_lines(
    pages: list[_PdfPage],
    secondary_lines: dict[tuple[int, float, float, str], str],
) -> tuple[list[_PdfPage], int]:
    """Repair remaining font glyphs from a geometry-aligned PDFium line reading."""

    replacements: dict[tuple[int, float, float, str], str] = {}
    for page in pages:
        for line in page.lines:
            key = _visual_line_key(line)
            secondary = secondary_lines.get(key)
            if (
                line.rotated
                or secondary is None
                or _suspicious_non_currency_dollar_count(line.text) == 0
            ):
                continue
            proposed = _secondary_dollar_glyph_candidate(line.text, (secondary,))
            if proposed is not None:
                replacements[key] = proposed
    if not replacements:
        return pages, 0
    reconciled = [_apply_page_text_replacements(page, replacements) for page in pages]
    LOGGER.info(
        "pdf_secondary_dollar_glyph_line_repair_completed accepted=%d",
        len(replacements),
    )
    return reconciled, len(replacements)


def _reconcile_systemic_secondary_native_lines(
    pages: list[_PdfPage],
    secondary_lines: dict[tuple[int, float, float, str], str],
) -> tuple[list[_PdfPage], int]:
    """Use geometry-aligned PDFium text after a document proves systemic font damage."""

    secondary_token_counts = _document_alpha_token_counts(secondary_lines.values())
    replacements: dict[tuple[int, float, float, str], str] = {}
    for page in pages:
        for line in page.lines:
            key = _visual_line_key(line)
            secondary = secondary_lines.get(key)
            if line.rotated or secondary is None:
                continue
            proposed = _systemic_secondary_native_line_candidate(
                line.text,
                secondary,
                secondary_token_counts=secondary_token_counts,
            )
            if proposed is not None:
                replacements[key] = proposed
    if not replacements:
        return pages, 0
    reconciled = [_apply_page_text_replacements(page, replacements) for page in pages]
    LOGGER.info(
        "pdf_systemic_secondary_native_repair_completed accepted=%d",
        len(replacements),
    )
    return reconciled, len(replacements)


def _systemic_secondary_native_line_candidate(
    native: str,
    secondary: str,
    *,
    secondary_token_counts: Counter[str] | None = None,
) -> str | None:
    if (
        secondary == native
        or not secondary
        or any(character in secondary for character in "\r\n\0�")
        or _suspicious_non_currency_dollar_count(secondary) > 0
        or _has_suspicious_text_glyph(secondary)
        or _content_number_tokens(secondary) != _content_number_tokens(native)
        or not 0.85 <= len(secondary) / max(len(native), 1) <= 1.15
        or SequenceMatcher(
            None,
            native.casefold(),
            secondary.casefold(),
            autojunk=False,
        ).ratio()
        < 0.88
    ):
        return None

    native_compact = _outline_comparison_key(native)
    secondary_compact = _outline_comparison_key(secondary)
    if native_compact == secondary_compact:
        transferred = _secondary_characters_with_native_separators(native, secondary)
        if transferred is not None:
            return transferred
        if secondary_token_counts is not None:
            return _confirmed_secondary_boundary_join(
                native,
                secondary,
                secondary_token_counts,
            )
        return None

    native_atoms = _visual_atoms(native)
    secondary_atoms = _visual_atoms(secondary)
    if not native_atoms or len(native_atoms) != len(secondary_atoms):
        return None
    changed = 0
    for native_atom, secondary_atom in zip(native_atoms, secondary_atoms, strict=True):
        if native_atom == secondary_atom:
            continue
        native_key = _diacritic_free_key(native_atom)
        secondary_key = _diacritic_free_key(secondary_atom)
        allowance = 2 if native_atom.isupper() and len(native_atom) <= 4 else 1
        if _levenshtein_distance(native_key, secondary_key) > allowance:
            return None
        changed += 1
    return secondary if 1 <= changed <= 4 else None


def _secondary_characters_with_native_separators(native: str, secondary: str) -> str | None:
    """Adopt PDFium glyphs while keeping native word boundaries and punctuation."""

    native_positions = [index for index, character in enumerate(native) if character.isalnum()]
    secondary_characters = [character for character in secondary if character.isalnum()]
    if len(native_positions) != len(secondary_characters):
        return None
    native_characters = [native[index] for index in native_positions]
    if "".join(_diacritic_free_key(character) for character in native_characters) != "".join(
        _diacritic_free_key(character) for character in secondary_characters
    ):
        return None
    rebuilt = list(native)
    for index, character in zip(native_positions, secondary_characters, strict=True):
        rebuilt[index] = character
    proposed = "".join(rebuilt)
    return proposed if proposed != native else None


def _confirmed_secondary_boundary_join(
    native: str,
    secondary: str,
    secondary_token_counts: Counter[str],
) -> str | None:
    """Remove a native word split only when PDFium repeats the joined token elsewhere."""

    native_tokens = tuple(re.finditer(r"[^\W\d_]+", native, re.UNICODE))
    secondary_tokens = tuple(re.finditer(r"[^\W\d_]+", secondary, re.UNICODE))
    if len(secondary_tokens) >= len(native_tokens):
        return None

    def boundaries(tokens: tuple[re.Match[str], ...]) -> set[int]:
        positions: set[int] = set()
        consumed = 0
        for token in tokens[:-1]:
            consumed += len(token.group(0))
            positions.add(consumed)
        return positions

    native_boundaries = boundaries(native_tokens)
    secondary_boundaries = boundaries(secondary_tokens)
    if not secondary_boundaries < native_boundaries:
        return None
    removed = native_boundaries - secondary_boundaries
    start = 0
    for token in secondary_tokens:
        end = start + len(token.group(0))
        if any(start < position < end for position in removed):
            if secondary_token_counts[token.group(0).casefold()] < 2:
                return None
        start = end
    return secondary


def _secondary_dollar_glyph_candidate(
    native: str,
    secondary_lines: tuple[str, ...],
) -> str | None:
    native_tokens = _ocr_addition_token_sequence(native)
    if not native_tokens or _suspicious_non_currency_dollar_count(native) == 0:
        return None
    candidates: list[str] = []
    for secondary in secondary_lines:
        proposed = _aligned_visual_candidate(native, secondary)
        proposed = _align_secondary_token_case(native, proposed)
        similarity = SequenceMatcher(
            None,
            native.casefold(),
            proposed.casefold(),
            autojunk=False,
        ).ratio()
        if (
            similarity < 0.90
            or proposed == native
            or any(character in proposed for character in "\r\n\0�")
            or len(proposed) > 300
            or _suspicious_non_currency_dollar_count(proposed) > 0
            or _has_suspicious_text_glyph(proposed)
            or _ocr_addition_token_sequence(proposed) != native_tokens
            or _content_number_tokens(proposed) != _content_number_tokens(native)
        ):
            continue
        candidates.append(proposed)
    unique = tuple(dict.fromkeys(candidates))
    return unique[0] if len(unique) == 1 else None


def _align_secondary_token_case(native: str, proposed: str) -> str:
    """Keep proven native capitalization while adopting PDFium punctuation and diacritics."""

    native_matches = tuple(re.finditer(r"[^\W_]+", native, re.UNICODE))
    proposed_matches = tuple(re.finditer(r"[^\W_]+", proposed, re.UNICODE))
    if len(native_matches) != len(proposed_matches):
        return proposed
    parts: list[str] = []
    previous_end = 0
    for native_match, proposed_match in zip(native_matches, proposed_matches, strict=True):
        parts.append(proposed[previous_end : proposed_match.start()])
        parts.append(
            _align_consensus_token_case(
                native_match.group(0),
                proposed_match.group(0),
            )
        )
        previous_end = proposed_match.end()
    parts.append(proposed[previous_end:])
    return "".join(parts)


def _reconcile_document_token_consensus(
    pages: list[_PdfPage],
    ocr_pages: dict[int, str],
) -> tuple[list[_PdfPage], int]:
    """Accept one OCR token only when independent document occurrences corroborate it."""

    disagreements = _visual_text_disagreements(pages, ocr_pages)
    if not disagreements:
        return pages, 0
    native_counts = _document_alpha_token_counts(
        line.text for page in pages for line in page.lines if not line.rotated
    )
    ocr_counts = _document_alpha_token_counts(
        line for markdown in ocr_pages.values() for line in _visible_ocr_lines(markdown)
    )
    replacements: dict[tuple[int, float, float, str], str] = {}
    for disagreement in disagreements:
        proposed = _document_consensus_candidate(
            disagreement.line.text,
            disagreement.ocr_text,
            native_counts,
            ocr_counts,
        )
        if proposed is not None:
            replacements[_visual_line_key(disagreement.line)] = proposed
    if not replacements:
        return pages, 0
    reconciled = [_apply_page_text_replacements(page, replacements) for page in pages]
    LOGGER.info("pdf_document_consensus_completed accepted=%d", len(replacements))
    return reconciled, len(replacements)


def _reconcile_suspicious_numbers_from_ocr(
    pages: list[_PdfPage],
    ocr_pages: dict[int, str],
) -> tuple[list[_PdfPage], int]:
    """Repair a broken numeric token only when its aligned OCR line selects one value."""

    replacements: dict[tuple[int, float, float, str], str] = {}
    for page in pages:
        ocr_markdown = ocr_pages.get(page.number)
        if not ocr_markdown:
            continue
        ocr_lines = _visible_ocr_lines(ocr_markdown)
        for line in page.lines:
            if line.rotated:
                continue
            suspicious_matches = _suspicious_numeric_glyph_matches(line.text)
            if not suspicious_matches:
                continue
            suspicious_spans = {match.span() for match in suspicious_matches}
            match = _best_matching_text_line(line.text, ocr_lines)
            if match is None or match[0] < 0.72:
                continue
            confirmed_numbers = set(re.findall(r"(?<!\d)\d{1,4}(?!\d)", match[1]))
            if not confirmed_numbers:
                continue

            def reconcile_token(
                candidate: re.Match[str],
                confirmed_numbers: set[str] = confirmed_numbers,
                suspicious_spans: set[tuple[int, int]] = suspicious_spans,
            ) -> str:
                if candidate.span() not in suspicious_spans:
                    return candidate.group(0)
                values = _numeric_glyph_candidates(candidate.group(0)) & confirmed_numbers
                return next(iter(values)) if len(values) == 1 else candidate.group(0)

            proposed = _SUSPICIOUS_NUMERIC_GLYPH_PATTERN.sub(reconcile_token, line.text)
            if proposed != line.text:
                replacements[_visual_line_key(line)] = proposed
    if not replacements:
        return pages, 0
    reconciled = [_apply_page_text_replacements(page, replacements) for page in pages]
    LOGGER.info("pdf_numeric_glyph_consensus_completed accepted=%d", len(replacements))
    return reconciled, len(replacements)


def _document_alpha_token_counts(texts: Iterable[str]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for text in texts:
        counts.update(
            atom.casefold()
            for atom in _visual_atoms(text)
            if atom.isalpha() and "\ufffd" not in atom
        )
    return counts


def _document_consensus_candidate(
    native: str,
    ocr: str,
    native_counts: Counter[str],
    ocr_counts: Counter[str],
) -> str | None:
    native_matches = tuple(_VISUAL_ATOM_PATTERN.finditer(unicodedata.normalize("NFC", native)))
    ocr_atoms = _visual_atoms(ocr)
    if not native_matches or len(native_matches) != len(ocr_atoms):
        return None
    native_atoms = tuple(match.group(0) for match in native_matches)
    changed = [
        index
        for index, (native_atom, ocr_atom) in enumerate(zip(native_atoms, ocr_atoms, strict=True))
        if native_atom != ocr_atom
    ]
    if not changed or len(changed) > 2:
        return None
    current_native_counts = Counter(atom.casefold() for atom in native_atoms if atom.isalpha())
    supported: list[tuple[int, str]] = []
    evidence_keys = set(native_counts) | set(ocr_counts)
    for index in changed:
        native_atom = native_atoms[index]
        ocr_atom = ocr_atoms[index]
        native_key = native_atom.casefold()
        ocr_key = ocr_atom.casefold()
        if (
            native_key == ocr_key
            or not native_atom.isalpha()
            or not ocr_atom.isalpha()
            or abs(len(native_atom) - len(ocr_atom)) > 1
            or _levenshtein_distance(native_key, ocr_key) != 1
            or re.search(r"(?<=[a-z])(?=[A-Z])", ocr_atom)
        ):
            continue
        native_has_internal_uppercase = bool(re.search(r"(?<=[a-z])[A-Z]", native_atom))
        prefixed_title_candidate = (
            native_atom[1:] == ocr_atom
            and native_atom[:1].islower()
            and ocr_atom[:1].isupper()
            and ocr_atom[1:].islower()
        )
        if native_has_internal_uppercase and not prefixed_title_candidate:
            continue
        same_base_letters = _diacritic_free_key(native_atom) == _diacritic_free_key(ocr_atom)
        if same_base_letters and not _has_diacritic(ocr_atom):
            continue
        if min(len(native_atom), len(ocr_atom)) < 5 and not (
            prefixed_title_candidate or same_base_letters
        ):
            continue
        external_native = native_counts[ocr_key] - current_native_counts[ocr_key]
        if external_native < 1 or ocr_counts[ocr_key] < 1:
            continue
        candidate_support = external_native + ocr_counts[ocr_key]
        native_support = (
            native_counts[native_key] - current_native_counts[native_key] + ocr_counts[native_key]
        )
        if candidate_support < max(2, native_support + 1):
            continue
        ambiguous = False
        for alternative_key in evidence_keys - {native_key, ocr_key}:
            if (
                abs(len(alternative_key) - len(native_key)) > 1
                or _levenshtein_distance(native_key, alternative_key) != 1
                or native_counts[alternative_key] < 1
                or ocr_counts[alternative_key] < 1
            ):
                continue
            alternative_support = native_counts[alternative_key] + ocr_counts[alternative_key]
            if alternative_support >= candidate_support:
                ambiguous = True
                break
        if ambiguous:
            continue
        supported.append((index, _align_consensus_token_case(native_atom, ocr_atom)))
    if len(supported) != 1:
        return None
    index, replacement = supported[0]
    match = native_matches[index]
    proposed = native[: match.start()] + replacement + native[match.end() :]
    return _validated_visual_reading(native, ocr, proposed)


def _diacritic_free_key(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(character)
    )


def _has_diacritic(value: str) -> bool:
    return _diacritic_free_key(value) != value.casefold()


def _align_consensus_token_case(native: str, candidate: str) -> str:
    if native.isupper():
        return candidate.upper()
    if native.islower():
        return candidate.lower()
    if native[:1].isupper() and native[1:].islower():
        return candidate[:1].upper() + candidate[1:].lower()
    return candidate


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
    inferred_table_pages = {
        page.number for page in pages if any(table.inferred_from_raster for table in page.tables)
    }
    for disagreement in sorted(
        disagreements,
        key=lambda item: (-item.priority, item.page_number, item.line.top, item.line.x0),
    ):
        per_page_limit = (
            _MAX_VISUAL_ARBITRATION_REGIONS_PER_PAGE * 2
            if disagreement.page_number in inferred_table_pages
            else _MAX_VISUAL_ARBITRATION_REGIONS_PER_PAGE
        )
        if per_page[disagreement.page_number] >= per_page_limit:
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
    reconciled = [_apply_page_text_replacements(page, replacements) for page in pages]
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
        if ocr_markdown is None or (
            not page.has_table and _should_replace_with_ocr(page, ocr_markdown)
        ):
            continue
        ocr_lines = _visible_ocr_lines(ocr_markdown)
        if not ocr_lines:
            continue
        for line in page.lines:
            if line.rotated or not (3 <= len(line.text) <= 300):
                continue
            native_replacement_suspicion = _has_suspicious_text_glyph(line.text)
            native_numeric_suspicion = _line_has_suspicious_numeric_glyph(line)
            native_dollar_suspicion = _suspicious_non_currency_dollar_count(line.text) > 0
            native_self_suspicion = bool(
                native_replacement_suspicion or native_numeric_suspicion or native_dollar_suspicion
            )
            match = _best_matching_text_line(line.text, ocr_lines)
            # A contents folio is often a detached native line but part of the OCR row.
            # Keep a deliberately low preliminary threshold, then validate the aligned
            # candidate with the strict same-atom guard below.
            if match is not None and match[0] >= 0.60:
                similarity, candidate = match
                candidate = _aligned_visual_candidate(line.text, candidate)
                similarity = SequenceMatcher(
                    None,
                    line.text.casefold(),
                    candidate.casefold(),
                    autojunk=False,
                ).ratio()
            else:
                similarity, candidate = 0.0, line.text
            priority = (
                _visual_disagreement_priority(line.text, candidate, similarity)
                if similarity >= 0.74
                else None
            )
            if priority is None:
                token_candidate = _token_reconciled_visual_candidate(line.text, ocr_lines)
                if token_candidate is not None:
                    token_similarity = SequenceMatcher(
                        None,
                        line.text.casefold(),
                        token_candidate.casefold(),
                        autojunk=False,
                    ).ratio()
                    token_priority = _visual_disagreement_priority(
                        line.text,
                        token_candidate,
                        token_similarity,
                    )
                    if token_priority is not None:
                        similarity = token_similarity
                        candidate = token_candidate
                        priority = token_priority
            if priority is None and native_self_suspicion:
                similarity, candidate = 1.0, line.text
                if native_replacement_suspicion:
                    priority = 135
                elif native_numeric_suspicion:
                    priority = 128
                else:
                    priority = 126
            if priority is None:
                continue
            disagreements.append(_PdfVisualDisagreement(page.number, line, candidate, priority))
    return tuple(disagreements)


def _token_reconciled_visual_candidate(
    native: str,
    ocr_lines: tuple[str, ...],
) -> str | None:
    """Build a bounded same-shape OCR candidate when page layout interleaves columns."""

    native_matches = tuple(_VISUAL_ATOM_PATTERN.finditer(unicodedata.normalize("NFC", native)))
    if not native_matches:
        return None
    ocr_atoms = tuple(atom for line in ocr_lines for atom in _visual_atoms(line) if len(atom) >= 4)
    counts = Counter(atom.casefold() for atom in ocr_atoms)
    surfaces: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for atom in ocr_atoms:
        surfaces[atom.casefold()][atom] += 1
    if not counts:
        return None

    replacements: dict[int, str] = {}
    for index, match in enumerate(native_matches):
        atom = match.group(0)
        if len(atom) < 4 or atom.isdigit() or atom.casefold() in counts:
            continue
        ranked: list[tuple[int, int, int, str]] = []
        for candidate_key, frequency in counts.items():
            if abs(len(candidate_key) - len(atom)) > 1:
                continue
            distance = _levenshtein_distance(atom.casefold(), candidate_key)
            if distance != 1:
                continue
            ranked.append(
                (distance, -frequency, abs(len(candidate_key) - len(atom)), candidate_key)
            )
        if not ranked:
            continue
        ranked.sort()
        best = ranked[0]
        if len(ranked) > 1 and ranked[1][:3] == best[:3]:
            continue
        candidate_key = best[3]
        surface = surfaces[candidate_key].most_common(1)[0][0]
        if atom[:1].isupper() and surface[:1].islower():
            surface = surface[:1].upper() + surface[1:]
        replacements[index] = surface
        if len(replacements) >= 2:
            break
    if not replacements:
        return None

    parts: list[str] = []
    previous_end = 0
    for index, match in enumerate(native_matches):
        parts.append(native[previous_end : match.start()])
        parts.append(replacements.get(index, match.group(0)))
        previous_end = match.end()
    parts.append(native[previous_end:])
    candidate = "".join(parts)
    return candidate if candidate != native else None


def _apply_page_text_replacements(
    page: _PdfPage,
    replacements: dict[tuple[int, float, float, str], str],
) -> _PdfPage:
    """Apply accepted visual readings to both flow lines and their structured table cells."""

    changed_lines = tuple(
        replace(line, text=replacements[_visual_line_key(line)])
        if _visual_line_key(line) in replacements
        else line
        for line in page.lines
    )
    if not page.tables:
        return replace(page, lines=changed_lines) if changed_lines != page.lines else page

    changed_tables: list[_PdfTable] = []
    for table in page.tables:
        table_replacements = tuple(
            (line.text, replacements[_visual_line_key(line)])
            for line in page.lines
            if _visual_line_key(line) in replacements and _line_inside_table(line, table)
        )
        if not table_replacements:
            changed_tables.append(table)
            continue
        rows = [list(row) for row in table.rows]
        for old, new in table_replacements:
            matches = [
                (row_index, column_index)
                for row_index, row in enumerate(rows)
                for column_index, cell in enumerate(row)
                if old in cell
            ]
            if len(matches) == 1:
                row_index, column_index = matches[0]
                if rows[row_index][column_index].count(old) == 1:
                    rows[row_index][column_index] = rows[row_index][column_index].replace(
                        old, new, 1
                    )
                    continue
            atom_replacement = _single_visual_atom_replacement(old, new)
            if atom_replacement is None:
                continue
            old_atom, new_atom = atom_replacement
            atom_matches = [
                (row_index, column_index, match.start(), match.end())
                for row_index, row in enumerate(rows)
                for column_index, cell in enumerate(row)
                for match in _VISUAL_ATOM_PATTERN.finditer(cell)
                if match.group(0) == old_atom
            ]
            if len(atom_matches) != 1:
                continue
            row_index, column_index, start, end = atom_matches[0]
            cell = rows[row_index][column_index]
            rows[row_index][column_index] = cell[:start] + new_atom + cell[end:]
        updated_rows = tuple(tuple(row) for row in rows)
        changed_tables.append(
            replace(table, rows=updated_rows) if updated_rows != table.rows else table
        )
    tables = tuple(changed_tables)
    if changed_lines == page.lines and tables == page.tables:
        return page
    return replace(page, lines=changed_lines, tables=tables)


def _single_visual_atom_replacement(old: str, new: str) -> tuple[str, str] | None:
    old_atoms = _visual_atoms(old)
    new_atoms = _visual_atoms(new)
    if not old_atoms or len(old_atoms) != len(new_atoms):
        return None
    changed = [
        (old_atom, new_atom)
        for old_atom, new_atom in zip(old_atoms, new_atoms, strict=True)
        if old_atom != new_atom
    ]
    return changed[0] if len(changed) == 1 else None


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
    if any(re.search(r"(?<=[a-z])[A-Z]", left) for left, _right in changed_pairs):
        return 122 + round(similarity * 10)
    if any(
        "".join(
            character
            for character in unicodedata.normalize("NFKD", left.casefold())
            if not unicodedata.combining(character)
        )
        == "".join(
            character
            for character in unicodedata.normalize("NFKD", right.casefold())
            if not unicodedata.combining(character)
        )
        and left.casefold() != right.casefold()
        for left, right in changed_pairs
    ):
        return 108 + round(similarity * 10)
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
        if any(max(len(left), len(right)) >= 11 for left, right in changed_pairs):
            return 86 + round(similarity * 10)
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
            and not (
                min(len(native_atom), len(ocr_atom), len(proposed_atom)) >= 4
                and _levenshtein_distance(
                    proposed_atom.casefold(),
                    native_atom.casefold(),
                )
                <= 1
                and _levenshtein_distance(
                    proposed_atom.casefold(),
                    ocr_atom.casefold(),
                )
                <= 1
            )
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
    bbox = _visual_text_crop_bbox(
        tuple(float(value) for value in page.bbox),
        line,
    )
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


def _visual_text_crop_bbox(
    page_bbox: tuple[float, float, float, float],
    line: _PdfLine,
) -> tuple[float, float, float, float]:
    page_x0, page_top, page_x1, page_bottom = page_bbox
    line_height = max(line.bottom - line.top, 2.0)
    vertical_padding = max(1.0, min(2.0, line_height * 0.15))
    bbox = (
        max(page_x0, line.x0 - max(8.0, line_height * 1.4)),
        max(page_top, line.top - vertical_padding),
        min(page_x1, line.x1 + max(8.0, line_height * 1.4)),
        min(page_bottom, line.bottom + vertical_padding),
    )
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        raise ValueError("invalid visual crop")
    return bbox


def _unresolved_text_visual_boxes(
    page: _PdfPage,
    ocr_markdown: str | None,
    page_bbox: tuple[float, float, float, float],
) -> tuple[tuple[float, float, float, float], ...]:
    """Select visual evidence only for text that neither native extraction nor OCR proves."""

    if page.has_table or not ocr_markdown or _visual_only_page_reason(page, ocr_markdown):
        return ()
    disagreements = tuple(
        disagreement
        for disagreement in _visual_text_disagreements([page], {page.number: ocr_markdown})
        if disagreement.priority >= _MIN_VISUAL_TEXT_FALLBACK_PRIORITY
        and not _native_diacritic_is_stronger_than_ocr(
            disagreement.line.text,
            disagreement.ocr_text,
        )
    )
    if not disagreements:
        return ()
    if len(disagreements) > _MAX_VISUAL_ARBITRATION_REGIONS:
        return (page_bbox,)
    return tuple(
        dict.fromkeys(
            _visual_text_crop_bbox(page_bbox, disagreement.line)
            for disagreement in sorted(
                disagreements,
                key=lambda item: (-item.priority, item.line.top, item.line.x0),
            )
        )
    )


def _visual_text_fallback_line_keys(
    page: _PdfPage,
    ocr_markdown: str | None,
    resources: tuple[PdfEmbeddedResource, ...],
) -> set[tuple[int, float, float, str]]:
    """Return only uncertain lines whose exact local visual evidence was exported."""

    if (
        page.has_table
        or not ocr_markdown
        or any(resource.visual_authority for resource in resources)
    ):
        return set()
    disagreements = _visual_text_disagreements([page], {page.number: ocr_markdown})
    preserved: set[tuple[int, float, float, str]] = set()
    for disagreement in disagreements:
        if disagreement.priority < _MIN_VISUAL_TEXT_FALLBACK_PRIORITY:
            continue
        if _native_diacritic_is_stronger_than_ocr(
            disagreement.line.text,
            disagreement.ocr_text,
        ):
            continue
        line_bbox = (
            disagreement.line.x0,
            disagreement.line.top,
            disagreement.line.x1,
            disagreement.line.bottom,
        )
        if any(
            resource.visual_text_authority
            and resource.bbox is not None
            and _bbox_overlap_ratio(line_bbox, resource.bbox) >= 0.99
            for resource in resources
        ):
            preserved.add(_visual_line_key(disagreement.line))
    return preserved


def _native_diacritic_is_stronger_than_ocr(native: str, ocr: str) -> bool:
    """Prefer a useful native layer when OCR only strips valid diacritics."""

    native_atoms = _visual_atoms(native)
    ocr_atoms = _visual_atoms(ocr)
    if not native_atoms or len(native_atoms) != len(ocr_atoms):
        return False
    changed = [
        (native_atom, ocr_atom)
        for native_atom, ocr_atom in zip(native_atoms, ocr_atoms, strict=True)
        if native_atom != ocr_atom
    ]
    return bool(changed) and all(
        native_atom.isalpha()
        and ocr_atom.isalpha()
        and _diacritic_free_key(native_atom) == _diacritic_free_key(ocr_atom)
        and _has_diacritic(native_atom)
        and not _has_diacritic(ocr_atom)
        for native_atom, ocr_atom in changed
    )


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
    full_page_scan = any(ratio >= _FULL_PAGE_IMAGE_AREA_RATIO for ratio in _image_area_ratios(page))
    minimum_rule_width_ratio = _OPEN_RASTER_TABLE_RULE_WIDTH_RATIO if full_page_scan else 0.70
    rule_groups = _spatial_table_rule_groups(
        _raster_horizontal_rules(
            page,
            minimum_width_ratio=minimum_rule_width_ratio,
        ),
        useful,
    )
    toc_page = _is_toc_page(list(useful))
    has_links = any(line.links for line in useful)
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
        open_table_boundaries = None
        open_sparse_matrix = False
        if full_page_scan and not toc_page and not has_links and not has_caption:
            open_table_boundaries = _open_raster_table_boundaries(
                page,
                region_lines,
                page_width,
                page_height,
                rules,
            )
            if open_table_boundaries is not None:
                vertical, horizontal, open_sparse_matrix = open_table_boundaries
        if len(rules) == 2 and has_caption:
            completed_rules = _complete_sparse_table_rules(rules, region_lines, page_width)
            if completed_rules is not None:
                rules = completed_rules
        if open_table_boundaries is None:
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
        native_rows = rows
        if open_sparse_matrix:
            rows = _restore_visual_matrix_placeholders(page, rows, vertical, horizontal)
            rows = _repair_consensus_degree_markers(rows)
        column_count = max((len(row) for row in rows), default=0)
        populated = sum(bool(cell) for row in rows for cell in row)
        minimum_rows = 4 if open_table_boundaries is not None else (2 if has_caption else 4)
        population_ratio = populated / max(1, len(rows) * column_count)
        open_table_shape_is_safe = True
        if open_table_boundaries is not None:
            if open_sparse_matrix:
                open_table_shape_is_safe = bool(
                    4 <= column_count <= 8
                    and rows
                    and all(rows[0])
                    and all(row and row[0] for row in rows)
                    and population_ratio >= 0.25
                )
            else:
                open_table_shape_is_safe = bool(
                    column_count == 2
                    and population_ratio >= 0.85
                    and all(row and row[0] for row in rows)
                )
        if (
            not minimum_rows <= len(rows) <= _MAX_PDF_TABLE_ROWS
            or not 2 <= column_count <= 8
            or (open_table_boundaries is None and populated / (len(rows) * column_count) < 0.60)
            or not open_table_shape_is_safe
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
            or not _table_has_exact_character_coverage(page, bbox, native_rows)
        ):
            continue
        model = _PdfTable((bbox[0], bbox[1], bbox[2], bbox[3]), (), _TableRendering.HTML)
        if any(line.links and _line_inside_table(line, model) for line in lines):
            continue
        tables.append(
            _PdfTable(
                model.bbox,
                rows,
                _table_rendering(rows, column_count),
                inferred_from_raster=open_table_boundaries is not None,
            )
        )
    return tuple(tables)


def _open_raster_table_boundaries(
    page: Any,
    lines: tuple[_PdfLine, ...],
    page_width: float,
    page_height: float,
    rules: tuple[_RasterHorizontalRule, ...],
) -> tuple[tuple[float, ...], tuple[float, ...], bool] | None:
    """Recover a strict open-table grid from an ABBYY full-page scan."""

    if not 2 <= len(rules) <= 3:
        return None
    first = rules[0]
    last = rules[-1]
    widths = tuple(rule.x1 - rule.x0 for rule in rules)
    overlap = min(rule.x1 for rule in rules) - max(rule.x0 for rule in rules)
    if min(widths, default=0.0) <= 0 or overlap / min(widths) < 0.90:
        return None
    outer_left = float(median(rule.x0 for rule in rules))
    outer_right = float(median(rule.x1 for rule in rules))
    area_ratio = (outer_right - outer_left) * (last.top - first.top) / (page_width * page_height)
    if not _OPEN_RASTER_TABLE_MIN_AREA_RATIO <= area_ratio <= _OPEN_RASTER_TABLE_MAX_AREA_RATIO:
        return None

    table_lines = tuple(
        line for line in lines if first.top < (line.top + line.bottom) / 2 < last.top
    )
    side_by_side = _side_by_side_line_indexes(table_lines, page_width)
    if (
        len(side_by_side) < _OPEN_RASTER_TABLE_MIN_SIDE_BY_SIDE_LINES
        or len(side_by_side) / max(1, len(table_lines)) < _OPEN_RASTER_TABLE_MIN_SIDE_BY_SIDE_RATIO
    ):
        return None

    sparse_matrix = len(rules) == 3
    if sparse_matrix:
        vertical = _header_gutter_table_boundaries(table_lines, page_width, rules)
        if vertical is None:
            return None
    else:
        middle = _RasterHorizontalRule(
            outer_left,
            float((first.top + last.top) / 2),
            outer_right,
        )
        repeated = _spatial_table_boundaries(
            table_lines,
            side_by_side,
            page_width,
            page_height,
            (first, middle, last),
        )
        if repeated is None or len(repeated[0]) != 3:
            return None
        vertical = repeated[0]

    horizontal_result = _open_table_horizontal_boundaries(page, vertical, rules)
    if horizontal_result is None:
        return None
    horizontal, labelled_row_ratio = horizontal_result
    if len(horizontal) - 1 < 4:
        return None
    if not sparse_matrix and labelled_row_ratio < 0.75:
        return None
    return vertical, horizontal, sparse_matrix


def _header_gutter_table_boundaries(
    lines: tuple[_PdfLine, ...],
    page_width: float,
    rules: tuple[_RasterHorizontalRule, ...],
) -> tuple[float, ...] | None:
    """Infer matrix columns only from gutters repeated across two header lines."""

    if len(rules) != 3:
        return None
    candidates: list[tuple[float, float]] = []
    for line in lines:
        center_y = (line.top + line.bottom) / 2
        if not rules[0].top < center_y < rules[1].top:
            continue
        characters = sorted(
            (
                character
                for character in line.chars
                if character.text and not character.text.isspace()
            ),
            key=lambda character: character.x0,
        )
        minimum_gap = max(page_width * 0.012, line.font_size * 0.80)
        for previous, current in zip(characters, characters[1:], strict=False):
            if current.x0 - previous.x1 >= minimum_gap:
                candidates.append(((previous.x1 + current.x0) / 2, line.top))
    if not candidates:
        return None

    tolerance = page_width * 0.015
    clusters: list[list[tuple[float, float]]] = []
    for candidate in sorted(candidates):
        if not clusters or candidate[0] - median(item[0] for item in clusters[-1]) > tolerance:
            clusters.append([candidate])
        else:
            clusters[-1].append(candidate)
    internal = tuple(
        float(median(item[0] for item in cluster))
        for cluster in clusters
        if len({round(item[1], 1) for item in cluster}) >= 2
    )
    table_lines = tuple(
        line for line in lines if rules[0].top < (line.top + line.bottom) / 2 < rules[-1].top
    )
    vertical = (
        min(
            float(median(rule.x0 for rule in rules)),
            min((line.x0 for line in table_lines), default=page_width),
        ),
        *internal,
        max(
            float(median(rule.x1 for rule in rules)),
            max((line.x1 for line in table_lines), default=0.0),
        ),
    )
    if not 5 <= len(vertical) <= 9 or any(
        left >= right for left, right in zip(vertical, vertical[1:], strict=False)
    ):
        return None
    return vertical


def _open_table_horizontal_boundaries(
    page: Any,
    vertical: tuple[float, ...],
    rules: tuple[_RasterHorizontalRule, ...],
) -> tuple[tuple[float, ...], float] | None:
    """Split open-table rows only at clear whitespace in the first column."""

    inferred: list[float] = []
    row_starts: list[dict[str, Any]] = []
    for upper, lower in zip(rules, rules[1:], strict=False):
        try:
            first_column_crop = page.crop(
                (vertical[0], upper.top, vertical[1], lower.top),
                strict=False,
            )
            raw_lines = sorted(
                first_column_crop.extract_text_lines(strip=True, return_chars=True),
                key=lambda line: float(line["top"]),
            )
            full_width_lines = sorted(
                page.crop(
                    (vertical[0], upper.top, vertical[-1], lower.top),
                    strict=False,
                ).extract_text_lines(strip=True, return_chars=True),
                key=lambda line: float(line["top"]),
            )
        except (PdfminerException, KeyError, OSError, TypeError, ValueError):
            return None
        raw_lines = [line for line in raw_lines if _normalize_text(str(line.get("text", "")))]
        full_width_lines = [
            line for line in full_width_lines if _normalize_text(str(line.get("text", "")))
        ]
        if not raw_lines:
            return None
        sizes = [
            float(character.get("size", 0))
            for line in raw_lines
            for character in (line.get("chars") or ())
            if float(character.get("size", 0)) > 0
        ]
        minimum_gap = max(3.5, (median(sizes) if sizes else 7.0) * 0.55)
        segment_starts = [raw_lines[0]]
        for previous, current in zip(raw_lines, raw_lines[1:], strict=False):
            gap = float(current["top"]) - float(previous["bottom"])
            if gap >= minimum_gap:
                segment_starts.append(current)
        row_starts.extend(segment_starts)
        row_tolerance = max(2.0, (median(sizes) if sizes else 7.0) * 0.35)
        for current, following in zip(segment_starts, segment_starts[1:], strict=False):
            current_top = float(current["top"])
            following_top = float(following["top"])
            previous_bottom = max(
                (
                    float(line["bottom"])
                    for line in full_width_lines
                    if float(line["top"]) >= current_top - row_tolerance
                    and float(line["top"]) < following_top - 1.0
                ),
                default=float(current["bottom"]),
            )
            if previous_bottom >= following_top:
                return None
            inferred.append(float((previous_bottom + following_top) / 2))

    horizontal = tuple(sorted((*(rule.top for rule in rules), *inferred)))
    if len(horizontal) - 1 > _MAX_PDF_TABLE_ROWS or any(
        top >= bottom for top, bottom in zip(horizontal, horizontal[1:], strict=False)
    ):
        return None
    labelled = sum(_raw_table_line_is_label(line) for line in row_starts)
    return horizontal, labelled / max(1, len(row_starts))


def _restore_visual_matrix_placeholders(
    page: Any,
    rows: tuple[tuple[str, ...], ...],
    vertical: tuple[float, ...],
    horizontal: tuple[float, ...],
) -> tuple[tuple[str, ...], ...]:
    """Restore dash placeholders visible in the scan but absent from hidden OCR text."""

    if (
        len(rows) + 1 != len(horizontal)
        or not rows
        or any(len(row) + 1 != len(vertical) for row in rows)
    ):
        return rows
    try:
        image = page.to_image(resolution=144, antialias=True).original.convert("L")
        page_width = float(page.width)
        page_height = float(page.height)
    except (OSError, TypeError, ValueError):
        return rows
    if page_width <= 0 or page_height <= 0:
        return rows

    scale_x = image.width / page_width
    scale_y = image.height / page_height
    restored = [list(row) for row in rows]
    for row_index in range(1, len(rows)):
        for column_index in range(1, len(rows[row_index])):
            if rows[row_index][column_index]:
                continue
            bbox = (
                vertical[column_index],
                horizontal[row_index],
                vertical[column_index + 1],
                horizontal[row_index + 1],
            )
            if _raster_cell_has_short_horizontal_mark(image, bbox, scale_x, scale_y):
                restored[row_index][column_index] = "—"
    return tuple(tuple(row) for row in restored)


def _repair_consensus_degree_markers(
    rows: tuple[tuple[str, ...], ...],
) -> tuple[tuple[str, ...], ...]:
    """Repair one hidden-OCR degree glyph only when peer headers establish the notation."""

    if not rows or len(rows[0]) < 4:
        return rows
    header = rows[0]
    confirmed = sum(bool(re.search(r"(?<!\d)\d{1,2}°\s*[A-Z]{3,}\b", cell)) for cell in header[1:])
    if confirmed < max(2, (len(header) - 1) // 2):
        return rows

    repaired_header: list[str] = []
    for cell in header:
        repaired = re.sub(
            r"(?<!\d)(?P<degree>[0-2]?\d)0(?=\s*[A-Z]{3,}\b)",
            lambda match: f"{match.group('degree')}°",
            cell,
        )
        repaired_header.append(re.sub(r"(?<=°)(?=[A-Z])", " ", repaired))
    if tuple(repaired_header) == header:
        return rows
    return (tuple(repaired_header), *rows[1:])


def _raster_cell_has_short_horizontal_mark(
    image: Image.Image,
    bbox: tuple[float, float, float, float],
    scale_x: float,
    scale_y: float,
) -> bool:
    """Recognize one small dash component while excluding the table's outer rules."""

    x0, top, x1, bottom = bbox
    inset_x = min(2.0, max(0.75, (x1 - x0) * 0.025))
    inset_y = min(2.0, max(0.75, (bottom - top) * 0.025))
    crop_box = (
        max(0, round((x0 + inset_x) * scale_x)),
        max(0, round((top + inset_y) * scale_y)),
        min(image.width, round((x1 - inset_x) * scale_x)),
        min(image.height, round((bottom - inset_y) * scale_y)),
    )
    if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
        return False
    crop = image.crop(crop_box)
    width, height = crop.size
    pixels = crop.load()
    visited: set[tuple[int, int]] = set()
    for y in range(height):
        for x in range(width):
            if (x, y) in visited or pixels[x, y] >= 155:
                continue
            pending = [(x, y)]
            visited.add((x, y))
            component: list[tuple[int, int]] = []
            while pending:
                current_x, current_y = pending.pop()
                component.append((current_x, current_y))
                for delta_y in (-1, 0, 1):
                    for delta_x in (-1, 0, 1):
                        neighbour = (current_x + delta_x, current_y + delta_y)
                        if (
                            0 <= neighbour[0] < width
                            and 0 <= neighbour[1] < height
                            and neighbour not in visited
                            and pixels[neighbour[0], neighbour[1]] < 155
                        ):
                            visited.add(neighbour)
                            pending.append(neighbour)
            component_width = (
                max(point[0] for point in component) - min(point[0] for point in component) + 1
            )
            component_height = (
                max(point[1] for point in component) - min(point[1] for point in component) + 1
            )
            density = len(component) / (component_width * component_height)
            if (
                6 <= component_width <= max(8, round(width * 0.45))
                and component_height <= max(5, round(component_width * 0.25))
                and component_width >= component_height * 3
                and len(component) >= component_width * 0.60
                and density >= 0.30
            ):
                return True
    return False


def _raw_table_line_is_label(raw_line: dict[str, Any]) -> bool:
    text = _normalize_text(str(raw_line.get("text", "")))
    if _is_uppercase_text(text):
        return True
    characters = tuple(raw_line.get("chars") or ())
    weighted = sum(max(1, len(str(character.get("text", "")))) for character in characters)
    bold = sum(
        max(1, len(str(character.get("text", ""))))
        for character in characters
        if _is_bold_font(str(character.get("fontname", "")))
    )
    return weighted > 0 and bold / weighted >= 0.55


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


def _raster_horizontal_rules(
    page: Any,
    *,
    minimum_width_ratio: float = 0.70,
) -> tuple[_RasterHorizontalRule, ...]:
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
        span = (
            _long_dark_horizontal_span(
                data,
                width,
                height,
                y,
                minimum_width_ratio=minimum_width_ratio,
            )
            if y < height
            else None
        )
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
    *,
    minimum_width_ratio: float = 0.70,
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
    if best is None or best[1] - best[0] < width * minimum_width_ratio:
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
    table_row_lines = tuple(
        line for line in lines if rules[0].top < (line.top + line.bottom) / 2 < rules[-1].top
    )
    internal_boundaries: list[float] = []
    minimum_gutter = 0.01
    for left_cluster, right_cluster in zip(clusters, clusters[1:], strict=False):
        substantial_right_lines = tuple(
            line for line in right_cluster if _heading_letter_count(line.text) >= 3
        )
        right_edge = min(line.x0 for line in (substantial_right_lines or tuple(right_cluster)))
        character_edges = [
            character.x1
            for line in table_row_lines
            for character in line.chars
            if character.x0 < right_edge and character.x1 <= right_edge
        ]
        left_edge = (
            max(character_edges) if character_edges else max(line.x1 for line in left_cluster)
        )
        if right_edge - left_edge < minimum_gutter:
            return None
        internal_boundaries.append(float((left_edge + right_edge) / 2))
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
            or not (
                _should_replace_with_ocr(target_page, markdown)
                or _sparse_raster_cover_should_stay_in_image(target_page)
            )
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


def _ordered_list_item_indexes(lines: list[_PdfLine], body_size: float) -> set[int]:
    """Identify only compact, visually consistent sequences starting at one."""
    candidates: list[tuple[int, int, str, _PdfLine]] = []
    for index, line in enumerate(lines):
        match = _ORDERED_LIST_PATTERN.match(line.text)
        if match is None or line.bold or _heading_letter_count(match.group("text")) < 3:
            continue
        candidates.append(
            (
                index,
                int(match.group("number")),
                match.group("marker"),
                line,
            )
        )

    confirmed: set[int] = set()
    run: list[tuple[int, int, str, _PdfLine]] = []

    def confirm_run() -> None:
        if len(run) >= 3:
            confirmed.update(candidate[0] for candidate in run)

    for candidate in candidates:
        index, number, marker, line = candidate
        if number == 1:
            confirm_run()
            run = [candidate]
            continue
        if not run:
            continue
        previous_index, previous_number, previous_marker, previous_line = run[-1]
        first_line = run[0][3]
        consistent = (
            number == previous_number + 1
            and marker == previous_marker
            and index - previous_index <= 6
            and line.top - previous_line.top
            <= max(body_size, previous_line.font_size, line.font_size) * 8
            and abs(line.x0 - first_line.x0) <= max(body_size * 1.75, line.page_width * 0.03)
            and abs(line.font_size - first_line.font_size) <= body_size * 0.25
        )
        if consistent:
            run.append(candidate)
            continue
        confirm_run()
        run = []
    confirm_run()
    return confirmed


def _ordered_list_continuation(
    item_start: _PdfLine,
    previous: _PdfLine,
    current: _PdfLine,
    body_size: float,
    gap_before: float,
) -> bool:
    """Join a wrapped list line only when its hanging indent is explicit."""
    return bool(
        _ORDERED_LIST_PATTERN.match(current.text) is None
        and _BULLET_PATTERN.match(current.text) is None
        and current.x0 - item_start.x0 >= max(body_size * 0.45, 4)
        and current.x0 - item_start.x0 <= current.page_width * 0.12
        and _should_join_lines(previous, current, body_size, gap_before)
    )


def _repair_astrological_series_roman_glyphs(
    pages: list[_PdfPage],
    body_size: float,
) -> list[_PdfPage]:
    """Recover malformed Roman labels only from clean sibling-heading consensus.

    Some embedded PDF fonts expose the visual glyph ``II`` as ``n`` or ``it`` in the text layer.
    A centered italic placement label is repaired only when nearby clean siblings agree, or when a
    preceding decan title proves the current member of the series.  The same fonts can expose
    ``II:`` as ``IE`` in an uppercase decan title; that form is repaired only when the other two
    titles for the sign prove the missing member of the I/II/III series.  Without consensus native
    text is left for review.
    """

    clean_by_sign: defaultdict[str, list[tuple[int, str, _PdfLine]]] = defaultdict(list)
    broken: list[tuple[int, int, re.Match[str], _PdfLine]] = []

    for page_index, page in enumerate(pages):
        for line_index, line in enumerate(page.lines):
            match = _ASTROLOGICAL_SERIES_HEADING_PATTERN.fullmatch(line.text.strip())
            if (
                match is None
                or not line.centered
                or not line.italic
                or line.font_size < body_size * 0.95
            ):
                continue
            series = match.group("series")
            if re.fullmatch(r"I{1,3}", series, re.IGNORECASE) is None:
                broken.append((page_index, line_index, match, line))
                continue
            clean_by_sign[match.group("sign").casefold()].append(
                (page.number, series.upper(), line)
            )

    replacements: dict[tuple[int, int], _PdfLine] = {}
    for page_index, line_index, match, line in broken:
        evidence = [
            series
            for page_number, series, sibling in clean_by_sign[match.group("sign").casefold()]
            if abs(page_number - line.page_number) <= 1
            and abs(sibling.font_size - line.font_size) <= max(1.0, body_size * 0.12)
        ]
        if len(evidence) < 2 or len(set(evidence)) != 1:
            continue
        series = evidence[0]
        start, end = match.span("series")
        repaired_text = f"{line.text[:start]}{series}{line.text[end:]}"
        replacements[(page_index, line_index)] = replace(
            line,
            text=repaired_text,
            chars=(),
            emphasis_spans=(),
        )

    clean_decan_series: defaultdict[str, set[str]] = defaultdict(set)
    decan_anchors: defaultdict[str, list[tuple[int, int, int, str]]] = defaultdict(list)
    broken_decan_titles: defaultdict[str, list[tuple[int, int, re.Match[str], _PdfLine]]] = (
        defaultdict(list)
    )
    for page_index, page in enumerate(pages):
        for line_index, line in enumerate(page.lines):
            if not line.centered or line.font_size < body_size * 1.05:
                continue
            clean_match = _ASTROLOGICAL_DECAN_TITLE_PATTERN.fullmatch(line.text.strip())
            if clean_match is not None:
                sign = clean_match.group("sign").casefold()
                series = clean_match.group("series").upper()
                clean_decan_series[sign].add(series)
                decan_anchors[sign].append((page_index, line_index, page.number, series))
                continue
            letters = tuple(character for character in line.text if character.isalpha())
            if (
                not letters
                or sum(character.isupper() for character in letters) / len(letters) < 0.80
            ):
                continue
            broken_match = _BROKEN_ASTROLOGICAL_DECAN_TITLE_PATTERN.fullmatch(line.text.strip())
            if broken_match is not None:
                broken_decan_titles[broken_match.group("sign").casefold()].append(
                    (page_index, line_index, broken_match, line)
                )

    expected_series = {"I", "II", "III"}
    for sign, candidates in broken_decan_titles.items():
        missing = expected_series - clean_decan_series[sign]
        if len(candidates) != 1 or len(clean_decan_series[sign]) != 2 or len(missing) != 1:
            continue
        page_index, line_index, match, line = candidates[0]
        start, end = match.span("series")
        repaired_series = missing.pop()
        repaired_text = f"{line.text[:start]}{repaired_series}:{line.text[end:]}"
        replacements[(page_index, line_index)] = replace(
            line,
            text=repaired_text,
            chars=(),
            emphasis_spans=(),
        )
        decan_anchors[sign].append((page_index, line_index, line.page_number, repaired_series))

    for page_index, line_index, match, line in broken:
        if (page_index, line_index) in replacements:
            continue
        anchors = sorted(decan_anchors[match.group("sign").casefold()])
        preceding = [
            anchor
            for anchor in anchors
            if (anchor[0], anchor[1]) < (page_index, line_index)
            and line.page_number - anchor[2] <= 12
        ]
        if not preceding:
            continue
        anchor_page_index, anchor_line_index, _anchor_page_number, series = preceding[-1]
        next_anchors = [
            anchor
            for anchor in anchors
            if (anchor[0], anchor[1]) > (anchor_page_index, anchor_line_index)
        ]
        if next_anchors and (page_index, line_index) >= (next_anchors[0][0], next_anchors[0][1]):
            continue
        start, end = match.span("series")
        repaired_text = f"{line.text[:start]}{series}{line.text[end:]}"
        replacements[(page_index, line_index)] = replace(
            line,
            text=repaired_text,
            chars=(),
            emphasis_spans=(),
        )

    if not replacements:
        return pages
    return [
        replace(
            page,
            lines=tuple(
                replacements.get((page_index, line_index), line)
                for line_index, line in enumerate(page.lines)
            ),
        )
        for page_index, page in enumerate(pages)
    ]


def _repair_ordered_list_label_spacing(text: str, native_page_words: set[str]) -> str:
    """Rejoin a tracked label only when its complete word occurs natively nearby."""
    match = _ORDERED_LIST_LABEL_PATTERN.match(text)
    if match is None:
        return text
    compact_label = re.sub(r"[ \t]+", "", match.group("label"))
    if compact_label.casefold() not in native_page_words:
        return text
    return f"{compact_label}{match.group('suffix')}"


def _render_document(
    pages: list[_PdfPage],
    body_size: float,
    heading_sizes: dict[float, int],
    repeated_margins: set[str],
    referenced_pages: set[int],
    ocr_pages: dict[int, str],
    ocr_failed_pages: set[int],
    page_images: dict[int, tuple[PdfEmbeddedResource, ...]] | None = None,
    required_figure_failure_pages: set[int] | None = None,
    on_progress: PdfProgressCallback | None = None,
) -> tuple[str, tuple[PdfReviewIssue, ...]]:
    pages = _repair_astrological_series_roman_glyphs(pages, body_size)
    blocks: list[_MarkdownBlock] = []
    previous_body_line: _PdfLine | None = None
    required_figure_failure_pages = required_figure_failure_pages or set()
    preserved_margin_headings = _preserved_structural_margin_headings(
        pages,
        body_size,
        repeated_margins,
    )
    toc_heading_references, toc_roman_references = _native_toc_heading_references(
        pages,
        body_size,
        heading_sizes,
        repeated_margins,
        preserved_margin_headings,
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
        if page.number in required_figure_failure_pages:
            blocks.append(
                _MarkdownBlock(
                    kind="warning",
                    text=(
                        f"> **Aviso de conversión (página {page.number}):** se detectó una "
                        "figura numerada en un escaneo, pero no se pudo conservar su referencia "
                        "visual. Compárala con el PDF original."
                    ),
                    page_number=page.number,
                )
            )
        if any(table.inferred_from_raster for table in page.tables):
            blocks.append(
                _MarkdownBlock(
                    kind="warning",
                    text=(
                        f"> **Aviso de conversión (página {page.number}):** se ha reconstruido "
                        "una tabla escaneada a partir de su geometría visual y se ha conservado "
                        "una referencia gráfica. Revisa la asociación entre filas y columnas."
                    ),
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
            ocr_markdown = _repair_ocr_spacing_from_native(page, ocr_markdown)
            ocr_markdown = _repair_ocr_degree_marker_consensus(ocr_markdown)
            ocr_markdown = _restore_preserved_structural_ocr_headings(
                page,
                ocr_markdown,
                preserved_margin_headings,
            )
        page_resources = page_images.get(page.number, ()) if page_images else ()
        visual_authority_tables = tuple(
            table
            for table in page.tables
            if _table_requires_visual_fallback(page, table, ocr_markdown)
            and _table_has_visual_crop(table, page_resources)
        )
        if visual_authority_tables:
            blocks.append(
                _MarkdownBlock(
                    kind="warning",
                    text=(
                        f"> **Aviso de conversión (página {page.number}):** la asociación o "
                        "grafía de una tabla escaneada no es demostrable. Se conserva su recorte "
                        "visual y no se publica una cuadrícula textual incierta."
                    ),
                    page_number=page.number,
                )
            )
        visual_text_fallback_lines = _visual_text_fallback_line_keys(
            page,
            ocr_markdown,
            page_resources,
        )
        if visual_text_fallback_lines:
            blocks.append(
                _MarkdownBlock(
                    kind="warning",
                    text=(
                        f"> **Aviso de conversión (página {page.number}):** una o varias "
                        "grafías no se pueden resolver con seguridad comparando el texto del PDF "
                        "y el OCR. Se conservan sus recortes visuales y no se publica una lectura "
                        "textual incierta."
                    ),
                    page_number=page.number,
                )
            )
        visual_only_reason = _visual_only_page_reason(page, ocr_markdown)
        if visual_only_reason is None and any(
            resource.visual_authority for resource in page_resources
        ):
            visual_only_reason = "authoritative_page_visual"
        if visual_only_reason is not None and page_images and page_images.get(page.number):
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
                "pdf_page_preserved_as_image page=%d reason=%s native_letters=%d ocr_letters=%d",
                page.number,
                visual_only_reason,
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
            if _visual_line_key(line) in visual_text_fallback_lines:
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
                preserved_repeated_headings=preserved_margin_headings,
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
        if not toc_page:
            visible_lines = _normalize_page_footnotes(visible_lines, body_size)
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
        active_ordered_list_block: _MarkdownBlock | None = None
        active_ordered_list_start: _PdfLine | None = None
        previous_ordered_list_line: _PdfLine | None = None
        pending_tables = list(
            sorted(
                (table for table in page.tables if table not in visual_authority_tables),
                key=lambda table: table.bbox[1],
            )
        )
        pending_image_insertions = _page_image_insertions(
            page_images.get(page.number, ()) if page_images else (),
            visible_lines,
        )
        toc_entry_lines = [
            line for line in visible_lines if _split_toc_entry_text(line.text) is not None
        ]
        toc_left = min((line.x0 for line in toc_entry_lines), default=0.0)
        ordered_list_indexes = (
            set() if toc_page else _ordered_list_item_indexes(visible_lines, body_size)
        )
        native_page_words = {
            word.casefold()
            for visible_line in visible_lines
            for word in re.findall(r"[^\W\d_]{4,}", visible_line.text, re.UNICODE)
        }
        for line_index, line in enumerate(visible_lines):
            while pending_image_insertions and pending_image_insertions[0][0] <= line_index:
                _append_page_image_resource(blocks, pending_image_insertions.pop(0)[1])
                previous_body_line = None
                previous_in_page = None
                active_ordered_list_block = None
                active_ordered_list_start = None
                previous_ordered_list_line = None
            while pending_tables and pending_tables[0].bbox[1] <= line.top:
                _append_pdf_table(blocks, page.number, pending_tables.pop(0))
                previous_body_line = None
                active_ordered_list_block = None
                active_ordered_list_start = None
                previous_ordered_list_line = None
            gap_before = (
                line.top - previous_in_page.bottom
                if previous_in_page is not None
                else body_size * 2
            )
            level = _heading_level(line, body_size, heading_sizes, gap_before, toc_page)
            if level is None and _visual_line_key(line) in preserved_margin_headings:
                # Consensus proved this is the unique visual opening while matching labels on
                # later pages are running heads. Preserve that structural fact even when the
                # opening label's type size alone resembles body text.
                level = 2
            following_line = (
                visible_lines[line_index + 1] if line_index + 1 < len(visible_lines) else None
            )
            if level is not None and _uppercase_leadin_continues(
                line,
                following_line,
                body_size,
            ):
                level = None
            ordered_match = (
                _ORDERED_LIST_PATTERN.match(line.text)
                if line_index in ordered_list_indexes
                else None
            )
            if ordered_match is not None:
                item_text = _ORDERED_LIST_PREFIX_PATTERN.sub(
                    "",
                    _apply_links(line),
                    count=1,
                ).strip()
                item_text = _repair_ordered_list_label_spacing(item_text, native_page_words)
                item_text = _apply_source_emphasis(line, item_text)
                active_ordered_list_block = _MarkdownBlock(
                    kind="list",
                    text=(
                        f"{ordered_match.group('number')}{ordered_match.group('marker')} "
                        f"{item_text}"
                    ),
                    page_number=page.number,
                    source_line=line,
                )
                blocks.append(active_ordered_list_block)
                active_ordered_list_start = line
                previous_ordered_list_line = line
                previous_body_line = None
            elif (
                active_ordered_list_block is not None
                and active_ordered_list_start is not None
                and previous_ordered_list_line is not None
                and level is None
                and _ordered_list_continuation(
                    active_ordered_list_start,
                    previous_ordered_list_line,
                    line,
                    body_size,
                    gap_before,
                )
            ):
                active_ordered_list_block.text = _join_ordered_list_continuation_text(
                    active_ordered_list_block.text,
                    _apply_links(line),
                    previous_ordered_list_line,
                    line,
                )
                active_ordered_list_block.source_line = line
                previous_ordered_list_line = line
                previous_body_line = None
            elif level is not None:
                _append_heading(blocks, line, level, gap_before)
                previous_body_line = None
                active_ordered_list_block = None
                active_ordered_list_start = None
                previous_ordered_list_line = None
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
                active_ordered_list_block = None
                active_ordered_list_start = None
                previous_ordered_list_line = None
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
                active_ordered_list_block = None
                active_ordered_list_start = None
                previous_ordered_list_line = None
            else:
                rendered = _apply_source_emphasis(
                    line,
                    _escape_literal_markdown_legend(_apply_links(line)),
                )
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
                active_ordered_list_block = None
                active_ordered_list_start = None
                previous_ordered_list_line = None
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
        if (
            ocr_markdown is not None
            and not visual_authority_tables
            and not visual_text_fallback_lines
            and _should_include_ocr_additions(
                page,
                skipped_vertical=skipped_vertical,
                resources=page_resources,
            )
        ):
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

        for _insertion_index, resource in pending_image_insertions:
            _append_page_image_resource(blocks, resource)

        if on_progress is not None:
            on_progress(PdfProgressPhase.STRUCTURING, current, total_pages)

    normalized_blocks = _conservative_container_hierarchy(
        _demote_prose_like_headings(_join_hyphenated_block_continuations(blocks)),
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
        _append_page_image_resource(blocks, resource)


def _append_page_image_resource(
    blocks: list[_MarkdownBlock],
    resource: PdfEmbeddedResource,
) -> None:
    blocks.append(
        _MarkdownBlock(
            kind="raw",
            text=(f"![](<{RESOURCE_REFERENCE_PREFIX}{resource.relative_path.as_posix()}>)"),
            page_number=resource.page_number,
        )
    )


def _page_image_insertions(
    resources: tuple[PdfEmbeddedResource, ...],
    visible_lines: list[_PdfLine],
) -> list[tuple[int, PdfEmbeddedResource]]:
    insertions: list[tuple[int, PdfEmbeddedResource]] = []
    for resource in resources:
        insertion_index = len(visible_lines)
        if resource.bbox is not None:
            x0, _top, x1, bottom = resource.bbox
            caption_candidates: list[tuple[float, int]] = []
            for index, line in enumerate(visible_lines):
                overlap = max(0.0, min(x1, line.x1) - max(x0, line.x0))
                minimum_width = min(max(x1 - x0, 0.0), max(line.x1 - line.x0, 0.0))
                gap = line.top - bottom
                if (
                    _is_numbered_figure_caption(line)
                    and minimum_width > 0
                    and overlap / minimum_width >= 0.45
                    and -line.font_size <= gap <= line.page_height * 0.15
                ):
                    caption_candidates.append((abs(gap), index))
            if caption_candidates:
                insertion_index = min(caption_candidates)[1]
            else:
                following = [
                    (line.top, index)
                    for index, line in enumerate(visible_lines)
                    if line.top >= bottom
                ]
                if following:
                    insertion_index = min(following)[1]
        insertions.append((insertion_index, resource))
    return sorted(
        insertions,
        key=lambda item: (
            item[0],
            item[1].bbox[1] if item[1].bbox is not None else float("inf"),
            item[1].relative_path.as_posix(),
        ),
    )


def _conservative_container_hierarchy(blocks: list[_MarkdownBlock]) -> list[_MarkdownBlock]:
    """Nest explicit chapter labels only when a container has two siblings."""

    normalized = [replace(block) for block in blocks]
    containers = [
        index
        for index, block in enumerate(normalized)
        if block.kind == "heading" and classify_heading_role(block.text.strip()) == "container"
    ]
    if not containers:
        return normalized
    boundaries = [*containers, len(normalized)]
    for start, end in zip(boundaries[:-1], boundaries[1:], strict=True):
        chapter_positions = [
            index
            for index in range(start + 1, end)
            if normalized[index].kind == "heading"
            and classify_heading_role(normalized[index].text.strip()) == "chapter"
        ]
        if len(chapter_positions) >= 2:
            normalized[start].level = 2
            for index in chapter_positions:
                normalized[index].level = 3
    return normalized


def _demote_prose_like_headings(blocks: list[_MarkdownBlock]) -> list[_MarkdownBlock]:
    """Undo a page-local font-size false positive without guessing real short titles."""

    normalized: list[tuple[_MarkdownBlock, bool]] = []
    for original in blocks:
        block = replace(original)
        visible = re.sub(r"[*_~]+", "", block.text)
        sentence_endings = len(re.findall(r"[.!?](?:[*_~]+)?(?:\s|$)", block.text))
        starts_like_numbered_structure = _SECTION_HEADING_PATTERN.match(visible.strip()) is not None
        prose_like = block.kind == "heading" and (
            (len(visible) > _MAX_HEADING_LENGTH * 2 and sentence_endings >= 2)
            or (starts_like_numbered_structure and classify_heading_role(visible.strip()) is None)
        )
        if prose_like:
            block.kind = "paragraph"
            block.level = None
        normalized.append((block, prose_like))

    repaired: list[_MarkdownBlock] = []
    previous_was_demoted = False
    for block, was_demoted in normalized:
        previous = repaired[-1] if repaired else None
        previous_line = previous.source_line if previous is not None else None
        current_line = block.source_line
        if (
            previous is not None
            and previous_was_demoted
            and block.kind == "paragraph"
            and previous_line is not None
            and current_line is not None
            and current_line.text[:1].islower()
            and previous_line.text.rstrip()[-1:] not in ".!?;:"
            and _should_join_lines(
                previous_line,
                current_line,
                max(previous_line.font_size, current_line.font_size),
                current_line.top - previous_line.bottom,
            )
        ):
            previous.text = _join_line_text(
                previous.text,
                block.text,
                previous_line,
                current_line,
            )
            previous.source_line = current_line
            previous.source_pages = tuple(
                dict.fromkeys((*previous.source_pages, *block.source_pages))
            )
            continue
        repaired.append(block)
        previous_was_demoted = was_demoted
    return repaired


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
    if (
        _visual_only_page_reason(page, ocr_markdown) is not None
        or _structured_raster_table_requires_visual(page)
        or _unresolved_raster_table_text(page, ocr_markdown)
        or any(_dense_raster_table_requires_visual(page, table) for table in page.tables)
    ):
        return False
    native_letters = _page_letter_count(page)
    ocr_letters = _heading_letter_count(ocr_markdown)
    native_quality = _native_page_quality(page)
    ocr_quality = _text_quality_score(ocr_markdown)
    if page.has_table and _MARKDOWN_TABLE_PATTERN.search(ocr_markdown):
        if _has_reliable_reflow_table(page, ocr_markdown):
            return False
        return ocr_quality >= max(0.42, native_quality - 0.08) and _table_ocr_is_faithful(
            page,
            ocr_markdown,
        )
    if native_letters < _MIN_USABLE_NATIVE_LETTERS:
        return ocr_letters >= 10 and ocr_quality >= 0.42
    if _has_suspicious_glyph_encoding(page):
        native_text = _native_page_text(page)
        if (
            _suspicious_non_currency_dollar_count(native_text) > 0
            and not _has_suspicious_text_glyph(native_text)
            and _suspicious_numeric_glyph_count(native_text) == 0
        ):
            # A broken embedded font can map accents and inverted punctuation to
            # isolated dollar signs. OCR is useful evidence for those local glyphs,
            # but it must not replace an otherwise useful native page and erase its
            # typographic hierarchy.
            return False
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


def _has_reliable_reflow_table(page: _PdfPage, ocr_markdown: str | None) -> bool:
    return any(
        table.rendering is not _TableRendering.STRUCTURED_TEXT
        and not table.inferred_from_raster
        and not _unresolved_raster_table(page, table, ocr_markdown)
        and not _dense_raster_table_requires_visual(page, table)
        for table in page.tables
    )


def _visual_only_page_reason(page: _PdfPage, ocr_markdown: str | None) -> str | None:
    if _fragmented_graphic_text_should_stay_in_image(page, ocr_markdown):
        return "fragmented_graphic_text"
    return None


def _sparse_raster_cover_should_stay_in_image(page: _PdfPage) -> bool:
    return bool(
        page.number == 1
        and not page.has_table
        and any(ratio >= _FULL_PAGE_IMAGE_AREA_RATIO for ratio in page.image_area_ratios)
        and _page_letter_count(page) < 4
        and not any(line.links for line in page.lines)
    )


def _unresolved_raster_table_text(page: _PdfPage, ocr_markdown: str | None) -> bool:
    """Prefer the scan when neither text layer proves typographic table details."""
    if (
        not page.has_table
        or not page.tables
        or not ocr_markdown
        or not any(ratio >= _FULL_PAGE_IMAGE_AREA_RATIO for ratio in page.image_area_ratios)
    ):
        return False

    return any(_unresolved_raster_table(page, table, ocr_markdown) for table in page.tables)


def _unresolved_raster_table(
    page: _PdfPage,
    table: _PdfTable,
    ocr_markdown: str | None,
) -> bool:
    if not ocr_markdown or not any(
        ratio >= _FULL_PAGE_IMAGE_AREA_RATIO for ratio in page.image_area_ratios
    ):
        return False

    table_text = "\n".join(cell for row in table.rows for cell in row if cell)
    native_atoms = tuple(atom for atom in _visual_atoms(table_text) if atom.isalpha())
    ocr_atoms = tuple(atom for atom in _visual_atoms(ocr_markdown) if atom.isalpha())
    exact_ocr_atoms = set(ocr_atoms)
    native_atom_keys = Counter(atom.casefold() for atom in native_atoms)
    for native_atom in native_atoms:
        if native_atom in exact_ocr_atoms:
            continue
        native_base = _diacritic_free_key(native_atom)
        for ocr_atom in ocr_atoms:
            ocr_base = _diacritic_free_key(ocr_atom)
            if native_base == ocr_base:
                loses_diacritic = _has_diacritic(native_atom) != _has_diacritic(ocr_atom)
                changes_acronym_case = (
                    len(native_atom) >= 3
                    and native_atom.casefold() == ocr_atom.casefold()
                    and (
                        (native_atom.islower() and ocr_atom.isupper())
                        or (native_atom.isupper() and ocr_atom.islower())
                    )
                )
                if loses_diacritic or changes_acronym_case:
                    return True
            if (
                re.search(r"(?<=[a-z])[A-Z]", native_atom)
                and len(native_atom) - len(ocr_atom) in {1, 2}
                and native_atom.casefold().endswith(ocr_atom.casefold())
            ):
                return True
            if (
                min(len(native_atom), len(ocr_atom)) >= 5
                and abs(len(native_atom) - len(ocr_atom)) <= 1
                and _levenshtein_distance(native_atom.casefold(), ocr_atom.casefold()) == 1
                and native_atom_keys[ocr_atom.casefold()] >= 1
            ):
                return True
    return False


def _structured_raster_table_requires_visual(page: _PdfPage) -> bool:
    return bool(
        any(ratio >= _FULL_PAGE_IMAGE_AREA_RATIO for ratio in page.image_area_ratios)
        and any(table.rendering is _TableRendering.STRUCTURED_TEXT for table in page.tables)
    )


def _structured_raster_table_has_visual_crop(
    page: _PdfPage,
    table: _PdfTable,
    resources: tuple[PdfEmbeddedResource, ...],
) -> bool:
    return bool(
        table.rendering is _TableRendering.STRUCTURED_TEXT
        and _structured_raster_table_requires_visual(page)
        and _table_has_visual_crop(table, resources)
    )


def _table_requires_visual_fallback(
    page: _PdfPage,
    table: _PdfTable,
    ocr_markdown: str | None,
) -> bool:
    return bool(
        (
            table.rendering is _TableRendering.STRUCTURED_TEXT
            and _structured_raster_table_requires_visual(page)
        )
        or _dense_raster_table_requires_visual(page, table)
        or _unresolved_raster_table(page, table, ocr_markdown)
    )


def _table_has_visual_crop(
    table: _PdfTable,
    resources: tuple[PdfEmbeddedResource, ...],
) -> bool:
    return any(
        resource.bbox is not None and _bbox_overlap_ratio(table.bbox, resource.bbox) >= 0.90
        for resource in resources
    )


def _dense_raster_table_requires_visual(page: _PdfPage, table: _PdfTable) -> bool:
    column_count = max((len(row) for row in table.rows), default=0)
    return bool(
        any(ratio >= _FULL_PAGE_IMAGE_AREA_RATIO for ratio in page.image_area_ratios)
        and len(table.rows) >= 20
        and column_count >= 4
    )


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
    if _dense_graphic_label_mosaic(page, ocr_lines):
        return True
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


def _dense_graphic_label_mosaic(
    page: _PdfPage,
    ocr_lines: tuple[str, ...],
) -> bool:
    if len(ocr_lines) < 16 or _page_letter_count(page) >= 4:
        return False
    compact_lines = tuple(" ".join(line.split()) for line in ocr_lines)
    short_line_ratio = sum(len(line) <= 28 for line in compact_lines) / len(compact_lines)
    short_word_ratio = sum(len(line.split()) <= 3 for line in compact_lines) / len(compact_lines)
    sentence_ratio = sum(
        len(line.split()) >= 5 and line.rstrip().endswith((".", "!", "?")) for line in compact_lines
    ) / len(compact_lines)
    graphic_signal_ratio = sum(
        any(character.isdigit() for character in line)
        or bool(
            (letters := [character for character in line if character.isalpha()])
            and sum(character.isupper() for character in letters) / len(letters) >= 0.65
        )
        for line in compact_lines
    ) / len(compact_lines)
    return bool(
        short_line_ratio >= 0.70
        and short_word_ratio >= 0.70
        and sentence_ratio <= 0.15
        and graphic_signal_ratio >= 0.45
    )


def _should_include_ocr_additions(
    page: _PdfPage,
    *,
    skipped_vertical: bool,
    resources: tuple[PdfEmbeddedResource, ...],
) -> bool:
    return not (page.image_orientation_mismatch and skipped_vertical and bool(resources))


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
    return bool(
        _has_suspicious_text_glyph(text)
        or _suspicious_glyph_count(text) >= 3
        or _suspicious_non_currency_dollar_count(text) >= 1
    )


def _has_suspicious_text_glyph(text: str) -> bool:
    """Find explicit replacement glyphs or a broken punctuation pair seen in native layers."""

    return "\ufffd" in text or "·." in text


def _has_suspicious_numeric_glyph_encoding(page: _PdfPage) -> bool:
    """Find broken font mappings inside numeric TOC tokens without distrusting prose."""

    for line in page.lines:
        if line.rotated:
            continue
        if _invalid_zodiac_degree_matches(line.text):
            return True
        matches = _suspicious_numeric_glyph_matches(line.text)
        if not matches:
            continue
        if (
            re.search(r"\d[$^]|[$^]\d", line.text)
            or any(len(_numeric_glyph_candidates(match.group(0))) == 1 for match in matches)
            or any(_looks_like_broken_numeric_token(match.group(0)) for match in matches)
        ):
            return True
    return False


def _suspicious_numeric_glyph_matches(text: str) -> tuple[re.Match[str], ...]:
    """Return numeric-looking damage outside ordinary identifiers and literal URLs."""

    url_spans = tuple(match.span() for match in _LITERAL_URL_PATTERN.finditer(text))
    matches: list[re.Match[str]] = []
    for match in _SUSPICIOUS_NUMERIC_GLYPH_PATTERN.finditer(text):
        if any(start <= match.start() and match.end() <= end for start, end in url_spans):
            continue
        token = match.group(0)
        if re.fullmatch(r"\$\d+", token):
            continue
        if re.search(r"\d[$^]|[$^]\d", token) or _looks_like_broken_numeric_token(token):
            matches.append(match)
    return tuple(matches)


def _looks_like_broken_numeric_token(token: str) -> bool:
    """Require every letter in a mixed token to be a known number-shaped glyph."""

    if not (
        any(character.isdigit() for character in token)
        and any(character.isalpha() for character in token)
    ):
        return False
    letters = tuple(character for character in token if character.isalpha())
    if not letters or any(character not in _NUMERIC_GLYPH_EXPANSIONS for character in letters):
        return False
    candidates = _numeric_glyph_candidates(token)
    if token[0].isalpha() and token[-1].isalpha():
        return any(len(candidate) == len(token) for candidate in candidates)
    return bool(candidates)


def _line_has_suspicious_numeric_glyph(line: _PdfLine) -> bool:
    return not line.rotated and bool(
        _suspicious_numeric_glyph_matches(line.text) or _invalid_zodiac_degree_matches(line.text)
    )


def _suspicious_numeric_glyph_count(text: str) -> int:
    return len(_suspicious_numeric_glyph_matches(text)) + len(_invalid_zodiac_degree_matches(text))


def _invalid_zodiac_degree_matches(text: str) -> tuple[re.Match[str], ...]:
    """Return impossible within-sign degrees that require independent visual confirmation."""

    return tuple(_INVALID_ZODIAC_DEGREE_PATTERN.finditer(text))


def _ocr_pages_safe_for_structural_rendering(
    pages: list[_PdfPage],
    ocr_pages: dict[int, str],
) -> dict[int, str]:
    """Keep inconclusive OCR as private evidence instead of reader-visible structure.

    OCR still participates in reconciliation, image preservation, and quality reporting before
    this boundary. When an impossible zodiac degree remains unresolved on a page whose useful
    native layer wins over whole-page OCR, allowing that same OCR into structural rendering can
    turn diagram labels into duplicate prose or lists. The native text and a review warning are
    safer than publishing those OCR-only additions.
    """

    safe_pages = dict(ocr_pages)
    for page in pages:
        ocr_markdown = ocr_pages.get(page.number)
        if (
            ocr_markdown is None
            or _should_replace_with_ocr(page, ocr_markdown)
            or not any(
                _invalid_zodiac_degree_matches(line.text) for line in page.lines if not line.rotated
            )
        ):
            continue
        safe_pages.pop(page.number, None)
        LOGGER.info(
            "pdf_ocr_quarantined_from_structure page=%d reason=unresolved_numeric_glyph",
            page.number,
        )
    return safe_pages


def _suspicious_non_currency_dollar_count(text: str) -> int:
    """Count isolated dollar-like substitutions without distrusting amounts or runs."""

    count = 0
    for index, character in enumerate(text):
        if character != "$":
            continue
        previous = text[index - 1] if index else ""
        following = text[index + 1] if index + 1 < len(text) else ""
        if previous == "$" or following == "$":
            continue
        previous_visible = next(
            (candidate for candidate in reversed(text[:index]) if not candidate.isspace()),
            "",
        )
        following_visible = next(
            (candidate for candidate in text[index + 1 :] if not candidate.isspace()),
            "",
        )
        if previous_visible.isdigit() or following_visible.isdigit():
            continue
        count += 1
    return count


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
        + _suspicious_non_currency_dollar_count(visible)
        + _suspicious_numeric_glyph_count(visible)
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
    native_sequence = _ocr_addition_token_sequence(native_text)
    native_tokens = set(native_sequence)
    additions: list[str] = []
    seen_additions: set[tuple[str, ...]] = set()
    for block in re.split(r"\n\s*\n", ocr_markdown):
        stripped = block.strip()
        if not stripped:
            continue
        block_sequence = _ocr_addition_token_sequence(stripped)
        block_tokens = set(block_sequence)
        alpha_tokens = {token for token in block_tokens if any(char.isalpha() for char in token)}
        is_table = bool(_MARKDOWN_TABLE_PATTERN.search(stripped))
        overlap = block_tokens & native_tokens
        ordered_copy = _is_ordered_ocr_copy(
            block_sequence,
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


def _ocr_addition_token_sequence(text: str) -> tuple[str, ...]:
    """Compare OCR additions without mistaking omitted diacritics for new prose."""

    return tuple(_diacritic_free_key(token) for token in _comparison_token_sequence(text))


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
    visible = _escape_literal_markdown_legend(visible)
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
    if line.outline_level is not None:
        blocks.append(
            _MarkdownBlock(
                kind="provenance",
                text=_pdf_outline_marker(line.outline_level),
                page_number=line.page_number,
            )
        )
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
    applied = False
    for span in reversed(line.emphasis_spans):
        fragment = span.text
        if "*" in fragment or line.text.count(fragment) != 1 or rendered.count(fragment) != 1:
            continue
        marker = "***" if span.bold and span.italic else "**" if span.bold else "*"
        position = rendered.find(fragment)
        rendered = (
            rendered[:position] + marker + fragment + marker + rendered[position + len(fragment) :]
        )
        applied = True
    if applied or line.emphasis_spans:
        return rendered
    marker = _source_emphasis_marker(line)
    return f"{marker}{rendered}{marker}" if marker is not None else rendered


def _source_emphasis_marker(line: _PdfLine) -> str | None:
    if _heading_letter_count(line.text) < 4:
        return None
    if line.bold and line.italic:
        return "***"
    if line.bold:
        return "**"
    if line.italic:
        return "*"
    return None


def _join_ordered_list_continuation_text(
    existing: str,
    following: str,
    previous_line: _PdfLine,
    current_line: _PdfLine,
) -> str:
    previous_marker = _source_emphasis_marker(previous_line)
    current_marker = _source_emphasis_marker(current_line)
    stripped = existing.rstrip()
    if (
        previous_marker is not None
        and previous_marker == current_marker
        and stripped.endswith(previous_marker)
    ):
        joined = _join_line_text(
            stripped[: -len(previous_marker)],
            following,
            previous_line,
            current_line,
        )
        return f"{joined}{previous_marker}"
    return _join_line_text(
        existing,
        _apply_source_emphasis(current_line, following),
        previous_line,
        current_line,
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
    same_page = previous.page_number == current.page_number
    wraps_word = previous.soft_hyphen_end or (
        previous.hard_hyphen_end and _hard_hyphen_wraps_word(previous.text, current.text)
    )
    if same_page and wraps_word and not _lines_share_text_column(previous, current):
        return False
    if previous.soft_hyphen_end:
        return True
    if previous.hard_hyphen_end and _hard_hyphen_wraps_word(previous.text, current.text):
        return True

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


def _lines_share_text_column(previous: _PdfLine, current: _PdfLine) -> bool:
    width = min(max(previous.x1 - previous.x0, 0.0), max(current.x1 - current.x0, 0.0))
    overlap = max(0.0, min(previous.x1, current.x1) - max(previous.x0, current.x0))
    return abs(previous.x0 - current.x0) <= current.page_width * 0.08 or (
        width > 0 and overlap / width >= 0.35
    )


def _join_line_text(
    existing: str,
    following: str,
    previous_line: _PdfLine,
    current_line: _PdfLine,
) -> str:
    left = existing.rstrip()
    right = following.lstrip()
    if previous_line.soft_hyphen_end:
        joined = _join_adjacent_emphasis_edges(left, right, drop_hyphen=False) or f"{left}{right}"
    elif previous_line.hard_hyphen_end and _hard_hyphen_wraps_word(
        previous_line.text, current_line.text
    ):
        emphasized = _join_adjacent_emphasis_edges(left, right, drop_hyphen=True)
        if emphasized is not None:
            joined = emphasized
        elif left.endswith("-"):
            joined = _carry_continuation_emphasis(left[:-1], right) or f"{left[:-1]}{right}"
        else:
            joined = f"{left} {right}"
    else:
        joined = f"{left} {right}"
    return _collapse_adjacent_links(joined)


def _join_adjacent_emphasis_edges(left: str, right: str, *, drop_hyphen: bool) -> str | None:
    for marker in ("***", "**", "*"):
        suffix = f"-{marker}" if drop_hyphen else marker
        if left.endswith(suffix) and right.startswith(marker):
            return f"{left[: -len(suffix)]}{right[len(marker) :]}"
    return None


def _carry_continuation_emphasis(left: str, right: str) -> str | None:
    for marker in ("***", "**", "*"):
        if not right.startswith(marker) or marker not in right[len(marker) :]:
            continue
        prefix, separator, fragment = left.rpartition(" ")
        return f"{prefix}{separator}{marker}{fragment}{right[len(marker) :]}"
    return None


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
    if len(text) > _MAX_HEADING_LENGTH or letter_count < 3 or text.startswith(("-", "–", "—", "―")):
        return None
    if line.outline_level is not None:
        return max(1, min(6, line.outline_level))
    if text.endswith((".", ";")):
        return None
    if _TOC_HEADING_PATTERN.fullmatch(text):
        return 2
    if toc_page and _toc_entry_page_number(text) is not None:
        return None
    if classify_heading_role(text) is not None:
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


def _uppercase_leadin_continues(
    line: _PdfLine,
    following: _PdfLine | None,
    body_size: float,
) -> bool:
    """Keep a small-caps sentence opening inside its paragraph."""

    if (
        following is None
        or not _is_uppercase_text(line.text)
        or not following.text[:1].islower()
        or line.font_size > body_size * 1.08
        or _SECTION_HEADING_PATTERN.match(line.text.strip())
    ):
        return False
    gap = following.top - line.bottom
    return _lines_share_text_column(line, following) and _should_join_lines(
        line,
        following,
        body_size,
        gap,
    )


def _omit_margin_line(
    line: _PdfLine,
    repeated_margins: set[str],
    previous: _PdfLine | None,
    body_size: float,
    *,
    toc_page: bool = False,
    preserved_repeated_headings: frozenset[tuple[int, float, float, str]] = frozenset(),
) -> bool:
    if _visual_line_key(line) in preserved_repeated_headings:
        return False
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
    in_top_repeated_header_band = (
        line.top <= line.page_height * 0.18 and _margin_key(line.text) in repeated_margins
    )
    in_bottom_margin = line.bottom >= line.page_height * 0.84
    if not (
        in_top_margin
        or in_top_folio_band
        or in_top_numbered_running_header
        or in_top_repeated_header_band
        or in_bottom_margin
    ):
        return False

    text = line.text.strip()
    if in_top_numbered_running_header:
        return True
    if in_top_folio_band and _is_plausible_isolated_confusable_folio(line, body_size):
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


def _preserved_structural_margin_headings(
    pages: list[_PdfPage],
    body_size: float,
    repeated_margins: set[str],
) -> frozenset[tuple[int, float, float, str]]:
    """Retain a repeated Chapter label only on its visually proven opening page."""

    candidates_by_label: dict[str, list[_PdfLine]] = defaultdict(list)
    for page in pages:
        for line in page.lines:
            margin_key = _margin_key(line.text)
            if (
                line.rotated
                # A true chapter opener can be deliberately inset below the running-head band.
                # It is still safe to consider here because the label must repeat elsewhere and
                # be followed by a prominent title; only the earliest proved occurrence survives.
                or line.top > line.page_height * 0.30
                or margin_key not in repeated_margins
                or _SECTION_HEADING_PATTERN.fullmatch(line.text.strip()) is None
            ):
                continue
            prominent_title_below = any(
                candidate is not line
                and not candidate.rotated
                and candidate.top >= line.bottom
                and candidate.top <= line.page_height * 0.42
                and candidate.font_size >= body_size * 1.35
                and _heading_letter_count(candidate.text) >= 4
                for candidate in page.lines
            )
            if prominent_title_below:
                candidates_by_label[margin_key].append(line)
    return frozenset(
        _visual_line_key(min(candidates, key=lambda line: (line.page_number, line.top, line.x0)))
        for candidates in candidates_by_label.values()
    )


def _restore_preserved_structural_ocr_headings(
    page: _PdfPage,
    markdown: str,
    preserved_headings: frozenset[tuple[int, float, float, str]],
) -> str:
    """Carry only visually proved native Chapter structure into a whole-page OCR winner."""

    label_keys = {
        _margin_key(line.text)
        for line in page.lines
        if _visual_line_key(line) in preserved_headings
        and _SECTION_HEADING_PATTERN.fullmatch(line.text.strip()) is not None
    }
    if not label_keys:
        return markdown

    lines = markdown.splitlines()
    fence: str | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        marker = (
            "```" if stripped.startswith("```") else "~~~" if stripped.startswith("~~~") else None
        )
        if marker is not None:
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is not None or not stripped or stripped.startswith("#"):
            continue
        visible = re.sub(r"[*_`~]", "", stripped).strip()
        if (
            _margin_key(visible) in label_keys
            and _SECTION_HEADING_PATTERN.fullmatch(visible) is not None
        ):
            lines[index] = f"## {stripped}"
    return "\n".join(lines)


def _repair_ocr_spacing_from_native(page: _PdfPage, markdown: str) -> str:
    """Restore spacing only when an OCR line has exactly the native line's characters."""

    native_words = {
        word.casefold()
        for line in page.lines
        if not line.rotated
        for word in re.findall(r"[^\W\d_]{5,}", line.text, re.UNICODE)
    }
    candidates: defaultdict[str, list[_PdfLine]] = defaultdict(list)
    for line in page.lines:
        key = _spacing_insensitive_line_key(line.text)
        if not line.rotated and len(key) >= 12:
            candidates[key].append(line)

    lines = markdown.splitlines()
    fence: str | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        marker = (
            "```" if stripped.startswith("```") else "~~~" if stripped.startswith("~~~") else None
        )
        if marker is not None:
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if (
            fence is not None
            or not stripped
            or stripped.startswith(("#", ">", "|", "![", "- ", "+ ", "* "))
        ):
            continue
        compact_line = _spacing_insensitive_text_with_offsets(stripped)
        if compact_line is not None:
            compact, offsets = compact_line
            replacements: list[tuple[int, int, str]] = []
            for key, native in candidates.items():
                if len(native) != 1:
                    continue
                compact_start = compact.find(key)
                if compact_start < 0 or compact.find(key, compact_start + 1) >= 0:
                    continue
                compact_end = compact_start + len(key) - 1
                start = offsets[compact_start]
                end = offsets[compact_end] + 1
                source_segment = stripped[start:end]
                native_text = native[0].text.strip()
                if len(re.findall(r"\s", source_segment)) < len(re.findall(r"\s", native_text)) + 2:
                    continue
                if any(
                    start < previous_end and end > previous_start
                    for previous_start, previous_end, _ in replacements
                ):
                    continue
                replacements.append((start, end, native_text))
            for start, end, replacement in sorted(replacements, reverse=True):
                stripped = f"{stripped[:start]}{replacement}{stripped[end:]}"
            if replacements:
                leading = line[: len(line) - len(line.lstrip())]
                lines[index] = f"{leading}{stripped}"
        repaired_words = stripped
        for pattern in (_SPACED_WORD_PATTERN, _PARTIAL_SPACED_WORD_PATTERN):
            repaired_words = pattern.sub(
                lambda match: (
                    compact
                    if (compact := re.sub(r"\s+", "", match.group())).casefold() in native_words
                    else match.group()
                ),
                repaired_words,
            )
        if repaired_words != stripped:
            leading = line[: len(line) - len(line.lstrip())]
            lines[index] = f"{leading}{repaired_words}"
            stripped = repaired_words
        native = candidates.get(_spacing_insensitive_line_key(stripped), ())
        if len(native) != 1:
            continue
        native_text = native[0].text.strip()
        if len(re.findall(r"\s", stripped)) < len(re.findall(r"\s", native_text)) + 2:
            continue
        leading = line[: len(line) - len(line.lstrip())]
        lines[index] = f"{leading}{native_text}"
    return "\n".join(lines)


def _spacing_insensitive_text_with_offsets(text: str) -> tuple[str, tuple[int, ...]] | None:
    compact: list[str] = []
    offsets: list[int] = []
    for index, character in enumerate(text):
        if character.isspace():
            continue
        normalized = unicodedata.normalize("NFKC", character).casefold()
        if len(normalized) != 1:
            return None
        compact.append(normalized)
        offsets.append(index)
    return "".join(compact), tuple(offsets)


def _repair_ocr_degree_marker_consensus(markdown: str) -> str:
    """Repair a repeated degree-shaped zero only beside independently retained degree notation."""

    sign_after_value = r"\s*(?:\|\s*)?[*_`~]{0,2}[A-ZÁÉÍÓÚÜÑ][^\W\d_]{2,}\b"
    candidate_pattern = re.compile(rf"(?<!\d)(?P<degree>[0-2]?\d)0(?={sign_after_value})")

    def repair_block(match: re.Match[str]) -> str:
        block = match.group(0)
        confirmed = len(
            re.findall(
                rf"(?<!\d)[0-2]?\d°{sign_after_value}",
                block,
            )
        )
        candidates = tuple(candidate_pattern.finditer(block))
        if confirmed < 1 or len(candidates) < 3:
            return block
        return candidate_pattern.sub(lambda value: f"{value.group('degree')}°", block)

    return re.sub(r"(?s)(?:^|(?<=\n\n)).+?(?=\n\n|$)", repair_block, markdown)


def _is_plausible_isolated_confusable_folio(line: _PdfLine, body_size: float) -> bool:
    decoded = _confusable_margin_folio_value(re.sub(r"\s+", "", line.text.strip()))
    return bool(
        decoded is not None
        and line.font_size <= body_size * 0.90
        and abs(decoded - line.page_number) <= 64
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
    if any(
        _has_suspicious_text_glyph(line.text)
        or _suspicious_non_currency_dollar_count(line.text) > 0
        for line in visible_lines
    ):
        return (
            f"> **Aviso de conversión (página {page.number}):** la capa de texto contiene "
            "un glifo ilegible que las lecturas locales no pudieron confirmar. Revisa el PDF "
            "original."
        )
    if any(_line_has_suspicious_numeric_glyph(line) for line in visible_lines):
        return (
            f"> **Aviso de conversión (página {page.number}):** la capa de texto contiene "
            "un glifo numérico ambiguo que el OCR local no pudo confirmar. Revisa el PDF original."
        )
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
    normalized_blocks = _conservative_container_hierarchy(
        _demote_prose_like_headings(_join_hyphenated_block_continuations(blocks)),
    )
    return _normalized_blocks_to_markdown(normalized_blocks)


def _normalized_blocks_to_markdown(blocks: list[_MarkdownBlock]) -> str:
    blocks = _promote_structural_toc_section_rows(blocks)
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


def _promote_structural_toc_section_rows(
    blocks: list[_MarkdownBlock],
) -> list[_MarkdownBlock]:
    """Keep unpaginated part/appendix labels inside an adjacent generated contents table."""

    promoted: list[_MarkdownBlock] = []
    for index, block in enumerate(blocks):
        visible = re.sub(r"[*_`~#\[\]]", " ", block.text)
        visible = re.sub(r"\s+", " ", visible).strip()
        if (
            block.kind not in {"toc", "heading"}
            or not _structural_toc_section_label(block.text)
            or (block.kind == "heading" and _TOC_HEADING_PATTERN.fullmatch(visible))
        ):
            promoted.append(block)
            continue
        adjacent_entry = any(
            0 <= candidate < len(blocks)
            and blocks[candidate].kind == "toc-entry"
            and blocks[candidate].page_number == block.page_number
            for candidate in (index - 1, index + 1)
        )
        promoted.append(
            replace(block, kind="toc-entry", toc_folio=None, toc_level=0)
            if adjacent_entry
            else block
        )
    return promoted


def _structural_toc_section_label(value: str) -> bool:
    visible = re.sub(r"\]\([^)]+\)", "]", value)
    visible = re.sub(r"[*_`~#\[\]]", " ", visible)
    visible = re.sub(r"\s+", " ", visible).strip()
    if not visible:
        return False
    return bool(
        classify_heading_role(visible) == "container"
        or re.fullmatch(
            r"(?:appendices|appendix|ap[eé]ndices|ap[eé]ndice|"
            r"tables?(?:\s+of\s+.+)?|tablas?(?:\s+de\s+.+)?|"
            r"bibliography|bibliograf[ií]a|references|referencias|"
            r"glossary|glosario|index|[ií]ndice)",
            visible,
            re.IGNORECASE,
        )
    )


def _toc_indent_level(line: _PdfLine, left: float, body_size: float) -> int:
    indentation = max(0.0, line.x0 - left)
    unit = max(body_size * 1.8, 10.0)
    return min(2, max(0, round(indentation / unit)))


def _toc_entries_html(blocks: list[_MarkdownBlock]) -> str:
    rows: list[str] = []
    for block in blocks:
        source_line = block.source_line
        emphasis_spans = source_line.emphasis_spans if source_line is not None else ()
        label, inline_emphasis_applied = _toc_label_xhtml(block.text, emphasis_spans)
        if source_line is not None and not emphasis_spans and not inline_emphasis_applied:
            if source_line.italic:
                label = f"<em>{label}</em>"
            if source_line.bold:
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


def _toc_label_xhtml(
    markdown: str,
    emphasis_spans: tuple[_PdfEmphasisSpan, ...] = (),
) -> tuple[str, bool]:
    link = re.fullmatch(r"\[([^\]]+)]\(<?(#page-\d{1,6})>?\)", markdown)
    if link is None:
        return _toc_visible_label_xhtml(markdown, emphasis_spans)
    label, applied = _toc_visible_label_xhtml(link.group(1), emphasis_spans)
    return f'<a href="{escape(link.group(2), quote=True)}">{label}</a>', applied


def _toc_visible_label_xhtml(
    text: str,
    emphasis_spans: tuple[_PdfEmphasisSpan, ...],
) -> tuple[str, bool]:
    matches: list[tuple[int, int, _PdfEmphasisSpan]] = []
    for span in emphasis_spans:
        if text.count(span.text) != 1:
            continue
        start = text.find(span.text)
        matches.append((start, start + len(span.text), span))
    matches.sort(key=lambda item: item[0])
    if any(
        current[0] < previous[1] for previous, current in zip(matches, matches[1:], strict=False)
    ):
        return escape(text), False
    output: list[str] = []
    cursor = 0
    for start, end, span in matches:
        output.append(escape(text[cursor:start]))
        fragment = escape(text[start:end])
        if span.italic:
            fragment = f"<em>{fragment}</em>"
        if span.bold:
            fragment = f"<strong>{fragment}</strong>"
        output.append(fragment)
        cursor = end
    output.append(escape(text[cursor:]))
    return "".join(output), bool(matches)


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


def _pdf_outline_marker(level: int) -> str:
    """Carry independently confirmed outline evidence without visible reader text."""

    return f"<!-- PZDOC PDF OUTLINE {max(1, min(6, level))} -->"


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
    if current.kind == "heading" and not (
        current_line.soft_hyphen_end or current_line.hard_hyphen_end
    ):
        # A complete heading is a structural boundary.  The heading exception
        # only exists for a wrapped prose fragment that was misclassified as a
        # heading; require the second fragment to continue wrapping as well.
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


def _escape_literal_markdown_legend(text: str) -> str:
    """Keep a printed ``* =`` legend from becoming a Markdown list item."""

    return re.sub(r"^([ \t]{0,3})([*+-])(?=[ \t]*=[ \t])", r"\1\\\2", text)


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
    normalized = _normalize_text(text).casefold()
    return re.sub(r"(?<=\d)\s+(?=\d)", "", normalized)


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
