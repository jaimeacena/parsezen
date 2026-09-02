"""Build a portable EPUB 3 book from Parsezen's Markdown document model."""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from difflib import SequenceMatcher
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
_PDF_OUTLINE_MARKER_PATTERN = re.compile(
    r"<!--\s*PZDOC PDF OUTLINE (?P<level>[1-6])\s*-->",
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
_DOCUMENT_TOC_LABEL_PATTERN = re.compile(
    r'<td\s+class=["\']toc-label toc-level-(?P<level>[0-2])["\']\s*>'
    r"(?P<label>.*?)</td>",
    re.IGNORECASE | re.DOTALL,
)
_BARE_XML_AMPERSAND_PATTERN = re.compile(
    r"&(?!amp;|lt;|gt;|apos;|quot;|#\d+;|#x[0-9a-f]+;)",
    re.IGNORECASE,
)
_SAFE_TABLE_TAGS = frozenset(
    {"table", "thead", "tbody", "tr", "th", "td", "br", "strong", "em", "a"}
)
_MAX_CHAPTER_CHARACTERS = 500_000
_MIN_CHAPTER_CHARACTERS = 1_500
_MIN_STRONG_CHAPTER_CHARACTERS = 240
_MAX_CHAPTERS = 250
_MAX_AUTOMATIC_CHAPTER_HEADINGS = 128
_MAX_AUTOMATIC_NAVIGATION_HEADINGS = 250
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
    toc_level: int | None = None


@dataclass(frozen=True, slots=True)
class EpubOutlineEntry:
    """One heading in the review outline and the chapter that owns it."""

    level: int
    title: str
    chapter_number: int
    starts_chapter: bool
    include_in_navigation: bool = True
    navigation_level: int | None = None


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
    fragment: str | None = None


@dataclass(slots=True)
class _MutableNavigationNode:
    """Internal outline node while heading levels are being nested."""

    title: str
    filename: str
    fragment: str
    children: list[_MutableNavigationNode]


@dataclass(slots=True)
class _ChapterNavigationGroup:
    """One chapter-level navigation item before cross-chapter nesting."""

    chapter: EpubChapterPlan | None
    nodes: tuple[EpubNavigationNode, ...]
    children: list[_ChapterNavigationGroup]


def classify_heading_role(title: str) -> str | None:
    """Classify only explicit, numbered container or chapter titles."""

    cleaned = re.sub(r"[*_`~\[\]]", "", title).strip()
    if _CONTAINER_TITLE_PATTERN.match(cleaned) and _structural_heading_is_explicit(
        cleaned,
        _CONTAINER_TITLE_PATTERN,
    ):
        return "container"
    if _CHAPTER_TITLE_PATTERN.match(cleaned) and _structural_heading_is_explicit(
        cleaned,
        _CHAPTER_TITLE_PATTERN,
    ):
        return "chapter"
    return None


def _structural_heading_is_explicit(title: str, pattern: re.Pattern[str]) -> bool:
    """Reject prose sentences that merely begin with a numbered Part or Chapter."""

    match = pattern.match(title)
    if match is None:
        return False
    tail = title[match.end() :].strip()
    if not tail:
        return True
    if tail[0] in ",;!?":
        return False
    if tail[0] in ":\u2013\u2014-":
        candidate = tail[1:].strip()
        return bool(candidate) and len(candidate.split()) <= 24
    if tail[0] == ".":
        candidate = tail[1:].strip()
        words = re.findall(r"[^\W\d_]+", candidate, re.UNICODE)
        if not candidate or len(words) > 12 or re.search(r"[.!?;]", candidate):
            return False
        significant = [word for word in words if len(word) > 2]
        title_words = sum(word[:1].isupper() for word in significant)
        return len(words) <= 4 or bool(significant) and title_words / len(significant) >= 0.6
    words = re.findall(r"[^\W\d_]+", tail, re.UNICODE)
    significant = [word for word in words if len(word) > 2]
    title_words = sum(word[:1].isupper() for word in significant)
    return (
        bool(words)
        and len(words) <= 16
        and not re.search(r"[,;.!?]", tail)
        and bool(significant)
        and title_words / len(significant) >= 0.6
    )


def _is_strong_chapter_title(title: str) -> bool:
    return (
        classify_heading_role(title) is not None
        or re.match(
            r"^(?:introduction|introducci[o\u00f3]n|preface|pr[o\u00f3]logo|foreword|"
            r"append(?:ix|ices)|ap[e\u00e9]ndic(?:e|es))\b",
            title,
            re.IGNORECASE,
        )
        is not None
    )


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
        navigation=_navigation_from_plan(plan),
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
    markdown = _separate_adjacent_markdown_headings(markdown)
    semantic_document = analyze_markdown(markdown)
    explicit_chapters = _explicit_chapters(markdown)
    if explicit_chapters is None:
        blocks = _markdown_blocks(markdown)
        preferred_level = _preferred_heading_level(blocks, semantic_document)
        toc_titles = _toc_titles(semantic_document)
        toc_is_reliable = _toc_evidence_is_reliable(
            blocks,
            semantic_document,
            toc_titles,
            preferred_level,
        )
        strong_headings = _strong_chapter_headings(
            blocks,
            semantic_document,
            preferred_level,
            toc_is_reliable=toc_is_reliable,
        )
        forced_headings = _forced_chapter_headings(blocks, semantic_document)
        preferred_headings = _preferred_chapter_headings(
            blocks,
            semantic_document,
            preferred_level,
            toc_titles,
            toc_is_reliable=toc_is_reliable,
        )
        chapters = _chapters_from_blocks(
            blocks,
            fallback_title,
            preferred_headings,
            strong_headings,
            forced_headings,
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
            toc_level=chapter.toc_level,
        )
        for chapter in chapters
    )
    toc_heading_levels = _toc_heading_levels(semantic_document)
    chapters = tuple(
        EpubChapterPlan(
            filename=chapter.filename,
            title=chapter.title,
            markdown=chapter.markdown,
            role=(
                chapter.role
                or (
                    "chapter"
                    if _is_numbered_toc_chapter_title(
                        chapter.title,
                        _toc_heading_level(chapter.title, toc_heading_levels),
                    )
                    else None
                )
            ),
            toc_level=_toc_heading_level(chapter.title, toc_heading_levels),
        )
        for chapter in chapters
    )
    chapters = _infer_toc_chapter_roles(chapters)
    headings_by_chapter = tuple(_markdown_headings(chapter.markdown) for chapter in chapters)
    automatic_navigation_level = min(4, (preferred_level or 1) + 2)
    automatic_navigation_count = sum(
        1
        for headings in headings_by_chapter
        for level, _title, _outline_level in headings
        if level <= automatic_navigation_level
    )
    expose_shallow_hierarchy = automatic_navigation_count <= _MAX_AUTOMATIC_NAVIGATION_HEADINGS
    outline: list[EpubOutlineEntry] = []
    toc_titles = _toc_titles(semantic_document)
    toc_navigation_matches = sum(
        1
        for headings in headings_by_chapter
        for level, title, _outline_level in headings
        if level <= automatic_navigation_level and _heading_matches_toc(title, toc_titles)
    )
    required_toc_navigation_matches = min(
        8,
        max(2, (automatic_navigation_count + 19) // 20),
    )
    toc_navigation_is_reliable = (
        bool(toc_titles) and toc_navigation_matches >= required_toc_navigation_matches
    )
    outline_navigation_matches = sum(
        1
        for headings in headings_by_chapter
        for level, _title, outline_level in headings
        if outline_level is not None and level <= automatic_navigation_level
    )
    outline_navigation_is_reliable = outline_navigation_matches >= required_toc_navigation_matches
    front_toc_end = _front_structured_toc_end_position(semantic_document)
    semantic_heading_evidence = iter(
        (block.role, block.position)
        for block in semantic_document.blocks
        for _heading in _markdown_headings(block.markdown)
    )
    for chapter_number, (chapter, chapter_headings) in enumerate(
        zip(chapters, headings_by_chapter, strict=True),
        start=1,
    ):
        first_heading = True
        combined_subtitle = _combined_numbered_chapter_subtitle(chapter.markdown)
        chapter_marker = _chapter_marker_key(chapter.title)
        step_navigation_level: int | None = None
        for level, raw_title, outline_level in chapter_headings:
            semantic_role, semantic_position = next(
                semantic_heading_evidence,
                (SemanticRole.BODY, len(semantic_document.blocks)),
            )
            title = re.sub(r"[*_`~\[\]]", "", raw_title).strip()
            if not title:
                continue
            starts_chapter = first_heading
            toc_level = _toc_heading_level(title, toc_heading_levels)
            navigation_level = toc_level + 1 if toc_level is not None else outline_level
            if _step_heading_key(title) is not None:
                if step_navigation_level is None:
                    step_navigation_level = navigation_level or level
                navigation_level = step_navigation_level
            title_component = combined_subtitle is not None and _normalized_heading_title(
                title
            ) == _normalized_heading_title(combined_subtitle)
            repeated_bare_chapter_marker = (
                not starts_chapter
                and _bare_chapter_marker_key(title) is not None
                and _bare_chapter_marker_key(title) == chapter_marker
            )
            semantic_navigation_allowed = (
                semantic_role is not SemanticRole.TOC
                and semantic_role is not SemanticRole.PROVENANCE
                and (front_toc_end is None or semantic_position > front_toc_end)
                and not (
                    semantic_role is SemanticRole.FRONT_MATTER and semantic_document.toc_blocks > 0
                )
            )
            outline.append(
                EpubOutlineEntry(
                    level=level,
                    title=title[:160],
                    chapter_number=chapter_number,
                    starts_chapter=starts_chapter,
                    include_in_navigation=(
                        semantic_navigation_allowed
                        and not title_component
                        and not repeated_bare_chapter_marker
                        and (
                            starts_chapter
                            or outline_level is not None
                            or (
                                expose_shallow_hierarchy
                                and not toc_navigation_is_reliable
                                and not outline_navigation_is_reliable
                                and level <= automatic_navigation_level
                            )
                            or _heading_matches_toc(title, toc_titles)
                            or _is_strong_chapter_title(title)
                        )
                    ),
                    navigation_level=navigation_level,
                )
            )
            first_heading = False
    return EpubPlan(chapters, tuple(outline), preferred_level)


def _separate_adjacent_markdown_headings(markdown: str) -> str:
    """Expose valid ATX headings that directly follow a paragraph to block analysis."""

    lines = markdown.splitlines(keepends=True)
    separated: list[str] = []
    fence: str | None = None
    for line in lines:
        fence_match = _FENCE_PATTERN.match(line)
        if fence_match is not None:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
        content = line.rstrip("\r\n")
        if (
            fence is None
            and _HEADING_PATTERN.fullmatch(content) is not None
            and separated
            and separated[-1].strip()
        ):
            separated.append("\r\n" if line.endswith("\r\n") else "\n")
        separated.append(line)
    return "".join(separated)


def _markdown_headings(markdown: str) -> tuple[tuple[int, str, int | None], ...]:
    """Return actual Markdown headings while ignoring examples inside fenced code."""

    headings: list[tuple[int, str, int | None]] = []
    fence: str | None = None
    pending_outline_level: int | None = None
    for line in markdown.splitlines():
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
        outline_marker = _PDF_OUTLINE_MARKER_PATTERN.fullmatch(line.strip())
        if outline_marker is not None:
            pending_outline_level = int(outline_marker.group("level"))
            continue
        heading = _HEADING_PATTERN.fullmatch(line)
        if heading is not None:
            headings.append((len(heading.group(1)), heading.group(2), pending_outline_level))
            pending_outline_level = None
        elif line.strip():
            pending_outline_level = None
    return tuple(headings)


def _heading_fragment(chapter_number: int, heading_number: int) -> str:
    return f"section-{chapter_number:04d}-{heading_number:04d}"


def _navigation_from_plan(plan: EpubPlan) -> tuple[EpubNavigationNode, ...]:
    """Expose the reviewed heading hierarchy without inventing new chapter boundaries."""

    entries_by_chapter: dict[int, list[EpubOutlineEntry]] = {}
    for entry in plan.outline:
        entries_by_chapter.setdefault(entry.chapter_number, []).append(entry)

    chapter_groups: list[_ChapterNavigationGroup] = []
    for chapter_number, chapter in enumerate(plan.chapters, start=1):
        roots: list[_MutableNavigationNode] = []
        stack: list[tuple[int, _MutableNavigationNode]] = []
        seen_titles = {_normalized_heading_title(chapter.title)}
        chapter_is_in_navigation = True
        for heading_number, entry in enumerate(
            entries_by_chapter.get(chapter_number, ()),
            start=1,
        ):
            normalized_title = _normalized_heading_title(entry.title)
            if entry.starts_chapter:
                chapter_is_in_navigation = entry.include_in_navigation
                continue
            if not entry.include_in_navigation or normalized_title in seen_titles:
                continue
            seen_titles.add(normalized_title)
            node = _MutableNavigationNode(
                entry.title,
                chapter.filename,
                _heading_fragment(chapter_number, heading_number),
                [],
            )
            navigation_level = entry.navigation_level or entry.level
            while stack and stack[-1][0] >= navigation_level:
                stack.pop()
            if stack:
                stack[-1][1].children.append(node)
            else:
                roots.append(node)
            stack.append((navigation_level, node))

        def freeze(node: _MutableNavigationNode) -> EpubNavigationNode:
            return EpubNavigationNode(
                node.title,
                node.filename,
                tuple(freeze(child) for child in node.children),
                node.fragment,
            )

        frozen_roots = tuple(freeze(node) for node in roots)
        if chapter_is_in_navigation:
            chapter_groups.append(
                _ChapterNavigationGroup(
                    chapter,
                    (
                        EpubNavigationNode(
                            chapter.title,
                            chapter.filename,
                            frozen_roots,
                        ),
                    ),
                    [],
                )
            )
        else:
            chapter_groups.append(_ChapterNavigationGroup(None, frozen_roots, []))

    nested_groups = _nest_explicit_chapter_navigation(
        _nest_toc_chapter_navigation(tuple(chapter_groups))
    )
    return tuple(
        node for group in nested_groups for node in _freeze_chapter_navigation_group(group)
    )


def _nest_toc_chapter_navigation(
    groups: tuple[_ChapterNavigationGroup, ...],
) -> tuple[_ChapterNavigationGroup, ...]:
    """Apply only exact printed-contents depth to consecutive chapter entries."""

    roots: list[_ChapterNavigationGroup] = []
    stack: list[tuple[int, _ChapterNavigationGroup]] = []
    for group in groups:
        level = group.chapter.toc_level if group.chapter is not None else None
        if level is None:
            roots.append(group)
            stack.clear()
            continue
        while stack and stack[-1][0] >= level:
            stack.pop()
        if stack:
            stack[-1][1].children.append(group)
        else:
            roots.append(group)
        stack.append((level, group))
    return tuple(roots)


def _nest_explicit_chapter_navigation(
    groups: tuple[_ChapterNavigationGroup, ...],
) -> tuple[_ChapterNavigationGroup, ...]:
    """Nest a part around two or more explicit chapter siblings as a safe fallback."""

    for group in groups:
        group.children = list(_nest_explicit_chapter_navigation(tuple(group.children)))

    roots: list[_ChapterNavigationGroup] = []
    index = 0
    while index < len(groups):
        group = groups[index]
        if group.chapter is None or group.chapter.role != "container" or group.children:
            roots.append(group)
            index += 1
            continue
        end = index + 1
        while (
            end < len(groups)
            and groups[end].chapter is not None
            and groups[end].chapter.role == "chapter"
        ):
            end += 1
        if end - index - 1 >= 2:
            group.children = list(groups[index + 1 : end])
            roots.append(group)
            index = end
            continue
        roots.append(group)
        index += 1
    return tuple(roots)


def _freeze_chapter_navigation_group(
    group: _ChapterNavigationGroup,
) -> tuple[EpubNavigationNode, ...]:
    nested = tuple(
        node for child in group.children for node in _freeze_chapter_navigation_group(child)
    )
    if group.chapter is None or len(group.nodes) != 1:
        return (*group.nodes, *nested)
    node = group.nodes[0]
    return (replace(node, children=(*node.children, *nested)),)


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
    preferred_heading_positions: frozenset[int],
    strong_heading_positions: frozenset[int] = frozenset(),
    forced_heading_positions: frozenset[int] = frozenset(),
) -> tuple[EpubChapterPlan, ...]:
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0
    for position, block in enumerate(blocks):
        should_split_for_heading = position in preferred_heading_positions and (
            current_size >= _MIN_CHAPTER_CHARACTERS
        )
        should_split_for_strong_heading = position in strong_heading_positions and (
            position in forced_heading_positions or current_size >= _MIN_STRONG_CHAPTER_CHARACTERS
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
            if title is not None and _is_strong_chapter_title(title):
                score += 5.0
            if title is not None and _heading_matches_toc(title, toc_titles):
                score += 3.0
            scores[level] = scores.get(level, 0.0) + score
    candidates = {
        level: scores.get(level, 0.0) / count
        for level, count in counts.items()
        if level <= 2 and 2 <= count <= _MAX_AUTOMATIC_CHAPTER_HEADINGS
    }
    if candidates:
        # Level-three and deeper headings are normally sections, even when the printed contents
        # lists them. Explicit Chapter/Part labels still split independently through the strong
        # heading path below, so choosing only the two shallowest levels protects long manuals from
        # becoming one XHTML file per subsection.
        return min(candidates, key=lambda level: (-candidates[level], level))
    return None


def _preferred_chapter_headings(
    blocks: list[str],
    semantic_document: SemanticDocument,
    preferred_level: int | None,
    toc_titles: frozenset[str],
    *,
    toc_is_reliable: bool,
) -> frozenset[int]:
    """Select safe automatic splits without turning front matter or callouts into chapters."""

    if preferred_level is None:
        return frozenset()
    front_toc_end = _front_structured_toc_end_position(semantic_document)
    selected: set[int] = set()
    for position, block in enumerate(blocks):
        if position >= len(semantic_document.blocks):
            continue
        semantic_role = semantic_document.blocks[position].role
        if semantic_role in {
            SemanticRole.FRONT_MATTER,
            SemanticRole.PROVENANCE,
            SemanticRole.TOC,
        }:
            continue
        if front_toc_end is not None and position <= front_toc_end:
            continue
        if _block_heading_level(block) != preferred_level:
            continue
        title = _block_heading_title(block)
        if title is None:
            continue
        if toc_is_reliable and not _heading_matches_toc(title, toc_titles):
            continue
        selected.add(position)
    return frozenset(selected)


def _toc_evidence_is_reliable(
    blocks: list[str],
    semantic_document: SemanticDocument,
    toc_titles: frozenset[str],
    preferred_level: int | None,
) -> bool:
    """Require several independent body matches before the printed index controls splitting."""

    if not toc_titles:
        return False
    front_toc_end = _front_structured_toc_end_position(semantic_document)
    body_headings = [
        title
        for position, block in enumerate(blocks)
        if position < len(semantic_document.blocks)
        and (front_toc_end is None or position > front_toc_end)
        and semantic_document.blocks[position].role
        not in {
            SemanticRole.FRONT_MATTER,
            SemanticRole.PROVENANCE,
            SemanticRole.TOC,
        }
        and (
            preferred_level is None
            or (level := _block_heading_level(block)) is not None
            and level <= preferred_level
        )
        and (title := _block_heading_title(block)) is not None
    ]
    matches = sum(_heading_matches_toc(title, toc_titles) for title in body_headings)
    required = min(8, max(2, (len(body_headings) + 19) // 20))
    return matches >= required


def _front_structured_toc_end_position(
    semantic_document: SemanticDocument,
) -> int | None:
    """Bound a front index without treating a later in-book contents list as front matter."""

    structured = [
        block
        for block in semantic_document.blocks
        if _DOCUMENT_TOC_OPENING_PATTERN.search(block.markdown) is not None
    ]
    if not structured:
        return None
    first = structured[0]
    if first.page_number is not None:
        if first.page_number > 12:
            return None
        eligible = [
            block
            for block in structured
            if block.page_number is not None and block.page_number <= 12
        ]
    else:
        if first.position > 40:
            return None
        eligible = [block for block in structured if block.position <= 80]
    return max((block.position for block in eligible), default=None)


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
    preferred_level: int | None,
    *,
    toc_is_reliable: bool = False,
) -> frozenset[int]:
    toc_titles = _toc_titles(semantic_document)
    toc_heading_levels = _toc_heading_levels(semantic_document)
    toc_container_titles = _toc_container_titles(semantic_document)
    selected: set[int] = set()
    had_front_matter = semantic_document.front_matter_blocks > 0
    first_body_heading_added = False
    selected_titles: set[str] = set()
    front_toc_end = _front_structured_toc_end_position(semantic_document)
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
        if front_toc_end is not None and position <= front_toc_end:
            continue
        normalized = _normalized_heading_title(title)
        level = _block_heading_level(block)
        series_key = _toc_series_key(title)
        toc_container = bool(_toc_match_keys(title).intersection(toc_container_titles))
        toc_backed_boundary = (
            (
                preferred_level is not None
                and level is not None
                and _heading_matches_toc(title, toc_titles)
                and (level <= preferred_level or series_key in toc_titles)
            )
            or _is_numbered_toc_chapter_title(
                title,
                _toc_heading_level(title, toc_heading_levels),
            )
            or toc_container
        )
        is_boundary = (
            _is_strong_chapter_title(title)
            or toc_backed_boundary
            or (had_front_matter and not first_body_heading_added and not toc_is_reliable)
        )
        if is_boundary and normalized not in selected_titles:
            selected.add(position)
            selected_titles.add(normalized)
        first_body_heading_added = True
    return frozenset(selected)


def _forced_chapter_headings(
    blocks: list[str],
    semantic_document: SemanticDocument,
) -> frozenset[int]:
    """Split index-backed numbered chapters even after a deliberately short part page."""

    toc_heading_levels = _toc_heading_levels(semantic_document)
    toc_titles = _toc_titles(semantic_document)
    toc_container_titles = _toc_container_titles(semantic_document)
    front_toc_end = _front_structured_toc_end_position(semantic_document)
    selected: set[int] = set()
    previous_toc_level: int | None = None
    previous_was_container = False
    for position, block in enumerate(blocks):
        title = _block_heading_title(block)
        if title is None or position >= len(semantic_document.blocks):
            continue
        if semantic_document.blocks[position].role in {
            SemanticRole.FRONT_MATTER,
            SemanticRole.PROVENANCE,
            SemanticRole.TOC,
        }:
            continue
        if front_toc_end is not None and position <= front_toc_end:
            continue
        toc_level = _toc_heading_level(title, toc_heading_levels)
        toc_container = bool(_toc_match_keys(title).intersection(toc_container_titles))
        follows_short_container = bool(
            previous_was_container
            and previous_toc_level is not None
            and toc_level is not None
            and toc_level > previous_toc_level
            and _heading_matches_toc(title, toc_titles)
        )
        if (
            _is_numbered_toc_chapter_title(title, toc_level)
            or follows_short_container
            or toc_container
            or (_flat_toc_container_title(title) and _heading_matches_toc(title, toc_titles))
        ):
            selected.add(position)
        if toc_level is not None and _heading_matches_toc(title, toc_titles):
            previous_toc_level = toc_level
            previous_was_container = _flat_toc_container_title(title)
    return frozenset(selected)


def _toc_titles(semantic_document: SemanticDocument | None) -> frozenset[str]:
    if semantic_document is None:
        return frozenset()
    titles: set[str] = set()
    series_counts: Counter[str] = Counter()

    def add_title(value: str) -> None:
        for key in _toc_match_keys(value):
            if key.startswith("series:"):
                series_counts[key] += 1
            else:
                titles.add(key)

    for block in semantic_document.blocks:
        if (
            block.role is not SemanticRole.TOC
            and _DOCUMENT_TOC_OPENING_PATTERN.search(block.markdown) is None
        ):
            continue
        table_labels = [
            unescape(re.sub(r"<[^>]+>", " ", match.group("label")))
            for match in _DOCUMENT_TOC_LABEL_PATTERN.finditer(block.markdown)
        ]
        for label in table_labels:
            add_title(" ".join(label.split()))
        for line in block.markdown.splitlines() if not table_labels else ():
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
                add_title(normalized)
    titles.update(key for key, count in series_counts.items() if count >= 2)
    return frozenset(titles)


def _toc_heading_levels(semantic_document: SemanticDocument) -> dict[str, int]:
    """Return only unambiguous title levels preserved by Parsezen's printed contents table."""

    records: list[tuple[str, str, int]] = []
    for block in semantic_document.blocks:
        if (
            block.role is not SemanticRole.TOC
            and _DOCUMENT_TOC_OPENING_PATTERN.search(block.markdown) is None
        ):
            continue
        for match in _DOCUMENT_TOC_LABEL_PATTERN.finditer(block.markdown):
            raw_label = match.group("label")
            visible = unescape(re.sub(r"<[^>]+>", " ", raw_label))
            label = " ".join(visible.split())
            if label:
                records.append((label, raw_label, int(match.group("level"))))
    resolved_levels = _resolved_toc_record_levels(records)
    candidates: dict[str, list[int]] = {}
    for (label, _raw_label, _source_level), level in zip(
        records,
        resolved_levels,
        strict=True,
    ):
        for key in _toc_match_keys(label):
            candidates.setdefault(key, []).append(level)
    return {
        title: levels[0]
        for title, levels in candidates.items()
        if len(set(levels)) == 1 and (not title.startswith("series:") or len(levels) >= 2)
    }


def _toc_container_titles(semantic_document: SemanticDocument) -> frozenset[str]:
    """Return index entries that own at least one immediately deeper following entry."""

    records: list[tuple[str, str, int]] = []
    for block in semantic_document.blocks:
        if (
            block.role is not SemanticRole.TOC
            and _DOCUMENT_TOC_OPENING_PATTERN.search(block.markdown) is None
        ):
            continue
        for match in _DOCUMENT_TOC_LABEL_PATTERN.finditer(block.markdown):
            raw_label = match.group("label")
            visible = unescape(re.sub(r"<[^>]+>", " ", raw_label))
            label = " ".join(visible.split())
            if label:
                records.append((label, raw_label, int(match.group("level"))))
    levels = _resolved_toc_record_levels(records)
    keys: set[str] = set()
    for index, ((label, _raw, _source_level), level) in enumerate(
        zip(records, levels, strict=True)
    ):
        next_level = levels[index + 1] if index + 1 < len(levels) else None
        if next_level is not None and next_level > level:
            keys.update(_toc_match_keys(label))
    return frozenset(keys)


def _resolved_toc_record_levels(records: list[tuple[str, str, int]]) -> tuple[int, ...]:
    """Recover a conservative two-level hierarchy from a visually styled index."""

    source_levels = tuple(level for _label, _raw, level in records)
    if not records:
        return source_levels
    container_flags = tuple(_flat_toc_container_title(label) for label, _raw, _ in records)
    if sum(container_flags) < 2 or len(records) - sum(container_flags) < 4:
        return source_levels
    all_source_levels_are_flat = all(level == 0 for level in source_levels)
    root_flags = tuple(
        _flat_toc_root_title(label)
        or (all_source_levels_are_flat and _toc_label_is_fully_emphasized(raw_label))
        for label, raw_label, _level in records
    )
    levels: list[int] = []
    inside_container = False
    for source_level, is_root, is_container in zip(
        source_levels,
        root_flags,
        container_flags,
        strict=True,
    ):
        if is_root:
            levels.append(0)
            inside_container = is_container
        else:
            levels.append(max(1, source_level) if inside_container else source_level)
    return tuple(levels)


def _flat_toc_container_title(title: str) -> bool:
    cleaned = re.sub(r"[*_`~\[\]]", "", title).strip()
    return bool(
        classify_heading_role(cleaned) == "container"
        or re.match(r"^[IVXLCDM]{1,8}\s*[-.:]\s*\S", cleaned)
        or re.match(
            r"^(?:(?:appendices|ap[eé]ndices)|"
            r"(?:appendix|ap[eé]ndice)\s+(?:\d{1,3}|[IVXLCDM]{1,8})\b)",
            cleaned,
            re.IGNORECASE,
        )
        or re.match(
            r"^(?:tables?(?:\s+of\s+.+)?|tablas?(?:\s+de\s+.+)?)$",
            cleaned,
            re.IGNORECASE,
        )
    )


def _toc_label_is_fully_emphasized(raw_label: str) -> bool:
    cleaned = raw_label.strip()
    anchor = re.fullmatch(r"<a\b[^>]*>(?P<label>.*)</a>", cleaned, re.IGNORECASE)
    if anchor is not None:
        cleaned = anchor.group("label").strip()
    return bool(
        re.fullmatch(
            r"<(?:strong|b)\b[^>]*>.*</(?:strong|b)>",
            cleaned,
            re.IGNORECASE,
        )
    )


def _flat_toc_root_title(title: str) -> bool:
    cleaned = re.sub(r"[*_`~\[\]]", "", title).strip()
    return bool(
        _flat_toc_container_title(cleaned)
        or re.fullmatch(
            r"(?:bibliography|bibliograf[ií]a|references|referencias|"
            r"glossary|glosario|index|[ií]ndice)",
            cleaned,
            re.IGNORECASE,
        )
    )


def _toc_match_keys(title: str) -> frozenset[str]:
    normalized = _normalized_heading_title(title)
    if not normalized:
        return frozenset()
    compact = re.sub(r"[^\w]+", "", normalized, flags=re.UNICODE)
    structural = re.sub(
        r"^(?:chapter|cap[i\u00ed]tulo|chapitre|cap)\s+",
        "",
        normalized,
        flags=re.IGNORECASE,
    )
    structural_compact = re.sub(r"[^\w]+", "", structural, flags=re.UNICODE)
    keys = {
        normalized,
        f"compact:{compact}",
        f"structural:{structural_compact}",
    }
    keys.update(_toc_numbered_marker_keys(title))
    if series_key := _toc_series_key(title):
        keys.add(series_key)
    keys.update(_toc_container_subtitle_keys(title))
    return frozenset(keys)


def _toc_series_key(title: str) -> str | None:
    """Return a family key for compact numbered series such as ``Topic II: ...``."""

    plain = unescape(re.sub(r"<[^>]+>", " ", title))
    plain = re.sub(r"[*_`~\[\]]", "", plain).strip()
    structural = re.sub(
        r"^(?:chapter|cap[ií]tulo|chapitre|cap\.)\s+",
        "chapter ",
        plain,
        flags=re.IGNORECASE,
    )
    prefix = re.split(r"\s*[:\u2013\u2014]\s*", structural, maxsplit=1)[0]
    normalized = _normalized_heading_title(prefix)
    tokens = normalized.split()
    if len(tokens) < 2:
        return None
    ordinal = tokens[-1]
    ordinal_pattern = r"(?:\d{1,3}|[ivxlcdm1lhke]{1,4})"
    if re.fullmatch(ordinal_pattern, ordinal, re.IGNORECASE) is not None:
        family_tokens = tokens[:-1]
    elif re.fullmatch(ordinal_pattern, tokens[1], re.IGNORECASE) is not None:
        family_tokens = tokens[:1]
    else:
        return None
    family = re.sub(r"[^\w]+", "", " ".join(family_tokens), flags=re.UNICODE)
    return f"series:{family}" if 2 <= len(family) <= 60 else None


def _toc_container_subtitle_keys(title: str) -> frozenset[str]:
    """Let a uniquely indexed container match a decorative subtitle-only body heading."""

    plain = unescape(re.sub(r"<[^>]+>", " ", title))
    plain = re.sub(r"[*_`~\[\]]", "", plain).strip()
    normalized = _normalized_heading_title(plain)
    compact = re.sub(r"[^\w]+", "", normalized, flags=re.UNICODE)
    keys = {f"container-subtitle:{compact}"}
    match = re.match(
        r"^(?:part|parte|book|libro|volume|volumen|tomo)\s+"
        r"(?:\d{1,3}|[ivxlcdm]+)\s*[:\u2013\u2014-]\s*(?P<subtitle>\S.*)$",
        plain,
        re.IGNORECASE,
    )
    if match is not None:
        subtitle = re.sub(
            r"[^\w]+",
            "",
            _normalized_heading_title(match.group("subtitle")),
            flags=re.UNICODE,
        )
        keys.add(f"container-subtitle:{subtitle}")
    return frozenset(key for key in keys if len(key.removeprefix("container-subtitle:")) >= 6)


def _toc_numbered_marker_keys(title: str) -> frozenset[str]:
    """Match a numbered body title to a longer index label without using loose substrings."""

    plain = unescape(re.sub(r"<[^>]+>", " ", title))
    plain = re.sub(r"[*_`~\[\]]", "", plain).strip()
    structural = re.sub(
        r"^(?:chapter|cap[ií]tulo|chapitre|cap\.)\s+",
        "",
        plain,
        flags=re.IGNORECASE,
    )
    normalized = _normalized_heading_title(structural)
    if not normalized:
        return frozenset()
    candidates: set[str] = set()
    starts_with_number = re.match(r"^\d{1,3}\b", normalized) is not None
    if starts_with_number and len(normalized.split()) <= 8:
        candidates.add(normalized)
    segments = re.split(r"\s*[:\u2013\u2014]\s*", structural)
    if len(segments) >= 2:
        prefix = _normalized_heading_title(": ".join(segments[:-1]))
        if prefix and (
            re.match(r"^\d{1,3}\b", prefix)
            or re.search(r"\b[ivxlcdm]{1,8}$", prefix, re.IGNORECASE)
        ):
            candidates.add(prefix)
    if re.search(r"\b[ivxlcdm]{1,8}$", normalized, re.IGNORECASE):
        candidates.add(normalized)
    return frozenset(
        "marker:" + re.sub(r"[^\w]+", "", candidate, flags=re.UNICODE)
        for candidate in candidates
        if len(re.sub(r"[^\w]+", "", candidate, flags=re.UNICODE)) >= 2
    )


def _infer_toc_chapter_roles(
    chapters: tuple[EpubChapterPlan, ...],
) -> tuple[EpubChapterPlan, ...]:
    """Use confirmed index depth to distinguish containers from reading chapters."""

    roles: list[str | None] = []
    for index, chapter in enumerate(chapters):
        if chapter.role is not None or chapter.toc_level is None:
            roles.append(chapter.role)
            continue
        next_level = next(
            (
                candidate.toc_level
                for candidate in chapters[index + 1 :]
                if candidate.toc_level is not None
            ),
            None,
        )
        roles.append(
            "container" if next_level is not None and next_level > chapter.toc_level else "chapter"
        )
    return tuple(
        EpubChapterPlan(
            filename=chapter.filename,
            title=chapter.title,
            markdown=chapter.markdown,
            role=role,
            toc_level=chapter.toc_level,
        )
        for chapter, role in zip(chapters, roles, strict=True)
    )


def _heading_matches_toc(title: str, toc_titles: frozenset[str]) -> bool:
    return bool(_toc_match_keys(title).intersection(toc_titles))


def _toc_heading_level(title: str, levels: dict[str, int]) -> int | None:
    matched = {levels[key] for key in _toc_match_keys(title) if key in levels}
    if len(matched) == 1:
        return matched.pop()
    normalized = _normalized_heading_title(title)
    number = re.match(r"^(\d{1,3})\b", normalized)
    compact = re.sub(r"[^\w]+", "", normalized, flags=re.UNICODE)
    if number is None or len(compact) < 8:
        return None
    candidates = sorted(
        (
            SequenceMatcher(None, compact, key.removeprefix("compact:"), autojunk=False).ratio(),
            level,
        )
        for key, level in levels.items()
        if key.startswith("compact:")
        and re.match(r"^(\d{1,3})", key.removeprefix("compact:")) is not None
        and re.match(r"^(\d{1,3})", key.removeprefix("compact:")).group(1) == number.group(1)
    )
    if not candidates or candidates[-1][0] < 0.92:
        return None
    best_score, best_level = candidates[-1]
    if len(candidates) > 1 and candidates[-2][0] >= best_score - 0.02:
        return None
    return best_level


def _is_numbered_toc_chapter_title(title: str, toc_level: int | None) -> bool:
    if toc_level != 0:
        return False
    cleaned = re.sub(r"[*_`~\[\]]", "", title).strip()
    return (
        re.match(
            r"^\d{1,3}\s*[.)]\s*[\"'“”‘’(\[]*\s*[^\W\d_]",
            cleaned,
            re.UNICODE,
        )
        is not None
    )


def _normalized_heading_title(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", title)
    return re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE).strip().casefold()


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
            subtitle = _combined_numbered_chapter_subtitle(markdown)
            if subtitle is not None:
                return f"{text} \u2014 {subtitle}"[:160]
            return text[:160]
    if total == 1:
        return fallback
    return f"Parte {index}"


def _combined_numbered_chapter_subtitle(markdown: str) -> str | None:
    """Use an adjacent subtitle to name a chapter whose first heading is only its number."""

    headings = tuple(_HEADING_PATTERN.finditer(markdown))
    if len(headings) < 2:
        return None
    first, second = headings[:2]
    first_title = re.sub(r"[*_`~\[\]]", "", first.group(2)).strip()
    if classify_heading_role(first_title) != "chapter":
        return None
    match = _CHAPTER_TITLE_PATTERN.match(first_title)
    if match is None or first_title[match.end() :].strip():
        return None
    between = markdown[first.end() : second.start()]
    between = _PDF_PAGE_MARKER_PATTERN.sub("", between)
    between = _PDF_OUTLINE_MARKER_PATTERN.sub("", between)
    between = _EPUB_ANCHOR_COMMENT_PATTERN.sub("", between)
    between = _SAFE_ANCHOR_PATTERN.sub("", between)
    if between.strip() or len(second.group(1)) <= len(first.group(1)):
        return None
    subtitle = re.sub(r"[*_`~\[\]]", "", second.group(2)).strip()
    if not subtitle or len(subtitle.split()) > 24 or re.search(r"[.!?](?:\s|$)", subtitle):
        return None
    return subtitle


def _chapter_marker_key(title: str) -> str | None:
    cleaned = re.sub(r"[*_`~\[\]]", "", title).strip()
    match = _CHAPTER_TITLE_PATTERN.match(cleaned)
    if match is None:
        return None
    marker = cleaned[: match.end()].rstrip(" .:\u2013\u2014-")
    return _normalized_heading_title(marker)


def _bare_chapter_marker_key(title: str) -> str | None:
    marker = _chapter_marker_key(title)
    return marker if marker is not None and marker == _normalized_heading_title(title) else None


def _step_heading_key(title: str) -> str | None:
    normalized = _normalized_heading_title(title)
    match = re.match(r"^step\s+(" + _ORDINAL_HEADING_PATTERN + r")\b", normalized)
    return match.group(1) if match is not None else None


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
    for chapter_number, (chapter, source) in enumerate(
        zip(chapters, prepared, strict=True),
        start=1,
    ):
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
        body = _add_generated_heading_ids(body, chapter_number)
        body = _rewrite_internal_links(body, chapter.filename, anchors_by_chapter)
        rendered.append(_xhtml_document(chapter.title, body, language))
    return tuple(rendered)


def _add_generated_heading_ids(html: str, chapter_number: int) -> str:
    """Give every rendered heading a stable target for nested EPUB navigation."""

    heading_number = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal heading_number
        heading_number += 1
        return (
            f'<h{match.group("level")} id="'
            f'{_heading_fragment(chapter_number, heading_number)}"{match.group("attributes")}'
            ">"
        )

    return re.sub(
        r"<h(?P<level>[1-6])(?P<attributes>(?:\s+[^>]*)?)>",
        replace,
        html,
    )


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
    prepared = _PDF_OUTLINE_MARKER_PATTERN.sub("", prepared)
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
        def href(node: EpubNavigationNode) -> str:
            filename = quote(node.filename, safe="._-~")
            fragment = f"#{quote(node.fragment, safe='._-~')}" if node.fragment is not None else ""
            return escape(f"text/{filename}{fragment}", quote=True)

        return (
            "<ol>"
            + "".join(
                "<li>"
                f'<a href="{href(node)}">'
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
    if not expected or not targets or not targets.issubset(expected):
        raise ConversionError("La tabla de contenidos del EPUB no enlaza capítulos válidos.")


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
