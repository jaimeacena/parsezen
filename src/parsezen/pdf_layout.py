"""Internal immutable layout records shared by the local PDF subsystems."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


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
class _PdfEmphasisSpan:
    """One source-confirmed bold/italic range inside a normalized PDF line."""

    text: str
    bold: bool
    italic: bool


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
    italic: bool = False
    emphasis_spans: tuple[_PdfEmphasisSpan, ...] = ()
    outline_level: int | None = None

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
    tables: tuple[_PdfTable, ...] = ()


class _TableRendering(StrEnum):
    MARKDOWN = "markdown"
    HTML = "html"
    STRUCTURED_TEXT = "structured_text"


@dataclass(frozen=True, slots=True)
class _PdfTable:
    bbox: tuple[float, float, float, float]
    rows: tuple[tuple[str, ...], ...]
    rendering: _TableRendering
    inferred_from_raster: bool = False


@dataclass(frozen=True, slots=True)
class _RasterHorizontalRule:
    x0: float
    top: float
    x1: float


@dataclass(slots=True)
class _MarkdownBlock:
    kind: str
    text: str
    page_number: int
    level: int | None = None
    source_line: _PdfLine | None = None
    source_pages: tuple[int, ...] = ()
    toc_folio: str | None = None
    toc_level: int = 0

    def __post_init__(self) -> None:
        """Keep a compact, non-user-visible page provenance for every block."""
        if not self.source_pages:
            self.source_pages = (self.page_number,)
