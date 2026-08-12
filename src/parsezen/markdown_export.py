"""Derive portable Markdown layouts from one canonical transformed document."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from parsezen.domain.jobs import MarkdownOrganization

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*$")
_PAGE_MARKER = re.compile(r"<!--\s*PZDOC PDF PAGE (\d+)\s*-->", re.IGNORECASE)
_UNSAFE_FILENAME = re.compile(r"[^a-z0-9]+")
_MARKDOWN_LINK = re.compile(
    r"(?P<prefix>!?\[[^\]]*\]\()(?P<angle><)?(?P<target>[^)>]+)(?(angle)>)(?P<suffix>\))"
)


@dataclass(frozen=True, slots=True)
class MarkdownChapter:
    filename: str
    title: str
    markdown: str


@dataclass(frozen=True, slots=True)
class MarkdownExport:
    primary_markdown: str
    chapters: tuple[MarkdownChapter, ...] = ()


def prepare_markdown_export(
    markdown: str,
    *,
    organization: MarkdownOrganization,
    source_name: str,
    include_metadata: bool,
    include_page_references: bool,
    chapter_directory: str = "",
) -> MarkdownExport:
    """Create a publication variant without mutating the canonical transformed text."""

    canonical = markdown.strip()
    prepared = _page_references(markdown, visible=include_page_references)
    metadata = _metadata_block(prepared, source_name) if include_metadata else ""
    if organization is not MarkdownOrganization.BY_CHAPTER:
        return MarkdownExport(_join(metadata, prepared) if metadata else prepared)

    chapters = tuple(
        MarkdownChapter(
            chapter.filename,
            chapter.title,
            _page_references(chapter.markdown, visible=include_page_references).strip() + "\n",
        )
        for chapter in _split_chapters(canonical)
    )
    if len(chapters) < 2:
        return MarkdownExport(_join(metadata, prepared) if metadata else prepared)

    document_title = _document_title(prepared) or Path(source_name).stem
    index_content = [f"# {document_title}", "", "## Índice", ""]
    index_lines = [metadata.rstrip(), "", *index_content] if metadata else index_content
    for chapter in chapters:
        target = (
            f"{chapter_directory}/{chapter.filename}" if chapter_directory else chapter.filename
        )
        index_lines.append(f"- [{_markdown_link_text(chapter.title)}](<{target}>)")
    return MarkdownExport("\n".join(index_lines).strip() + "\n", chapters)


def rebase_relative_markdown_links(markdown: str) -> str:
    """Move local links one directory up when content is placed in a chapter folder."""

    def replace(match: re.Match[str]) -> str:
        target = match.group("target").strip()
        if (
            not target
            or target.startswith(("#", "/", "\\", "../", "./"))
            or re.match(r"^[a-z][a-z0-9+.-]*:", target, re.IGNORECASE)
        ):
            return match.group(0)
        angle = "<" if match.group("angle") else ""
        closing = ">" if angle else ""
        return f"{match.group('prefix')}{angle}../{target}{closing}{match.group('suffix')}"

    return _MARKDOWN_LINK.sub(replace, markdown)


def _page_references(markdown: str, *, visible: bool) -> str:
    if not visible:
        return markdown

    def replace(match: re.Match[str]) -> str:
        number = match.group(1)
        return f'<a id="pagina-{number}"></a>\n\n> Página original {number}'

    return _PAGE_MARKER.sub(replace, markdown)


def _metadata_block(markdown: str, source_name: str) -> str:
    title = next(
        (
            match.group(2).strip().rstrip("#").strip()
            for line in markdown.splitlines()
            if (match := _HEADING.match(line)) is not None
        ),
        Path(source_name).stem,
    )
    return (
        "---\n"
        f'title: "{_yaml_text(title)}"\n'
        f'source: "{_yaml_text(Path(source_name).name)}"\n'
        "generator: Parsezen\n"
        "---"
    )


def _split_chapters(markdown: str) -> tuple[MarkdownChapter, ...]:
    lines = markdown.splitlines()
    headings = _heading_positions(lines)
    if not headings:
        return ()
    counts: dict[int, int] = {}
    for _, level, _ in headings:
        counts[level] = counts.get(level, 0) + 1
    split_level = next((level for level in sorted(counts) if counts[level] >= 2), None)
    if split_level is None:
        return ()
    heading_starts = [
        (position, title) for position, level, title in headings if level == split_level
    ]
    starts = [
        (_include_leading_page_marker(lines, position), title) for position, title in heading_starts
    ]
    if len(starts) < 2:
        return ()

    chapters: list[MarkdownChapter] = []
    preamble = "\n".join(lines[: starts[0][0]]).strip()
    if _contains_prose(preamble):
        chapters.append(_chapter(len(chapters) + 1, "Introducción", preamble))
    for index, (start, title) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(lines)
        content = "\n".join(lines[start:end]).strip()
        if content:
            chapters.append(_chapter(len(chapters) + 1, title, content))
    return tuple(chapters)


def _heading_positions(lines: list[str]) -> list[tuple[int, int, str]]:
    positions: list[tuple[int, int, str]] = []
    fence: str | None = None
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            fence = None if fence == marker else marker if fence is None else fence
            continue
        if fence is not None:
            continue
        match = _HEADING.match(line)
        if match is not None:
            positions.append(
                (index, len(match.group(1)), match.group(2).strip().rstrip("#").strip())
            )
    return positions


def _include_leading_page_marker(lines: list[str], heading_position: int) -> int:
    position = heading_position - 1
    while position >= 0 and not lines[position].strip():
        position -= 1
    if position >= 0 and _PAGE_MARKER.fullmatch(lines[position].strip()):
        return position
    return heading_position


def _chapter(number: int, title: str, markdown: str) -> MarkdownChapter:
    normalized = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = _UNSAFE_FILENAME.sub("-", normalized.casefold()).strip("-")[:64] or "capitulo"
    return MarkdownChapter(f"{number:02d}-{slug}.md", title, markdown.rstrip() + "\n")


def _yaml_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _markdown_link_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _document_title(markdown: str) -> str | None:
    return next(
        (
            match.group(2).strip().rstrip("#").strip()
            for line in markdown.splitlines()
            if (match := _HEADING.match(line)) is not None
        ),
        None,
    )


def _contains_prose(markdown: str) -> bool:
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped or stripped == "---" or _HEADING.match(stripped):
            continue
        if re.match(r"^[A-Za-z_][\w-]*:\s*", stripped):
            continue
        return True
    return False


def _join(prefix: str, markdown: str) -> str:
    parts = [part for part in (prefix.strip(), markdown.strip()) if part]
    return "\n\n".join(parts) + "\n"
