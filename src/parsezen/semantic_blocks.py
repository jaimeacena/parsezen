"""Deterministic semantic blocks shared by quality, translation and EPUB planning."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum

from parsezen.revision import MarkdownBlock, split_markdown_blocks
from parsezen.translation_quality import is_probable_organization_name_line

_PAGE_MARKER_PATTERN = re.compile(r"<!--\s*PZDOC PDF PAGE (?P<page>\d+)\s*-->")
_HEADING_PATTERN = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_IMAGE_PATTERN = re.compile(r"^\s*!\[[^\]]*]\([^)]+\)\s*$", re.MULTILINE)
_TABLE_DIVIDER_PATTERN = re.compile(r"(?m)^\s*\|?(?:\s*:?-{3,}:?\s*\|)+")
_TOC_HEADING_PATTERN = re.compile(
    r"^(?:table of contents|contents|índice|indice|sumario)\s*$",
    re.IGNORECASE,
)
_TOC_ENTRY_PATTERN = re.compile(r"(?m)^.{2,100}(?:\.{2,}|\s{2,})\s*\d+\s*$")
_CHAPTER_HEADING_PATTERN = re.compile(
    r"^(?:(?:chapter|cap[ií]tulo|part|parte|book|libro)\s+"
    r"(?:[0-9ivxlcdm]+|one|two|three|four|five|six|seven|eight|nine|ten)"
    r"(?:\b|[.:—-])|introduction|introducci[oó]n)\b",
    re.IGNORECASE,
)
_FRONT_MATTER_PATTERN = re.compile(
    r"\b(?:copyright|all rights reserved|isbn|publisher|published|edition|"
    r"dedication|acknowledg(?:e)?ments|about the author|author|translator|"
    r"derechos reservados|editorial|edici[oó]n|dedicatoria|agradecimientos|"
    r"sobre el autor|traductor)\b",
    re.IGNORECASE,
)
_NOTE_PATTERN = re.compile(
    r"^\s*(?:>\s*)?(?:note|nota|warning|aviso|footnote|nota al pie)\b",
    re.IGNORECASE,
)
_TERM_PATTERN = re.compile(
    r"(?<![\w'-])"
    r"([A-ZÁÉÍÓÚÜÑ][\wÁÉÍÓÚÜÑáéíóúüñ’'-]{1,}"
    r"(?:[^\S\r\n]+(?:(?:de|del|da|di|la|las|los|van|von|y|&)[^\S\r\n]+)?"
    r"[A-ZÁÉÍÓÚÜÑ][\wÁÉÍÓÚÜÑáéíóúüñ’'-]{1,}){1,4})"
)
_TERM_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "appendix",
        "book",
        "chapter",
        "contents",
        "copyright",
        "figure",
        "introduction",
        "part",
        "table",
        "the",
        "this",
        "volume",
        "capítulo",
        "figura",
        "índice",
        "introducción",
        "libro",
        "parte",
        "tabla",
        "volumen",
    }
)
_TERM_BYLINE_CONTEXT_WORDS = frozenset({"author", "autor", "by", "por"})
_TERM_LEADING_CONTEXT_WORDS = frozenset(
    {
        "according",
        "finally",
        "however",
        "later",
        "meanwhile",
        "then",
        *_TERM_BYLINE_CONTEXT_WORDS,
    }
)
_BYLINE_PREFIX_PATTERN = re.compile(
    r"(?i)(?:^|\n)[ \t]*(?:written[ \t]+by|by|author|autor|por)[ \t]*[:\-]?[ \t]*$"
)
_MAX_FRONT_MATTER_BLOCKS = 48
_MAX_FRONT_MATTER_PAGE = 12
_MAX_TERM_MEMORY_ENTRIES = 50


class SemanticRole(StrEnum):
    """A conservative role inferred without changing document content."""

    PROVENANCE = "provenance"
    FRONT_MATTER = "front_matter"
    TOC = "toc"
    HEADING = "heading"
    BODY = "body"
    IMAGE = "image"
    TABLE = "table"
    NOTE = "note"
    CODE = "code"


@dataclass(frozen=True, slots=True)
class SemanticBlock:
    """One stable Markdown block plus its inferred role and source page."""

    identifier: str
    markdown: str
    position: int
    role: SemanticRole
    page_number: int | None
    confidence: float


@dataclass(frozen=True, slots=True)
class DocumentTerm:
    """A repeated proper term safe to keep consistent during translation."""

    text: str
    occurrences: int


@dataclass(frozen=True, slots=True)
class SemanticDocument:
    """Semantic view of a Markdown document with no duplicated payload."""

    blocks: tuple[SemanticBlock, ...]
    terms: tuple[DocumentTerm, ...]

    @property
    def front_matter_blocks(self) -> int:
        return sum(block.role is SemanticRole.FRONT_MATTER for block in self.blocks)

    @property
    def toc_blocks(self) -> int:
        return sum(block.role is SemanticRole.TOC for block in self.blocks)


def analyze_markdown(markdown: str) -> SemanticDocument:
    """Classify stable Markdown blocks using bounded, deterministic heuristics."""
    raw_blocks = split_markdown_blocks(markdown)
    page_numbers = _page_numbers(raw_blocks)
    base_roles = tuple(_base_role(block.markdown) for block in raw_blocks)
    toc_positions = _toc_positions(raw_blocks, base_roles)
    front_matter_end = _front_matter_end(raw_blocks, page_numbers, toc_positions)

    blocks: list[SemanticBlock] = []
    for block, page_number, base_role in zip(
        raw_blocks,
        page_numbers,
        base_roles,
        strict=True,
    ):
        if block.position in toc_positions:
            role = SemanticRole.TOC
            confidence = 0.96
        elif block.position < front_matter_end and base_role not in {
            SemanticRole.PROVENANCE,
            SemanticRole.CODE,
            SemanticRole.TABLE,
        }:
            role = SemanticRole.FRONT_MATTER
            confidence = 0.82
        else:
            role = base_role
            confidence = 0.92 if role is not SemanticRole.BODY else 0.76
        blocks.append(
            SemanticBlock(
                identifier=block.identifier,
                markdown=block.markdown,
                position=block.position,
                role=role,
                page_number=page_number,
                confidence=confidence,
            )
        )
    return SemanticDocument(tuple(blocks), discover_document_terms(markdown))


def discover_document_terms(markdown: str) -> tuple[DocumentTerm, ...]:
    """Return only repeated, multiword proper terms; never infer translations."""
    protected = re.sub(r"(?ms)^\s*(```|~~~).*?^\s*\1\s*$", " ", markdown)
    protected = re.sub(r"<!--[\s\S]*?-->|https?://\S+|!\[[^\]]*]\([^)]+\)", " ", protected)
    spellings: dict[str, Counter[str]] = {}
    evidence: set[str] = set()
    for match in _TERM_PATTERN.finditer(protected):
        candidate = re.sub(r"\s+", " ", match.group(1)).strip(" .,:;!?()[]{}")
        words = tuple(re.findall(r"[^\W\d_]+", candidate, re.UNICODE))
        leading_context: str | None = None
        if words and words[0].casefold() in _TERM_LEADING_CONTEXT_WORDS:
            leading_context = words[0].casefold()
            candidate = candidate[len(words[0]) :].strip()
            words = tuple(re.findall(r"[^\W\d_]+", candidate, re.UNICODE))
        if (
            len(words) < 2
            or len(candidate) > 80
            or any(word.casefold() in _TERM_STOP_WORDS for word in words)
        ):
            continue
        key = candidate.casefold()
        spellings.setdefault(key, Counter())[candidate] += 1
        prefix = protected[max(0, match.start() - 32) : match.start()]
        suffix = protected[match.end() : min(len(protected), match.end() + 8)]
        if (
            leading_context in _TERM_BYLINE_CONTEXT_WORDS
            or _BYLINE_PREFIX_PATTERN.search(prefix)
            or re.search(r"©\s*$", prefix)
            or re.match(r"(?:['’]s)\b", suffix, re.IGNORECASE)
            or is_probable_organization_name_line(candidate)
        ):
            evidence.add(key)

    terms = [
        DocumentTerm(counter.most_common(1)[0][0], sum(counter.values()))
        for key, counter in spellings.items()
        if sum(counter.values()) >= 2 and key in evidence
    ]
    terms.sort(key=lambda item: (-item.occurrences, -len(item.text), item.text.casefold()))
    return tuple(terms[:_MAX_TERM_MEMORY_ENTRIES])


def terminology_fingerprint(terms: tuple[DocumentTerm, ...]) -> str:
    """Identify inferred terminology without exposing it in logs or cache names."""
    digest = hashlib.sha256()
    for term in terms:
        digest.update(term.text.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(term.occurrences).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _page_numbers(blocks: tuple[MarkdownBlock, ...]) -> tuple[int | None, ...]:
    current_page: int | None = None
    pages: list[int | None] = []
    for block in blocks:
        marker = _PAGE_MARKER_PATTERN.search(block.markdown)
        if marker is not None:
            current_page = int(marker.group("page"))
        pages.append(current_page)
    return tuple(pages)


def _base_role(markdown: str) -> SemanticRole:
    stripped = markdown.strip()
    if _PAGE_MARKER_PATTERN.fullmatch(stripped):
        return SemanticRole.PROVENANCE
    if stripped.startswith(("```", "~~~")):
        return SemanticRole.CODE
    if _IMAGE_PATTERN.fullmatch(stripped):
        return SemanticRole.IMAGE
    if _TABLE_DIVIDER_PATTERN.search(stripped):
        return SemanticRole.TABLE
    if _NOTE_PATTERN.match(stripped):
        return SemanticRole.NOTE
    if _HEADING_PATTERN.match(stripped):
        return SemanticRole.HEADING
    return SemanticRole.BODY


def _toc_positions(
    blocks: tuple[MarkdownBlock, ...],
    roles: tuple[SemanticRole, ...],
) -> frozenset[int]:
    selected: set[int] = set()
    active = False
    entries = 0
    for block, role in zip(blocks, roles, strict=True):
        visible = _visible_text(block.markdown)
        heading = _heading_text(block.markdown)
        if heading is not None and _TOC_HEADING_PATTERN.fullmatch(heading):
            active = True
            entries = 0
            selected.add(block.position)
            continue
        if not active:
            continue
        looks_like_entry = bool(_TOC_ENTRY_PATTERN.search(visible))
        if looks_like_entry:
            selected.add(block.position)
            entries += 1
            continue
        if role is SemanticRole.PROVENANCE or not visible:
            selected.add(block.position)
            continue
        if role is SemanticRole.HEADING and entries >= 2:
            active = False
            continue
        if len(visible) <= 160 and re.search(r"\b\d+\s*$", visible):
            selected.add(block.position)
            entries += 1
            continue
        if entries >= 2:
            active = False
        elif len(selected) > 8:
            active = False
    return frozenset(selected)


def _front_matter_end(
    blocks: tuple[MarkdownBlock, ...],
    page_numbers: tuple[int | None, ...],
    toc_positions: frozenset[int],
) -> int:
    if not blocks:
        return 0
    evidence = 0
    saw_toc = False
    for block, page_number in zip(blocks, page_numbers, strict=True):
        if block.position >= _MAX_FRONT_MATTER_BLOCKS:
            return block.position
        if page_number is not None and page_number > _MAX_FRONT_MATTER_PAGE:
            return block.position
        heading = _heading_text(block.markdown)
        visible = _visible_text(block.markdown)
        base_role = _base_role(block.markdown)
        if saw_toc and heading is not None and block.position not in toc_positions:
            return block.position
        if (
            heading is not None
            and _CHAPTER_HEADING_PATTERN.match(heading)
            and (block.position == 0 or evidence > 0)
        ):
            return block.position
        if block.position in toc_positions:
            evidence += 2
            saw_toc = True
        elif _FRONT_MATTER_PATTERN.search(visible):
            evidence += 2
        elif base_role is SemanticRole.IMAGE and block.position <= 4:
            evidence += 1
        elif heading is not None and block.position <= 4:
            evidence += 1
        elif 0 < len(visible) <= 120 and block.position <= 6:
            evidence += 1
        elif len(visible) > 180 and base_role is SemanticRole.BODY:
            return block.position if evidence >= 2 else 0
        if evidence == 0 and block.position >= 2:
            return 0
    return min(len(blocks), _MAX_FRONT_MATTER_BLOCKS) if evidence >= 2 else 0


def _heading_text(markdown: str) -> str | None:
    match = _HEADING_PATTERN.match(markdown.strip())
    return re.sub(r"[*_`~\[\]]", "", match.group(2)).strip() if match else None


def _visible_text(markdown: str) -> str:
    text = _PAGE_MARKER_PATTERN.sub("", markdown)
    text = re.sub(r"!\[([^\]]*)]\([^)]+\)", r"\1", text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
    return re.sub(r"\s+", " ", text).strip()
