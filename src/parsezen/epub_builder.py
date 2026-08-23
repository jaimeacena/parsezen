"""Build a portable EPUB 3 book from Parsezen's Markdown document model."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from html import escape, unescape
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, cast
from urllib.parse import quote, unquote, urlsplit
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile, ZipInfo

from defusedxml import ElementTree as SafeElementTree
from defusedxml.common import DefusedXmlException
from markdown_it import MarkdownIt

from parsezen.cancellation import CancellationToken, check_cancelled
from parsezen.document_model import RESOURCE_REFERENCE_PREFIX, ConvertedResource
from parsezen.errors import ConversionError
from parsezen.final_integrity import FinalIntegrityReport, verify_epub_payload
from parsezen.semantic_blocks import SemanticDocument, SemanticRole, analyze_markdown

LOGGER = logging.getLogger(__name__)

_SAFE_ANCHOR_PATTERN = re.compile(
    r'<a\s+id="([A-Za-z][A-Za-z0-9._:-]{0,127})"\s*>\s*</a>',
    re.IGNORECASE,
)
_EPUB_ANCHOR_COMMENT_PATTERN = re.compile(
    r"<!--\s*PZDOC EPUB ANCHOR ([a-z0-9][a-z0-9-]*)\s*-->",
    re.IGNORECASE,
)
_PDF_PAGE_MARKER_PATTERN = re.compile(
    r"<!--\s*PZDOC PDF PAGE \d+\s*-->",
    re.IGNORECASE,
)
_EXPLICIT_CHAPTER_PATTERN = re.compile(
    r"(?m)^\s*<!--\s*PZDOC EPUB CHAPTER\s*-->\s*$",
    re.IGNORECASE,
)
EPUB_CHAPTER_MARKER = "<!-- PZDOC EPUB CHAPTER -->"
_RESOURCE_PATTERN = re.compile(
    re.escape(RESOURCE_REFERENCE_PREFIX) + r"([^\s)>'\"]+)",
)
_RENDERED_IMAGE_SOURCE_PATTERN = re.compile(r'<img\b[^>]*\bsrc="([^"]+)"', re.IGNORECASE)
_RENDERED_LINK_PATTERN = re.compile(
    r'<a\b(?P<before>[^>]*?)\bhref="(?P<href>[^"]*)"'
    r"(?P<after>[^>]*)>(?P<label>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)
_FENCE_PATTERN = re.compile(r"^\s*(```|~~~)")
_NUMERIC_REFERENCE_LIST_PATTERN = re.compile(
    r"(?m)^(?P<indent>[ \t]{0,3})(?P<marker>\d{1,4})(?P<delimiter>[.)])"
    r"(?P<space>[ \t]+)(?P<references>[^\r\n]*)$"
)
_NUMERIC_REFERENCE_TOKEN_PATTERN = re.compile(r"\d+(?:[.,:/-]\d+)*")
_RAW_TABLE_BLOCK_PATTERN = re.compile(
    r"<table(?:\s+[^>]*)?>.*?</table>",
    re.IGNORECASE | re.DOTALL,
)
_DOCUMENT_TOC_OPENING_PATTERN = re.compile(
    r'^\s*<table\s+class=["\']document-toc["\']\s*>',
    re.IGNORECASE,
)
_BARE_XML_AMPERSAND_PATTERN = re.compile(
    r"&(?!amp;|lt;|gt;|apos;|quot;|#\d+;|#x[0-9a-f]+;)",
    re.IGNORECASE,
)
_SAFE_TABLE_TAGS = frozenset(
    {"table", "thead", "tbody", "tr", "th", "td", "br", "strong", "em", "a"}
)
_MAX_CHAPTER_CHARACTERS = 120_000
_MIN_CHAPTER_CHARACTERS = 1_500
_MIN_STRONG_CHAPTER_CHARACTERS = 240
_MAX_CHAPTERS = 250
_ORDINAL_HEADING_PATTERN = (
    r"(?:\d+|[ivxlcdm]+|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez)"
)
_CONTAINER_TITLE_PATTERN = re.compile(
    r"^\s*(?:part|parte|book|libro|volume|volumen|section|secci(?:\u00f3|o)n|tomo)\s+"
    + _ORDINAL_HEADING_PATTERN
    + r"(?:\b|[.:\u2013\u2014-])",
    re.IGNORECASE,
)
_CHAPTER_TITLE_PATTERN = re.compile(
    r"^\s*(?:chapter|cap(?:\u00ed|i)tulo|chapitre|cap\.)\s+"
    + _ORDINAL_HEADING_PATTERN
    + r"(?:\b|[.:\u2013\u2014-])",
    re.IGNORECASE,
)
_STRONG_CHAPTER_TITLE_PATTERN = re.compile(
    r"^(?:(?:chapter|cap[ií]tulo|part|parte|book|libro)\s+"
    r"(?:\d+|[ivxlcdm]+|one|two|three|four|five|six|seven|eight|nine|ten)"
    r"(?:\b|[.:—-])|introduction|introducci[oó]n|preface|pr[oó]logo|"
    r"foreword|appendix|ap[eé]ndice)\b",
    re.IGNORECASE,
)
_ALLOWED_IMAGE_MEDIA_TYPES = frozenset(
    {"image/gif", "image/jpeg", "image/png", "image/svg+xml", "image/webp"}
)
_ALLOWED_EXTERNAL_LINK_SCHEMES = frozenset({"ftp", "http", "https", "mailto"})
_FORBIDDEN_XML_ELEMENTS = frozenset({"applet", "embed", "form", "iframe", "object", "script"})
_CONTAINER_NAMESPACE = "urn:oasis:names:tc:opendocument:xmlns:container"
_DC_NAMESPACE = "http://purl.org/dc/elements/1.1/"
_EPUB_NAMESPACE = "http://www.idpf.org/2007/ops"
_OPF_NAMESPACE = "http://www.idpf.org/2007/opf"
_XML_NAMESPACE = "http://www.w3.org/XML/1998/namespace"
_XHTML_NAMESPACE = "http://www.w3.org/1999/xhtml"
_SVG_NAMESPACE = "http://www.w3.org/2000/svg"


@dataclass(frozen=True, slots=True)
class EpubBookMetadata:
    """Minimal metadata needed by every EPUB publication."""

    title: str
    language: str = "und"
    author: str | None = None
    cover_resource: PurePosixPath | None = None
    identifiers: tuple[str, ...] = ()
    publisher: str | None = None
    publication_date: str | None = None


@dataclass(frozen=True, slots=True)
class BuiltEpub:
    """Complete in-memory EPUB plus useful validation counters."""

    content: bytes
    chapter_count: int
    resource_count: int
    integrity_report: FinalIntegrityReport | None = None


@dataclass(frozen=True, slots=True)
class EpubChapterPlan:
    """One exact XHTML document that will be written to the EPUB."""

    filename: str
    title: str
    markdown: str
    role: str | None = None


@dataclass(frozen=True, slots=True)
class EpubOutlineEntry:
    """One heading in the review outline and the chapter that owns it."""

    level: int
    title: str
    chapter_number: int
    starts_chapter: bool


@dataclass(frozen=True, slots=True)
class EpubPlan:
    """The single source of truth shared by preview, navigation and export."""

    chapters: tuple[EpubChapterPlan, ...]
    outline: tuple[EpubOutlineEntry, ...]
    preferred_heading_level: int | None


@dataclass(frozen=True, slots=True)
class EpubNavigationNode:
    """One nested table-of-contents entry for an already rendered chapter."""

    title: str
    filename: str
    children: tuple[EpubNavigationNode, ...] = ()


def classify_heading_role(title: str) -> str | None:
    """Classify only explicit, numbered container or chapter titles."""

    cleaned = re.sub(r"[*_`~\[\]]", "", title).strip()
    if _CONTAINER_TITLE_PATTERN.match(cleaned):
        return "container"
    if _CHAPTER_TITLE_PATTERN.match(cleaned):
        return "chapter"
    return None


def _markdown_heading_role(markdown: str) -> str | None:
    heading = _HEADING_PATTERN.search(markdown)
    return classify_heading_role(heading.group(2)) if heading else None


def chapter_filename(index: int) -> str:
    """Return the one canonical filename used for generated EPUB chapters."""

    if not 1 <= index <= _MAX_CHAPTERS:
        raise ValueError("El número de capítulo no es válido.")
    return f"chapter-{index:04d}.xhtml"


def build_epub(
    markdown: str,
    resources: tuple[ConvertedResource, ...],
    metadata: EpubBookMetadata,
    *,
    cancellation: CancellationToken | None = None,
    identifier: UUID | None = None,
    modified_at: datetime | None = None,
) -> BuiltEpub:
    """Return a standards-shaped, reflowable EPUB 3 without touching the filesystem."""
    check_cancelled(cancellation)
    markdown = _xml_safe_text(markdown)
    if not markdown.strip():
        raise ConversionError("No hay contenido suficiente para crear el EPUB.")
    title = _clean_metadata_text(metadata.title, "Documento sin título")
    language = _clean_language(metadata.language)
    author = _clean_metadata_text(metadata.author, "") if metadata.author else None
    identifiers = _publication_identifiers(metadata.identifiers, identifier)
    publisher = _clean_metadata_text(metadata.publisher, "") if metadata.publisher else None
    publication_date = (
        _clean_metadata_text(metadata.publication_date, "") if metadata.publication_date else None
    )
    normalized_resources = _normalized_resources(resources)
    cover_resource = (
        metadata.cover_resource.as_posix() if metadata.cover_resource is not None else None
    )
    if cover_resource is not None and cover_resource not in normalized_resources:
        raise ConversionError("Falta la imagen elegida como portada del EPUB.")
    _validate_resource_references(markdown, normalized_resources)
    plan = plan_epub(markdown, title)
    chapters = plan.chapters
    modified = (modified_at or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)

    rendered_chapters = _render_chapters(
        chapters,
        normalized_resources,
        language,
        cancellation,
    )
    content = _write_epub_archive(
        chapters,
        rendered_chapters,
        normalized_resources,
        title=title,
        language=language,
        author=author,
        identifiers=identifiers,
        publisher=publisher,
        publication_date=publication_date,
        cover_resource=cover_resource,
        modified=modified,
        cancellation=cancellation,
    )
    integrity_report = verify_epub_payload(
        content,
        {
            chapter.filename: rendered
            for chapter, rendered in zip(chapters, rendered_chapters, strict=True)
        },
        normalized_resources,
    )
    return BuiltEpub(
        content,
        len(chapters),
        len(normalized_resources),
        integrity_report,
    )


def reconcile_generated_html_tables(source: str, candidate: str) -> str:
    """Keep a safe generated table envelope while accepting only aligned text changes.

    Translation and review guards compare the document's table structure, but a model can still
    mutate a CSS class or link attribute without changing the ordered row/cell tags.  Rebuild that
    narrow case from the already validated source envelope.  If text nodes no longer align exactly,
    preserve the complete source table instead of making the whole EPUB unpublishable.
    """

    source_tables = tuple(_RAW_TABLE_BLOCK_PATTERN.finditer(source))
    candidate_tables = tuple(_RAW_TABLE_BLOCK_PATTERN.finditer(candidate))
    if not source_tables or len(source_tables) != len(candidate_tables):
        return candidate

    replacements: list[tuple[int, int, str]] = []
    rebuilt_count = 0
    preserved_count = 0
    for source_match, candidate_match in zip(source_tables, candidate_tables, strict=True):
        source_table = source_match.group(0)
        candidate_table = candidate_match.group(0)
        if (
            _safe_table_xhtml(source_table) is None
            or _safe_table_xhtml(candidate_table) is not None
        ):
            continue
        rebuilt = _rebuild_generated_table_text(source_table, candidate_table)
        if rebuilt is not None and _safe_table_xhtml(rebuilt) is not None:
            replacement = rebuilt
            rebuilt_count += 1
        else:
            replacement = source_table
            preserved_count += 1
        replacements.append((candidate_match.start(), candidate_match.end(), replacement))

    for start, end, replacement in reversed(replacements):
        candidate = f"{candidate[:start]}{replacement}{candidate[end:]}"
    if replacements:
        LOGGER.info(
            "generated_table_markup_reconciled rebuilt=%d preserved=%d",
            rebuilt_count,
            preserved_count,
        )
    return candidate


def _rebuild_generated_table_text(source: str, candidate: str) -> str | None:
    """Copy aligned plain text onto the exact source tag and attribute envelope."""

    tag_pattern = re.compile(r"<\s*(/?)\s*([A-Za-z][\w:-]*)\b[^>]*?(\/?)\s*>")

    def tag_signature(value: str) -> tuple[tuple[str, str, str], ...]:
        return tuple(
            (
                "/" if match.group(1) else "",
                match.group(2).casefold(),
                "/" if match.group(3) else "",
            )
            for match in tag_pattern.finditer(value)
        )

    if tag_signature(source) != tag_signature(candidate):
        return None
    source_nodes = tuple(re.finditer(r"(?<=>)[^<>]*(?=<)", source))
    candidate_nodes = tuple(re.finditer(r"(?<=>)[^<>]*(?=<)", candidate))
    if len(source_nodes) != len(candidate_nodes):
        return None

    replacements: list[tuple[int, int, str]] = []
    for source_node, candidate_node in zip(source_nodes, candidate_nodes, strict=True):
        source_value = source_node.group(0)
        if not source_value.strip():
            continue
        candidate_value = candidate_node.group(0)
        if not candidate_value.strip() or "\0" in candidate_value:
            return None
        leading = source_value[: len(source_value) - len(source_value.lstrip())]
        trailing = source_value[len(source_value.rstrip()) :]
        escaped_value = escape(unescape(candidate_value.strip()), quote=False)
        replacements.append(
            (source_node.start(), source_node.end(), f"{leading}{escaped_value}{trailing}")
        )

    rebuilt = source
    for start, end, replacement in reversed(replacements):
        rebuilt = f"{rebuilt[:start]}{replacement}{rebuilt[end:]}"
    return rebuilt


def build_epub_from_xhtml(
    chapters: tuple[EpubChapterPlan, ...],
    rendered_chapters: tuple[str, ...],
    resources: tuple[ConvertedResource, ...],
    metadata: EpubBookMetadata,
    *,
    navigation: tuple[EpubNavigationNode, ...] = (),
    cancellation: CancellationToken | None = None,
    identifier: UUID | None = None,
    modified_at: datetime | None = None,
    stylesheet: str | None = None,
) -> BuiltEpub:
    """Publish validated XHTML from the normalized book editor."""

    rendered_chapters = tuple(_xml_safe_text(chapter) for chapter in rendered_chapters)
    if not chapters or len(chapters) > _MAX_CHAPTERS or len(chapters) != len(rendered_chapters):
        raise ConversionError("El libro editable no contiene capítulos válidos.")
    normalized_resources = _normalized_resources(resources)
    cover_resource = (
        metadata.cover_resource.as_posix() if metadata.cover_resource is not None else None
    )
    if cover_resource is not None and cover_resource not in normalized_resources:
        raise ConversionError("Falta la imagen elegida como portada del EPUB.")
    for chapter, xhtml in zip(chapters, rendered_chapters, strict=True):
        if not chapter.filename.endswith(".xhtml") or not xhtml.strip():
            raise ConversionError("Un capítulo del libro no es válido.")
    title = _clean_metadata_text(metadata.title, "Documento sin título")
    language = _clean_language(metadata.language)
    author = _clean_metadata_text(metadata.author, "") if metadata.author else None
    identifiers = _publication_identifiers(metadata.identifiers, identifier)
    publisher = _clean_metadata_text(metadata.publisher, "") if metadata.publisher else None
    publication_date = (
        _clean_metadata_text(metadata.publication_date, "") if metadata.publication_date else None
    )
    modified = (modified_at or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    content = _write_epub_archive(
        chapters,
        rendered_chapters,
        normalized_resources,
        title=title,
        language=language,
        author=author,
        identifiers=identifiers,
        publisher=publisher,
        publication_date=publication_date,
        cover_resource=cover_resource,
        modified=modified,
        cancellation=cancellation,
        navigation=navigation,
        stylesheet=stylesheet,
    )
    integrity_report = verify_epub_payload(
        content,
        {
            chapter.filename: rendered
            for chapter, rendered in zip(chapters, rendered_chapters, strict=True)
        },
        normalized_resources,
    )
    return BuiltEpub(
        content,
        len(chapters),
        len(normalized_resources),
        integrity_report,
    )


def plan_epub(markdown: str, fallback_title: str) -> EpubPlan:
    """Return the exact chapter split and a complete heading outline."""
    explicit_chapters = _explicit_chapters(markdown)
    if explicit_chapters is None:
        blocks = _markdown_blocks(markdown)
        semantic_document = analyze_markdown(markdown)
        preferred_level = _preferred_heading_level(blocks, semantic_document)
        strong_headings = _strong_chapter_headings(blocks, semantic_document)
        chapters = _chapters_from_blocks(
            blocks,
            fallback_title,
            preferred_level,
            strong_headings,
        )
    else:
        preferred_level = None
        chapters = tuple(
            EpubChapterPlan(
                filename=chapter_filename(index),
                title=_chapter_title(chapter, fallback_title, index, len(explicit_chapters)),
                markdown=chapter,
                role=_markdown_heading_role(chapter),
            )
            for index, chapter in enumerate(explicit_chapters, start=1)
        )
    chapters = tuple(
        EpubChapterPlan(
            filename=chapter.filename,
            title=chapter.title,
            markdown=_normalize_heading_hierarchy(chapter.markdown),
            role=chapter.role,
        )
        for chapter in chapters
    )
    outline: list[EpubOutlineEntry] = []
    for chapter_number, chapter in enumerate(chapters, start=1):
        first_heading = True
        for match in _HEADING_PATTERN.finditer(chapter.markdown):
            title = re.sub(r"[*_`~\[\]]", "", match.group(2)).strip()
            if not title:
                continue
            level = len(match.group(1))
            outline.append(
                EpubOutlineEntry(
                    level=level,
                    title=title[:160],
                    chapter_number=chapter_number,
                    starts_chapter=first_heading and title == chapter.title,
                )
            )
            first_heading = False
    return EpubPlan(chapters, tuple(outline), preferred_level)


def compose_explicit_epub_chapters(chapters: Iterable[str]) -> str:
    """Compose an exact, review-approved EPUB split without exposing it in the book."""
    normalized = tuple(chapter.strip() for chapter in chapters if chapter.strip())
    if not normalized:
        return ""
    separator = f"\n\n{EPUB_CHAPTER_MARKER}\n\n"
    return separator.join(normalized).rstrip() + "\n"


def _explicit_chapters(markdown: str) -> tuple[str, ...] | None:
    if _EXPLICIT_CHAPTER_PATTERN.search(markdown) is None:
        return None
    chapters = tuple(
        chunk.strip() for chunk in _EXPLICIT_CHAPTER_PATTERN.split(markdown) if chunk.strip()
    )
    if not chapters:
        raise ConversionError("La estructura EPUB revisada no contiene capítulos válidos.")
    if len(chapters) > _MAX_CHAPTERS:
        raise ConversionError("La estructura EPUB revisada contiene demasiados capítulos.")
    return chapters


def _normalized_resources(
    resources: tuple[ConvertedResource, ...],
) -> dict[str, ConvertedResource]:
    normalized: dict[str, ConvertedResource] = {}
    for resource in resources:
        path = resource.relative_path
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
            or "\\" in path.as_posix()
        ):
            raise ConversionError("Una imagen del EPUB tiene una ruta interna no válida.")
        if resource.media_type not in _ALLOWED_IMAGE_MEDIA_TYPES:
            raise ConversionError("El EPUB contiene un formato de imagen no compatible.")
        key = path.as_posix()
        previous = normalized.get(key)
        if previous is not None and previous.read_content() != resource.read_content():
            raise ConversionError("Dos imágenes del EPUB intentan usar el mismo nombre.")
        normalized[key] = resource
    return normalized


def _validate_resource_references(
    markdown: str,
    resources: dict[str, ConvertedResource],
) -> None:
    referenced = {match.group(1) for match in _RESOURCE_PATTERN.finditer(markdown)}
    missing = referenced.difference(resources)
    if missing:
        raise ConversionError("Falta una imagen necesaria para crear el EPUB.")


def _chapters(markdown: str, fallback_title: str) -> tuple[EpubChapterPlan, ...]:
    """Compatibility helper for callers that only need the chapter sequence."""
    return plan_epub(markdown, fallback_title).chapters


def _chapters_from_blocks(
    blocks: list[str],
    fallback_title: str,
    preferred_level: int | None,
    strong_heading_positions: frozenset[int] = frozenset(),
) -> tuple[EpubChapterPlan, ...]:
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0
    for position, block in enumerate(blocks):
        heading_level = _block_heading_level(block)
        should_split_for_heading = (
            preferred_level is not None
            and heading_level == preferred_level
            and current_size >= _MIN_CHAPTER_CHARACTERS
        )
        should_split_for_strong_heading = (
            position in strong_heading_positions and current_size >= _MIN_STRONG_CHAPTER_CHARACTERS
        )
        should_split_for_size = current_size >= _MAX_CHAPTER_CHARACTERS
        if current and (
            should_split_for_heading or should_split_for_strong_heading or should_split_for_size
        ):
            chunks.append("".join(current).strip())
            current = []
            current_size = 0
        current.append(block)
        current_size += len(block)
    if current:
        chunks.append("".join(current).strip())
    chunks = [chunk for chunk in chunks if chunk]
    if len(chunks) > _MAX_CHAPTERS:
        chunks = _merge_excess_chapters(chunks)
    return tuple(
        EpubChapterPlan(
            filename=chapter_filename(index),
            title=_chapter_title(chunk, fallback_title, index, len(chunks)),
            markdown=chunk,
            role=_markdown_heading_role(chunk),
        )
        for index, chunk in enumerate(chunks, start=1)
    )


def _markdown_blocks(markdown: str) -> list[str]:
    """Split only at blank lines outside fenced code blocks."""
    blocks: list[str] = []
    current: list[str] = []
    fence: str | None = None
    for line in markdown.splitlines(keepends=True):
        fence_match = _FENCE_PATTERN.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
        current.append(line)
        if fence is None and not line.strip():
            blocks.append("".join(current))
            current = []
    if current:
        blocks.append("".join(current))
    return blocks or [markdown]


def _normalize_heading_hierarchy(markdown: str) -> str:
    """Clamp only unsafe downward jumps while preserving every heading's text."""
    lines = markdown.splitlines(keepends=True)
    fence: str | None = None
    previous_level: int | None = None
    for index, line in enumerate(lines):
        fence_match = _FENCE_PATTERN.match(line)
        if fence_match is not None:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is not None:
            continue
        ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        content = line[: -len(ending)] if ending else line
        heading = re.match(r"^( {0,3})(#{1,6})([ \t]+.*)$", content)
        if heading is None:
            continue
        level = len(heading.group(2))
        maximum_level = 2 if previous_level is None else previous_level + 1
        normalized_level = min(level, maximum_level)
        if normalized_level != level:
            lines[index] = f"{heading.group(1)}{'#' * normalized_level}{heading.group(3)}{ending}"
        previous_level = normalized_level
    return "".join(lines)


def _preferred_heading_level(
    blocks: list[str],
    semantic_document: SemanticDocument | None = None,
) -> int | None:
    counts: dict[int, int] = {}
    scores: dict[int, float] = {}
    toc_titles = _toc_titles(semantic_document)
    for position, block in enumerate(blocks):
        level = _block_heading_level(block)
        if level is not None:
            if (
                semantic_document is not None
                and position < len(semantic_document.blocks)
                and semantic_document.blocks[position].role is SemanticRole.FRONT_MATTER
            ):
                continue
            counts[level] = counts.get(level, 0) + 1
            title = _block_heading_title(block)
            score = 1.0
            if title is not None and _STRONG_CHAPTER_TITLE_PATTERN.match(title):
                score += 5.0
            if title is not None and _normalized_heading_title(title) in toc_titles:
                score += 3.0
            scores[level] = scores.get(level, 0.0) + score
    candidates = {
        level: scores.get(level, 0.0) / count
        for level, count in counts.items()
        if 2 <= count <= _MAX_CHAPTERS
    }
    if candidates:
        strong_candidates = {
            level: score for level, score in candidates.items() if level <= 2 or score >= 2.0
        }
        if strong_candidates:
            return min(
                strong_candidates,
                key=lambda level: (-strong_candidates[level], level),
            )
    return None


def _block_heading_level(block: str) -> int | None:
    first_line = block.lstrip().splitlines()[0] if block.strip() else ""
    match = _HEADING_PATTERN.fullmatch(first_line)
    return len(match.group(1)) if match else None


def _block_heading_title(block: str) -> str | None:
    first_line = block.lstrip().splitlines()[0] if block.strip() else ""
    match = _HEADING_PATTERN.fullmatch(first_line)
    if match is None:
        return None
    return re.sub(r"[*_`~\[\]]", "", match.group(2)).strip()


def _strong_chapter_headings(
    blocks: list[str],
    semantic_document: SemanticDocument,
) -> frozenset[int]:
    toc_titles = _toc_titles(semantic_document)
    selected: set[int] = set()
    had_front_matter = semantic_document.front_matter_blocks > 0
    first_body_heading_added = False
    for position, block in enumerate(blocks):
        title = _block_heading_title(block)
        if title is None or position >= len(semantic_document.blocks):
            continue
        semantic_block = semantic_document.blocks[position]
        if semantic_block.role in {
            SemanticRole.FRONT_MATTER,
            SemanticRole.PROVENANCE,
            SemanticRole.TOC,
        }:
            continue
        normalized = _normalized_heading_title(title)
        if (
            _STRONG_CHAPTER_TITLE_PATTERN.match(title)
            or normalized in toc_titles
            or (had_front_matter and not first_body_heading_added)
        ):
            selected.add(position)
        first_body_heading_added = True
    return frozenset(selected)


def _toc_titles(semantic_document: SemanticDocument | None) -> frozenset[str]:
    if semantic_document is None:
        return frozenset()
    titles: set[str] = set()
    for block in semantic_document.blocks:
        if block.role is not SemanticRole.TOC:
            continue
        for line in block.markdown.splitlines():
            visible = re.sub(r"\]\([^)]+\)", "]", line)
            visible = re.sub(r"[*_`~#\[\]|]", " ", visible)
            visible = re.sub(r"(?:\.{2,}|\s{2,})\s*\d+\s*$", "", visible)
            normalized = _normalized_heading_title(visible)
            if 2 <= len(normalized) <= 120 and normalized not in {
                "contents",
                "table of contents",
                "índice",
                "indice",
                "sumario",
            }:
                titles.add(normalized)
    return frozenset(titles)


def _normalized_heading_title(title: str) -> str:
    return re.sub(r"[^\w]+", " ", title, flags=re.UNICODE).strip().casefold()


def _merge_excess_chapters(chunks: list[str]) -> list[str]:
    merged: list[str] = []
    stride = max(2, (len(chunks) + _MAX_CHAPTERS - 1) // _MAX_CHAPTERS)
    for index in range(0, len(chunks), stride):
        merged.append("\n\n".join(chunks[index : index + stride]))
    return merged


def _chapter_title(markdown: str, fallback: str, index: int, total: int) -> str:
    heading = _HEADING_PATTERN.search(markdown)
    if heading:
        text = re.sub(r"[*_`~\[\]]", "", heading.group(2)).strip()
        if text:
            return text[:160]
    if total == 1:
        return fallback
    return f"Parte {index}"


def _render_chapters(
    chapters: tuple[EpubChapterPlan, ...],
    resources: dict[str, ConvertedResource],
    language: str,
    cancellation: CancellationToken | None,
) -> tuple[str, ...]:
    renderer = MarkdownIt(
        "commonmark",
        {"html": False, "linkify": False, "typographer": False, "xhtmlOut": True},
    ).enable(["table", "strikethrough"])
    anchors_by_chapter: dict[str, str] = {}
    prepared: list[str] = []
    for chapter in chapters:
        text = _prepare_markdown(chapter.markdown, chapter.filename, resources)
        prepared.append(text)
        for anchor in _anchors(chapter.markdown):
            anchors_by_chapter.setdefault(anchor, chapter.filename)

    rendered: list[str] = []
    for chapter, source in zip(chapters, prepared, strict=True):
        check_cancelled(cancellation)
        protected_source, safe_tables = _protect_safe_table_blocks(source)
        body = renderer.render(protected_source)
        for sentinel, table in safe_tables:
            body = body.replace(f"<p>{sentinel}</p>\n", f"{table}\n")
        if any(
            not match.group(1).startswith("../images/")
            for match in _RENDERED_IMAGE_SOURCE_PATTERN.finditer(body)
        ):
            raise ConversionError(
                "El documento contiene una imagen que no se pudo incluir de forma segura."
            )
        for anchor in _anchors(chapter.markdown):
            body = body.replace(
                _anchor_sentinel(anchor),
                f'<span id="{escape(anchor, quote=True)}"></span>',
            )
        body = _rewrite_internal_links(body, chapter.filename, anchors_by_chapter)
        rendered.append(_xhtml_document(chapter.title, body, language))
    return tuple(rendered)


def _protect_safe_table_blocks(markdown: str) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Protect only Parsezen's restricted table HTML while arbitrary raw HTML stays disabled."""

    replacements: list[tuple[str, str]] = []

    def replace(match: re.Match[str]) -> str:
        table = _safe_table_xhtml(match.group(0))
        if table is None:
            if _DOCUMENT_TOC_OPENING_PATTERN.match(match.group(0)):
                raise ConversionError(
                    "El índice interno no se pudo convertir en una tabla EPUB segura."
                )
            return match.group(0)
        sentinel = f"PZDOCEPUBTABLE{sha256(match.group(0).encode('utf-8')).hexdigest()[:24]}"
        while sentinel in markdown or any(item[0] == sentinel for item in replacements):
            sentinel = f"Z{sentinel}"
        replacements.append((sentinel, table))
        return f"\n\n{sentinel}\n\n"

    return _RAW_TABLE_BLOCK_PATTERN.sub(replace, markdown), tuple(replacements)


def _safe_table_xhtml(value: str) -> str | None:
    """Return normalized XHTML for a strict generated table fragment."""

    normalized = re.sub(r"<br\s*/?>", "<br />", value, flags=re.IGNORECASE)
    normalized = _BARE_XML_AMPERSAND_PATTERN.sub("&amp;", normalized)
    try:
        root = SafeElementTree.fromstring(normalized)
    except (DefusedXmlException, SafeElementTree.ParseError):
        return None
    elements = tuple(root.iter())
    if (
        root.tag.casefold() != "table"
        or any(element.tag.casefold() not in _SAFE_TABLE_TAGS for element in elements)
        or any(not _safe_table_attributes(element) for element in elements)
        or (root.text or "").strip()
    ):
        return None
    children = list(root)
    if [child.tag.casefold() for child in children] != ["thead", "tbody"]:
        return None
    thead, tbody = children
    header_rows = list(thead)
    body_rows = list(tbody)
    if len(header_rows) != 1 or not body_rows:
        return None
    header_cells = list(header_rows[0])
    if not header_cells or any(cell.tag.casefold() != "th" for cell in header_cells):
        return None
    column_count = len(header_cells)
    if any(
        row.tag.casefold() != "tr"
        or len(row) != column_count
        or any(cell.tag.casefold() != "td" for cell in row)
        for row in body_rows
    ):
        return None
    cells = (*header_cells, *(cell for row in body_rows for cell in row))
    structural = (thead, tbody, header_rows[0], *body_rows)
    if any((element.text or "").strip() for element in structural) or any(
        (element.tail or "").strip()
        for element in (*children, *header_rows, *body_rows, *header_cells, *cells)
    ):
        return None
    if any(not _safe_table_cell_content(cell) for cell in cells):
        return None
    return cast(
        str,
        SafeElementTree.tostring(root, encoding="unicode", short_empty_elements=True),
    )


def _safe_table_attributes(element: object) -> bool:
    tag = str(getattr(element, "tag", "")).casefold()
    attributes = dict(getattr(element, "attrib", {}))
    if not attributes:
        return True
    if tag == "table":
        return attributes == {"class": "document-toc"}
    if tag == "th":
        return attributes.get("class") in {"toc-label", "toc-folio"} and len(attributes) == 1
    if tag == "td":
        css_class = attributes.get("class")
        return (
            css_class == "toc-folio"
            or css_class in {f"toc-label toc-level-{level}" for level in range(3)}
        ) and len(attributes) == 1
    if tag == "a":
        href = attributes.get("href", "")
        return len(attributes) == 1 and re.fullmatch(r"#page-\d{1,6}", href) is not None
    return False


def _safe_table_cell_content(cell: Any) -> bool:
    for descendant in tuple(cell.iter())[1:]:
        tag = str(getattr(descendant, "tag", "")).casefold()
        if tag not in {"br", "strong", "em", "a"} or not _safe_table_attributes(descendant):
            return False
        if tag == "br" and ((getattr(descendant, "text", None) or "").strip() or len(descendant)):
            return False
    return True


def _prepare_markdown(
    markdown: str,
    chapter_filename: str,
    resources: dict[str, ConvertedResource],
) -> str:
    del chapter_filename
    prepared = _SAFE_ANCHOR_PATTERN.sub(
        lambda match: _anchor_sentinel(match.group(1)),
        markdown,
    )
    prepared = _EPUB_ANCHOR_COMMENT_PATTERN.sub(
        lambda match: _anchor_sentinel(match.group(1)),
        prepared,
    )
    prepared = _PDF_PAGE_MARKER_PATTERN.sub("", prepared)
    prepared = _escape_numeric_reference_list_markers(prepared)
    for path in resources:
        target = quote(f"../images/{path}", safe="/._-~")
        prepared = prepared.replace(f"{RESOURCE_REFERENCE_PREFIX}{path}", target)
    return prepared


def _escape_numeric_reference_list_markers(markdown: str) -> str:
    """Keep dense index continuations from being renumbered as CommonMark lists."""

    def replace(match: re.Match[str]) -> str:
        references = match.group("references")
        if (
            int(match.group("marker")) < 10
            or any(character.isalpha() for character in references)
            or len(_NUMERIC_REFERENCE_TOKEN_PATTERN.findall(references)) < 2
        ):
            return match.group(0)
        return (
            f"{match.group('indent')}{match.group('marker')}\\{match.group('delimiter')}"
            f"{match.group('space')}{references}"
        )

    return _NUMERIC_REFERENCE_LIST_PATTERN.sub(replace, markdown)


def _anchors(markdown: str) -> set[str]:
    return {
        *(match.group(1) for match in _SAFE_ANCHOR_PATTERN.finditer(markdown)),
        *(match.group(1) for match in _EPUB_ANCHOR_COMMENT_PATTERN.finditer(markdown)),
    }


def _anchor_sentinel(anchor: str) -> str:
    return f"PZDOCEPUBANCHOR{sha256(anchor.encode('utf-8')).hexdigest()[:24]}"


def _rewrite_internal_links(
    html: str,
    current_filename: str,
    anchors_by_chapter: dict[str, str],
) -> str:
    def replacement(match: re.Match[str]) -> str:
        href = match.group("href")
        try:
            parsed = urlsplit(href)
        except ValueError:
            return match.group("label")
        if parsed.scheme or parsed.netloc:
            return match.group(0)
        if parsed.path or parsed.query or not parsed.fragment:
            # A generated EPUB cannot safely package arbitrary relative files.
            # Keep the visible label from malformed or out-of-scope PDF links
            # instead of publishing a dead interactive control.
            return match.group("label")
        anchor = unquote(parsed.fragment)
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9._:-]{0,127}", anchor) is None:
            return match.group("label")
        destination = anchors_by_chapter.get(anchor)
        if destination is None:
            # A partial document can retain the label of a link whose target
            # falls outside the selected content. Keep the information without
            # publishing an invalid interactive control.
            return match.group("label")
        if destination == current_filename:
            return match.group(0)
        return (
            f'<a{match.group("before")}href="{destination}#{escape(anchor, quote=True)}"'
            f"{match.group('after')}>{match.group('label')}</a>"
        )

    return _RENDERED_LINK_PATTERN.sub(replacement, html)


def _xhtml_document(
    title: str,
    body: str,
    language: str,
    *,
    body_epub_type: str | None = None,
) -> str:
    epub_namespace = (
        ' xmlns:epub="http://www.idpf.org/2007/ops"' if body_epub_type is not None else ""
    )
    body_attributes = (
        f' epub:type="{escape(body_epub_type, quote=True)}"' if body_epub_type is not None else ""
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<!DOCTYPE html>\n"
        f'<html xmlns="http://www.w3.org/1999/xhtml"{epub_namespace} '
        f'xml:lang="{escape(language, quote=True)}" lang="{escape(language, quote=True)}">\n'
        "<head>\n"
        f"<title>{escape(title)}</title>\n"
        '<meta charset="utf-8" />\n'
        '<link rel="stylesheet" type="text/css" href="../styles/book.css" />\n'
        f"</head>\n<body{body_attributes}>\n"
        f"{body}"
        "</body>\n</html>\n"
    )


def _write_epub_archive(
    chapters: tuple[EpubChapterPlan, ...],
    rendered_chapters: tuple[str, ...],
    resources: dict[str, ConvertedResource],
    *,
    title: str,
    language: str,
    author: str | None,
    identifiers: tuple[str, ...],
    publisher: str | None,
    publication_date: str | None,
    cover_resource: str | None,
    modified: datetime,
    cancellation: CancellationToken | None,
    navigation: tuple[EpubNavigationNode, ...] = (),
    stylesheet: str | None = None,
) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        mimetype = ZipInfo("mimetype")
        mimetype.compress_type = ZIP_STORED
        archive.writestr(mimetype, b"application/epub+zip")
        archive.writestr("META-INF/container.xml", _container_xml())
        archive.writestr("EPUB/styles/book.css", stylesheet or _book_css())
        archive.writestr(
            "EPUB/nav.xhtml",
            (
                _nested_navigation_document(title, navigation, language)
                if navigation
                else _navigation_document(title, chapters, language)
            ),
        )
        if cover_resource is not None:
            archive.writestr(
                "EPUB/text/cover.xhtml",
                _cover_document(title, cover_resource, language),
            )
        archive.writestr(
            "EPUB/package.opf",
            _package_document(
                title,
                language,
                author,
                identifiers,
                publisher,
                publication_date,
                cover_resource,
                modified,
                chapters,
                resources,
            ),
        )
        for chapter, chapter_content in zip(chapters, rendered_chapters, strict=True):
            check_cancelled(cancellation)
            archive.writestr(f"EPUB/text/{chapter.filename}", chapter_content)
        for path, resource in resources.items():
            check_cancelled(cancellation)
            archive.writestr(f"EPUB/images/{path}", resource.read_content())
    archive_content = buffer.getvalue()
    validate_epub_archive(archive_content)
    return archive_content


def _container_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="EPUB/package.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )


def _navigation_document(
    title: str,
    chapters: tuple[EpubChapterPlan, ...],
    language: str,
) -> str:
    items = "\n".join(
        f'<li><a href="text/{chapter.filename}">{escape(chapter.title)}</a></li>'
        for chapter in chapters
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<!DOCTYPE html>\n"
        f'<html xmlns="http://www.w3.org/1999/xhtml" '
        f'xmlns:epub="http://www.idpf.org/2007/ops" lang="{escape(language, quote=True)}">'
        f'<head><title>{escape(title)}</title><meta charset="utf-8" /></head>'
        f'<body><nav epub:type="toc" id="toc"><h1>{escape(title)}</h1><ol>{items}</ol>'
        "</nav></body></html>"
    )


def _nested_navigation_document(
    title: str,
    navigation: tuple[EpubNavigationNode, ...],
    language: str,
) -> str:
    def render(nodes: tuple[EpubNavigationNode, ...]) -> str:
        return (
            "<ol>"
            + "".join(
                "<li>"
                f'<a href="text/{escape(quote(node.filename, safe="._-~"), quote=True)}">'
                f"{escape(node.title)}</a>"
                f"{render(node.children) if node.children else ''}"
                "</li>"
                for node in nodes
            )
            + "</ol>"
        )

    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<!DOCTYPE html>\n"
        f'<html xmlns="http://www.w3.org/1999/xhtml" '
        f'xmlns:epub="http://www.idpf.org/2007/ops" lang="{escape(language, quote=True)}">'
        f'<head><title>{escape(title)}</title><meta charset="utf-8" /></head>'
        f'<body><nav epub:type="toc" id="toc"><h1>{escape(title)}</h1>'
        f"{render(navigation)}</nav></body></html>"
    )


def _cover_document(title: str, cover_resource: str, language: str) -> str:
    source = escape(
        quote(f"../images/{cover_resource}", safe="/._-~"),
        quote=True,
    )
    return _xhtml_document(
        title,
        f'<div class="cover"><img src="{source}" alt="Portada" /></div>\n',
        language,
        body_epub_type="cover",
    )


def _package_document(
    title: str,
    language: str,
    author: str | None,
    identifiers: tuple[str, ...],
    publisher: str | None,
    publication_date: str | None,
    cover_resource: str | None,
    modified: datetime,
    chapters: tuple[EpubChapterPlan, ...],
    resources: dict[str, ConvertedResource],
) -> str:
    chapter_manifest = "\n".join(
        f'<item id="chapter-{index}" href="text/{chapter.filename}" '
        'media-type="application/xhtml+xml"/>'
        for index, chapter in enumerate(chapters, start=1)
    )
    resource_manifest = "\n".join(
        f'<item id="image-{index}" '
        f'href="{escape(quote(f"images/{path}", safe="/._-~"), quote=True)}" '
        f'media-type="{escape(resource.media_type, quote=True)}"'
        f"{_cover_image_property(path, cover_resource)}/>"
        for index, (path, resource) in enumerate(resources.items(), start=1)
    )
    cover_manifest = (
        '<item id="cover-page" href="text/cover.xhtml" media-type="application/xhtml+xml"/>'
        if cover_resource is not None
        else ""
    )
    chapter_spine = "\n".join(
        f'<itemref idref="chapter-{index}"/>' for index in range(1, len(chapters) + 1)
    )
    spine = (
        f'<itemref idref="cover-page"/>\n{chapter_spine}'
        if cover_resource is not None
        else chapter_spine
    )
    cover_guide = (
        '<guide><reference type="cover" title="Portada" href="text/cover.xhtml"/></guide>'
        if cover_resource is not None
        else ""
    )
    creator = f"<dc:creator>{escape(author)}</dc:creator>" if author else ""
    primary_identifier, *additional_identifiers = identifiers
    identifier_elements = (
        f'<dc:identifier id="book-id">{escape(primary_identifier)}</dc:identifier>'
        + "".join(
            f"<dc:identifier>{escape(value)}</dc:identifier>" for value in additional_identifiers
        )
    )
    publisher_element = f"<dc:publisher>{escape(publisher)}</dc:publisher>" if publisher else ""
    date_element = f"<dc:date>{escape(publication_date)}</dc:date>" if publication_date else ""
    modified_text = modified.strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        f'unique-identifier="book-id" xml:lang="{escape(language, quote=True)}">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/">'
        f"{identifier_elements}"
        f"<dc:title>{escape(title)}</dc:title><dc:language>{escape(language)}</dc:language>"
        f"{creator}{publisher_element}{date_element}"
        f'<meta property="dcterms:modified">{modified_text}</meta></metadata>'
        '<manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
        'properties="nav"/><item id="css" href="styles/book.css" '
        f'media-type="text/css"/>{cover_manifest}{chapter_manifest}'
        f"{resource_manifest}</manifest><spine>{spine}</spine>{cover_guide}</package>"
    )


def _cover_image_property(path: str, cover_resource: str | None) -> str:
    return ' properties="cover-image"' if path == cover_resource else ""


def _book_css() -> str:
    return """body {
  font-family: serif;
  line-height: 1.5;
  margin: 5%;
  padding-top: 0.5em;
}
h1, h2, h3, h4, h5, h6 {
  break-inside: avoid;
  line-height: 1.2;
  max-width: 100%;
  overflow-wrap: anywhere;
  page-break-after: avoid;
  page-break-inside: avoid;
  word-break: normal;
}
p { orphans: 2; widows: 2; }
img { display: block; height: auto; margin: 1.2em auto; max-width: 100%; }
.cover { align-items: center; display: flex; justify-content: center; min-height: 90vh; }
.cover img { margin: 0; max-height: 90vh; }
table { border-collapse: collapse; margin: 1em 0; width: 100%; }
th, td { border: 1px solid #777; padding: 0.35em 0.5em; text-align: left; }
table.document-toc { border: 0; table-layout: fixed; }
.document-toc thead {
  clip: rect(0 0 0 0);
  clip-path: inset(50%);
  height: 1px;
  overflow: hidden;
  position: absolute;
  white-space: nowrap;
  width: 1px;
}
.document-toc tr { break-inside: avoid; page-break-inside: avoid; }
.document-toc th, .document-toc td { border: 0; padding: 0.16em 0; vertical-align: baseline; }
.document-toc .toc-label { overflow-wrap: anywhere; padding-right: 0.8em; }
.document-toc .toc-level-1 { padding-left: 1.25em; }
.document-toc .toc-level-2 { padding-left: 2.5em; }
.document-toc .toc-folio {
  font-variant-numeric: tabular-nums;
  text-align: right;
  white-space: nowrap;
  width: 4.5em;
}
blockquote { border-left: 0.2em solid #777; margin-left: 0; padding-left: 1em; }
pre { overflow-wrap: anywhere; white-space: pre-wrap; }
code { font-family: monospace; }
"""


def _clean_metadata_text(value: str | None, fallback: str) -> str:
    cleaned = " ".join(_xml_safe_text(value or "").split()).strip()
    return (cleaned or fallback)[:500]


def _xml_safe_text(value: str) -> str:
    """Replace only code points forbidden by XML 1.0 without joining words."""

    return "".join(
        character if _xml_character_is_valid(ord(character)) else " " for character in value
    )


def _xml_character_is_valid(codepoint: int) -> bool:
    return (
        codepoint in {0x9, 0xA, 0xD}
        or 0x20 <= codepoint <= 0xD7FF
        or 0xE000 <= codepoint <= 0xFFFD
        or 0x10000 <= codepoint <= 0x10FFFF
    )


def _publication_identifiers(
    values: tuple[str, ...],
    explicit_identifier: UUID | None,
) -> tuple[str, ...]:
    cleaned: list[str] = []
    for value in values:
        normalized = _clean_metadata_text(value, "")
        if normalized and normalized not in cleaned:
            cleaned.append(normalized)
    if explicit_identifier is not None:
        primary = f"urn:uuid:{explicit_identifier}"
        return (primary, *(value for value in cleaned if value != primary))
    if cleaned:
        return tuple(cleaned)
    return (f"urn:uuid:{uuid4()}",)


def _clean_language(language: str) -> str:
    cleaned = language.strip().replace("_", "-").lower()
    if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*|und", cleaned):
        return "und"
    return cleaned


def epub_archive_names(content: bytes) -> tuple[str, ...]:
    """Return archive names for diagnostics and focused tests."""
    with ZipFile(BytesIO(content)) as archive:
        return tuple(archive.namelist())


def iter_epub_text_documents(content: bytes) -> Iterable[tuple[str, str]]:
    """Yield XHTML text documents for tests and internal diagnostics."""
    with ZipFile(BytesIO(content)) as archive:
        for name in archive.namelist():
            if name.startswith("EPUB/text/") and name.endswith(".xhtml"):
                yield name, archive.read(name).decode("utf-8")


def validate_epub_archive(content: bytes) -> None:
    """Reject a generated EPUB whose package, resources or local links are inconsistent."""

    if not content:
        raise ConversionError("El EPUB generado está vacío.")
    try:
        with ZipFile(BytesIO(content)) as archive:
            _validate_epub_zip(archive)
    except (BadZipFile, OSError, RuntimeError) as exc:
        raise ConversionError("El EPUB generado no contiene un archivo ZIP válido.") from exc


def validate_epub_file(path: Path) -> None:
    """Reject a malformed EPUB staging file without loading it again in memory."""

    try:
        if not path.is_file() or path.stat().st_size == 0:
            raise ConversionError("El EPUB generado está vacío.")
        with ZipFile(path) as archive:
            _validate_epub_zip(archive)
    except ConversionError:
        raise
    except (BadZipFile, OSError, RuntimeError) as exc:
        raise ConversionError("El EPUB generado no contiene un archivo ZIP válido.") from exc


def _validate_epub_zip(archive: ZipFile) -> None:
    infos = archive.infolist()
    names = tuple(info.filename for info in infos)
    if not names or names[0] != "mimetype":
        raise ConversionError("El EPUB generado no comienza por su tipo MIME.")
    if len(names) != len(set(names)):
        raise ConversionError("El EPUB generado contiene archivos internos duplicados.")
    for name in names:
        _validate_archive_name(name)
    if archive.testzip() is not None:
        raise ConversionError("El EPUB generado contiene datos internos dañados.")
    mimetype = archive.getinfo("mimetype")
    if mimetype.compress_type != ZIP_STORED:
        raise ConversionError("El tipo MIME del EPUB generado está comprimido.")
    if archive.read("mimetype") != b"application/epub+zip":
        raise ConversionError("El EPUB generado declara un tipo MIME incorrecto.")

    container_name = "META-INF/container.xml"
    if container_name not in names:
        raise ConversionError("El EPUB generado no contiene META-INF/container.xml.")
    container = _parse_xml_entry(archive, container_name)
    rootfiles = container.findall(f".//{{{_CONTAINER_NAMESPACE}}}rootfile")
    if len(rootfiles) != 1:
        raise ConversionError("El EPUB generado no declara un paquete único.")
    package_name = rootfiles[0].attrib.get("full-path", "")
    _validate_archive_name(package_name)
    if package_name not in names:
        raise ConversionError("El paquete declarado por el EPUB no existe.")

    package = _parse_xml_entry(archive, package_name)
    if _qualified_name(package.tag) != (_OPF_NAMESPACE, "package"):
        raise ConversionError("El paquete OPF del EPUB no es válido.")
    manifest = _manifest_items(package, package_name, frozenset(names))
    manifest_by_path = {item.path: item for item in manifest.values()}
    _validate_package_metadata(package)
    navigation_path, spine_paths = _validate_package_navigation_and_spine(package, manifest)

    package_parent = PurePosixPath(package_name).parent.as_posix()
    allowed_package_files = {
        package_name,
        *(item.path for item in manifest.values()),
    }
    unexpected = tuple(
        name
        for name in names
        if name.startswith(f"{package_parent}/") and name not in allowed_package_files
    )
    if unexpected:
        raise ConversionError("El EPUB generado contiene recursos sin declarar.")

    xml_documents: dict[str, tuple[SafeElementTree.Element, frozenset[str]]] = {}
    for item in manifest.values():
        if item.media_type not in {"application/xhtml+xml", "image/svg+xml"}:
            continue
        root = _parse_xml_entry(archive, item.path)
        expected = (
            (_XHTML_NAMESPACE, "html")
            if item.media_type == "application/xhtml+xml"
            else (_SVG_NAMESPACE, "svg")
        )
        if _qualified_name(root.tag) != expected:
            raise ConversionError(f'El recurso XML "{item.path}" no tiene el tipo declarado.')
        xml_documents[item.path] = (root, _document_ids(root, item.path))

    _validate_navigation_document(
        xml_documents[navigation_path][0],
        navigation_path=navigation_path,
        spine_paths=spine_paths,
    )
    for path, (root, _) in xml_documents.items():
        _validate_document_references(
            root,
            document_path=path,
            archive_names=frozenset(names),
            manifest_paths=frozenset(manifest_by_path),
            xml_documents=xml_documents,
        )


@dataclass(frozen=True, slots=True)
class _ManifestItem:
    identifier: str
    path: str
    media_type: str
    properties: frozenset[str]


def _manifest_items(
    package: SafeElementTree.Element,
    package_name: str,
    archive_names: frozenset[str],
) -> dict[str, _ManifestItem]:
    items: dict[str, _ManifestItem] = {}
    paths: set[str] = set()
    for element in package.findall(f".//{{{_OPF_NAMESPACE}}}manifest/{{{_OPF_NAMESPACE}}}item"):
        identifier = element.attrib.get("id", "").strip()
        href = element.attrib.get("href", "").strip()
        media_type = element.attrib.get("media-type", "").strip().casefold()
        if not identifier or not href or not media_type:
            raise ConversionError("El manifiesto del EPUB contiene una entrada incompleta.")
        if identifier in items:
            raise ConversionError("El manifiesto del EPUB contiene identificadores duplicados.")
        path, fragment, external = _resolve_archive_reference(package_name, href)
        if external or fragment:
            raise ConversionError("El manifiesto del EPUB contiene una ruta no válida.")
        if path in paths:
            raise ConversionError("El manifiesto del EPUB declara dos veces el mismo recurso.")
        if path not in archive_names:
            raise ConversionError(f'El recurso declarado "{path}" no existe en el EPUB.')
        item = _ManifestItem(
            identifier,
            path,
            media_type,
            frozenset(element.attrib.get("properties", "").split()),
        )
        items[identifier] = item
        paths.add(path)
    if not items:
        raise ConversionError("El manifiesto del EPUB está vacío.")
    return items


def _validate_package_metadata(package: SafeElementTree.Element) -> None:
    unique_identifier = package.attrib.get("unique-identifier", "").strip()
    identifiers = package.findall(f".//{{{_DC_NAMESPACE}}}identifier")
    if not unique_identifier or not any(
        element.attrib.get("id") == unique_identifier for element in identifiers
    ):
        raise ConversionError("El EPUB generado no contiene un identificador principal válido.")
    title = package.find(f".//{{{_DC_NAMESPACE}}}title")
    language = package.find(f".//{{{_DC_NAMESPACE}}}language")
    if (
        title is None
        or not (title.text or "").strip()
        or language is None
        or not (language.text or "").strip()
    ):
        raise ConversionError("El EPUB generado no contiene título e idioma válidos.")


def _validate_package_navigation_and_spine(
    package: SafeElementTree.Element,
    manifest: dict[str, _ManifestItem],
) -> tuple[str, tuple[str, ...]]:
    navigation = tuple(item for item in manifest.values() if "nav" in item.properties)
    if len(navigation) != 1 or navigation[0].media_type != "application/xhtml+xml":
        raise ConversionError("El EPUB generado no contiene una navegación única y válida.")
    cover_images = tuple(item for item in manifest.values() if "cover-image" in item.properties)
    if len(cover_images) > 1 or any(
        item.media_type not in _ALLOWED_IMAGE_MEDIA_TYPES for item in cover_images
    ):
        raise ConversionError("El EPUB generado contiene una portada no válida.")
    itemrefs = package.findall(f".//{{{_OPF_NAMESPACE}}}spine/{{{_OPF_NAMESPACE}}}itemref")
    if not itemrefs:
        raise ConversionError("El EPUB generado no contiene un orden de lectura.")
    spine_paths: list[str] = []
    seen_identifiers: set[str] = set()
    for itemref in itemrefs:
        identifier = itemref.attrib.get("idref", "")
        item = manifest.get(identifier)
        if item is None or item.media_type != "application/xhtml+xml":
            raise ConversionError("El orden de lectura del EPUB referencia un capítulo no válido.")
        if identifier in seen_identifiers:
            raise ConversionError("El orden de lectura del EPUB contiene capítulos duplicados.")
        seen_identifiers.add(identifier)
        spine_paths.append(item.path)
    return navigation[0].path, tuple(spine_paths)


def _validate_navigation_document(
    root: SafeElementTree.Element,
    *,
    navigation_path: str,
    spine_paths: tuple[str, ...],
) -> None:
    toc_nodes = tuple(
        element
        for element in root.iter()
        if _qualified_name(element.tag) == (_XHTML_NAMESPACE, "nav")
        and "toc" in element.attrib.get(f"{{{_EPUB_NAMESPACE}}}type", "").split()
    )
    if len(toc_nodes) != 1:
        raise ConversionError("La navegación del EPUB no contiene una tabla de contenidos única.")
    targets: set[str] = set()
    for element in toc_nodes[0].iter():
        if _qualified_name(element.tag) != (_XHTML_NAMESPACE, "a"):
            continue
        href = element.attrib.get("href", "")
        target, _fragment, external = _resolve_archive_reference(navigation_path, href)
        if external:
            raise ConversionError("La tabla de contenidos del EPUB contiene un enlace remoto.")
        targets.add(target)
    expected = {
        path for path in spine_paths if PurePosixPath(path).name.casefold() != "cover.xhtml"
    }
    if not expected or not expected.issubset(targets):
        raise ConversionError("La tabla de contenidos del EPUB no incluye todos los capítulos.")


def _document_ids(root: SafeElementTree.Element, path: str) -> frozenset[str]:
    identifiers: set[str] = set()
    for element in root.iter():
        identifier = element.attrib.get("id") or element.attrib.get(f"{{{_XML_NAMESPACE}}}id")
        if not identifier:
            continue
        if identifier in identifiers:
            raise ConversionError(f'El documento "{path}" contiene anclas duplicadas.')
        identifiers.add(identifier)
    return frozenset(identifiers)


def _validate_document_references(
    root: SafeElementTree.Element,
    *,
    document_path: str,
    archive_names: frozenset[str],
    manifest_paths: frozenset[str],
    xml_documents: dict[str, tuple[SafeElementTree.Element, frozenset[str]]],
) -> None:
    for element in root.iter():
        namespace, local_name = _qualified_name(element.tag)
        if local_name.casefold() in _FORBIDDEN_XML_ELEMENTS or (
            namespace == _SVG_NAMESPACE and local_name == "foreignObject"
        ):
            raise ConversionError(f'El documento "{document_path}" contiene contenido activo.')
        for raw_name, value in element.attrib.items():
            attribute = _qualified_name(raw_name)[1].casefold()
            if attribute.startswith("on"):
                raise ConversionError(
                    f'El documento "{document_path}" contiene contenido ejecutable.'
                )
            if attribute not in {"href", "src"}:
                continue
            target, fragment, external = _resolve_archive_reference(document_path, value)
            if external:
                scheme = urlsplit(value.strip()).scheme.casefold()
                if attribute == "src" or (scheme and scheme not in _ALLOWED_EXTERNAL_LINK_SCHEMES):
                    raise ConversionError(
                        f'El documento "{document_path}" contiene un recurso remoto no seguro.'
                    )
                continue
            if target not in archive_names or target not in manifest_paths:
                raise ConversionError(
                    f'El documento "{document_path}" referencia un recurso que no existe.'
                )
            if fragment:
                target_document = xml_documents.get(target)
                if target_document is None or fragment not in target_document[1]:
                    raise ConversionError(
                        f'El documento "{document_path}" referencia un ancla inexistente.'
                    )


def _resolve_archive_reference(
    current_document: str,
    reference: str,
) -> tuple[str, str, bool]:
    cleaned = reference.strip()
    if not cleaned or "\0" in cleaned or "\\" in cleaned:
        raise ConversionError("El EPUB generado contiene una referencia interna no válida.")
    try:
        parsed = urlsplit(cleaned)
    except ValueError as exc:
        raise ConversionError(
            "El EPUB generado contiene una referencia interna no válida."
        ) from exc
    if parsed.scheme or parsed.netloc:
        return "", "", True
    if parsed.query:
        raise ConversionError("El EPUB generado contiene una referencia interna con parámetros.")
    raw_path = unquote(parsed.path)
    fragment = unquote(parsed.fragment)
    if raw_path.startswith("/"):
        raise ConversionError("El EPUB generado contiene una referencia interna absoluta.")
    base = PurePosixPath(current_document).parent.parts
    parts = list(base)
    for part in PurePosixPath(raw_path).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise ConversionError("El EPUB generado contiene una ruta fuera del libro.")
            parts.pop()
            continue
        parts.append(part)
    target = "/".join(parts) if raw_path else current_document
    _validate_archive_name(target)
    return target, fragment, False


def _parse_xml_entry(archive: ZipFile, name: str) -> SafeElementTree.Element:
    try:
        return SafeElementTree.fromstring(archive.read(name))
    except KeyError as exc:
        raise ConversionError(f'El recurso declarado "{name}" no existe en el EPUB.') from exc
    except (SafeElementTree.ParseError, DefusedXmlException, ValueError) as exc:
        raise ConversionError(f'El recurso XML "{name}" no es válido.') from exc


def _validate_archive_name(name: str) -> None:
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or "\0" in name
        or any(part in {"", ".", ".."} for part in PurePosixPath(name).parts)
    ):
        raise ConversionError("El EPUB generado contiene una ruta interna no válida.")


def _qualified_name(name: str) -> tuple[str, str]:
    if name.startswith("{") and "}" in name:
        namespace, local_name = name[1:].split("}", 1)
        return namespace, local_name
    return "", name
