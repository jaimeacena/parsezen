"""Bounded, schema-validated checkpoints for extracted PDF pages."""

from __future__ import annotations

import json
import math

from parsezen.pdf_layout import (
    _PdfCharacter,
    _PdfLine,
    _PdfLink,
    _PdfPage,
    _PdfTable,
    _TableRendering,
)

_PDF_PAGE_CHECKPOINT_VERSION = 11
_MAX_PDF_PAGE_CHECKPOINT_BYTES = 8 * 1024 * 1024
_MAX_PDF_PAGE_LINES = 50_000
_MAX_PDF_LINE_CHARACTERS = 100_000
_MAX_PDF_LINE_LINKS = 10_000
_MAX_PDF_TABLES = 100
_MAX_PDF_TABLE_ROWS = 500
_MAX_PDF_TABLE_COLUMNS = 32
_MAX_PDF_TABLE_CELL_CHARACTERS = 10_000


def _serialize_page_checkpoint(page: _PdfPage) -> str:
    record = {
        "version": _PDF_PAGE_CHECKPOINT_VERSION,
        "page": page.number,
        "has_images": page.has_images,
        "image_area_ratios": list(page.image_area_ratios),
        "has_table": page.has_table,
        "image_orientation_mismatch": page.image_orientation_mismatch,
        "tables": [
            {
                "bbox": list(table.bbox),
                "rows": [list(row) for row in table.rows],
                "rendering": table.rendering.value,
            }
            for table in page.tables
        ],
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
        "tables",
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
    raw_tables = raw["tables"]
    if (
        not isinstance(raw_ratios, list)
        or len(raw_ratios) > _MAX_PDF_LINE_CHARACTERS
        or not isinstance(raw_lines, list)
        or len(raw_lines) > _MAX_PDF_PAGE_LINES
        or not isinstance(raw_tables, list)
        or len(raw_tables) > _MAX_PDF_TABLES
    ):
        raise ValueError
    ratios = tuple(_checkpoint_float(value, minimum=0, maximum=100) for value in raw_ratios)
    lines = tuple(_line_from_checkpoint(line, page_number) for line in raw_lines)
    page_widths = {line.page_width for line in lines}
    page_heights = {line.page_height for line in lines}
    if len(page_widths) > 1 or len(page_heights) > 1:
        raise ValueError
    page_width = next(iter(page_widths), 0.0)
    page_height = next(iter(page_heights), 0.0)
    tables = tuple(
        _table_from_checkpoint(table, page_width=page_width, page_height=page_height)
        for table in raw_tables
    )
    if tables and (not raw["has_table"] or page_width <= 0 or page_height <= 0):
        raise ValueError
    return _PdfPage(
        number=page_number,
        lines=lines,
        has_images=raw["has_images"],
        image_area_ratios=ratios,
        has_table=raw["has_table"],
        image_orientation_mismatch=raw["image_orientation_mismatch"],
        tables=tables,
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


def _table_from_checkpoint(
    raw: object,
    *,
    page_width: float,
    page_height: float,
) -> _PdfTable:
    if not isinstance(raw, dict) or set(raw) != {"bbox", "rows", "rendering"}:
        raise ValueError
    raw_bbox = raw["bbox"]
    raw_rows = raw["rows"]
    if (
        not isinstance(raw_bbox, list)
        or len(raw_bbox) != 4
        or not isinstance(raw_rows, list)
        or len(raw_rows) > _MAX_PDF_TABLE_ROWS
    ):
        raise ValueError
    bbox = tuple(_checkpoint_float(value, minimum=0, maximum=100_000) for value in raw_bbox)
    if bbox[0] >= bbox[2] or bbox[1] >= bbox[3] or bbox[2] > page_width or bbox[3] > page_height:
        raise ValueError
    rows: list[tuple[str, ...]] = []
    column_count: int | None = None
    for raw_row in raw_rows:
        if (
            not isinstance(raw_row, list)
            or not 1 <= len(raw_row) <= _MAX_PDF_TABLE_COLUMNS
            or (column_count is not None and len(raw_row) != column_count)
        ):
            raise ValueError
        row = tuple(str(cell) for cell in raw_row)
        if any(len(cell) > _MAX_PDF_TABLE_CELL_CHARACTERS for cell in row):
            raise ValueError
        column_count = len(row)
        rows.append(row)
    if len(rows) < 2 or (column_count or 0) < 2:
        raise ValueError
    return _PdfTable(
        (bbox[0], bbox[1], bbox[2], bbox[3]),
        tuple(rows),
        _TableRendering(raw["rendering"]),
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
