"""Deterministic semantic blocks shared by quality, translation and EPUB planning."""

from __future__ import annotations

import hashlib
import html
import logging
import re
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum

from parsezen.revision import MarkdownBlock, split_markdown_blocks
from parsezen.translation_quality import is_probable_organization_name_line

_PAGE_MARKER_PATTERN = re.compile(r"<!--\s*PZDOC PDF PAGE (?P<page>\d+)\s*-->")
_HEADING_PATTERN = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_IMAGE_PATTERN = re.compile(r"^\s*!\[[^\]]*]\([^)]+\)\s*$", re.MULTILINE)
_TABLE_DIVIDER_PATTERN = re.compile(r"(?m)^\s*\|?(?:\s*:?-{3,}:?\s*\|)+")
_TOC_HEADING_PATTERN = re.compile(
    r"^(?:table of contents|contents|content|index|índice|indice|sumario|contenido)\s*$",
    re.IGNORECASE,
)
_TOC_ENTRY_PATTERN = re.compile(
    r"(?m)^.{2,100}(?:\.{2,}|\s{2,}|\s+[—–-]\s+)\s*(?:\d+|[ivxlcdm]+)\s*$",
    re.IGNORECASE,
)
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
_EVIDENCE_WORD_PATTERN = re.compile(r"(?<!\w)[A-ZÁÉÍÓÚÜÑÆŒ][A-Za-zÁÉÍÓÚÜÑáéíóúüñÆæŒœ]{6,}(?!\w)")
_EVIDENCE_PROTECTED_PATTERN = re.compile(
    r"(?ms)^\s*(```|~~~).*?^\s*\1\s*$"
    r"|`+[^`\n]*`+"
    r"|<!--[\s\S]*?-->"
    r"|!?\[[^\]]*]\((?:<[^>]+>|[^)]+)\)"
    r"|(?:https?://|mailto:)[^\s<>)\]]+"
)
_TOC_REFERENCE_LINE_PATTERN = re.compile(
    r"(?m)^(?P<prefix>[ \t]*(?:[-+*][ \t]+)?)"
    r"(?P<label>[^\r\n|]{3,140}?)"
    r"(?P<leader>[ \t]*(?:\.{2,}|[—–-])[ \t]*|[ \t]{2,})"
    r"(?P<folio>\d{1,6}|[ivxlcdm]{1,8})(?P<suffix>[ \t]*)$",
    re.IGNORECASE,
)
_TOC_TABLE_LABEL_PATTERN = re.compile(
    r'(?P<prefix><td class="toc-label toc-level-[0-2]">)'
    r"(?P<label>(?:(?!</td>).){3,500})(?P<suffix></td>)",
    re.IGNORECASE,
)
_DOCUMENT_TOC_TABLE_PATTERN = re.compile(
    r'<table\s+class=["\']document-toc["\'][^>]*>.*?</table>',
    re.IGNORECASE | re.DOTALL,
)
_FRAGMENTED_PART_TITLE_PATTERN = re.compile(
    r"(?mi)^(?:[ \t]*#{1,6}[ \t]+)?P[ \t]*(?P<roman>[IVXLCDM]+)[ \t]*$"
    r"(?:\r?\n){2}^[ \t]*(?:#{1,6}[ \t]+)?ART[ \t]*$"
    r"(?:\r?\n){2}^[ \t]*(?:#{1,6}[ \t]+)?"
    r"(?P<initials>[A-Z](?:[ \t]*[A-Z]){0,5})[ \t]*$"
    r"(?:\r?\n){2}^[ \t]*(?:#{1,6}[ \t]+)?"
    r"(?P<suffix>[A-Z][A-Z'’\u2013\u2014-]*(?:[ \t]+[A-Z][A-Z'’\u2013\u2014-]*)*)[ \t]*$"
)

LOGGER = logging.getLogger(__name__)


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
    raw_blocks = split_markdown_blocks(markdown, enforce_review_limit=False)
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
        if block.position in toc_positions or base_role is SemanticRole.TOC:
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


def reconcile_document_evidence(markdown: str) -> tuple[str, int]:
    """Repair only source spellings supported by strong evidence inside the same document."""

    reconciled, spelling_changes = _reconcile_dominant_proper_spellings(markdown)
    reconciled, reference_changes = _reconcile_toc_heading_references(reconciled)
    reconciled, fragmented_container_changes = _reconcile_fragmented_part_titles(reconciled)
    reconciled, container_changes = _reconcile_truncated_container_headings(reconciled)
    reconciled, subtitle_container_changes = _reconcile_subtitle_only_container_headings(reconciled)
    reconciled, numbered_heading_changes = _promote_index_backed_numbered_headings(reconciled)
    changes = (
        spelling_changes
        + reference_changes
        + fragmented_container_changes
        + container_changes
        + subtitle_container_changes
        + numbered_heading_changes
    )
    if changes:
        LOGGER.info("document_evidence_reconciled changes=%d", changes)
    return reconciled, changes


def _reconcile_dominant_proper_spellings(markdown: str) -> tuple[str, int]:
    protected = [
        (match.start(), match.end()) for match in _EVIDENCE_PROTECTED_PATTERN.finditer(markdown)
    ]
    matches = [
        match
        for match in _EVIDENCE_WORD_PATTERN.finditer(markdown)
        if not any(start <= match.start() < end for start, end in protected)
    ]
    counts = Counter(match.group(0) for match in matches)
    replacements: dict[str, str] = {}
    for rare, rare_count in counts.items():
        if rare_count != 1:
            continue
        candidates = [
            dominant
            for dominant, dominant_count in counts.items()
            if dominant_count >= 4
            and dominant != rare
            and _same_case_shape(dominant, rare)
            and _strict_spelling_neighbor(dominant, rare)
        ]
        if len(candidates) == 1:
            replacements[rare] = candidates[0]
    if not replacements:
        return markdown, 0
    pieces: list[str] = []
    cursor = 0
    changes = 0
    for match in matches:
        replacement = replacements.get(match.group(0))
        if replacement is None:
            continue
        pieces.append(markdown[cursor : match.start()])
        pieces.append(replacement)
        cursor = match.end()
        changes += 1
    pieces.append(markdown[cursor:])
    return "".join(pieces), changes


def _reconcile_toc_heading_references(markdown: str) -> tuple[str, int]:
    headings = tuple(
        _plain_markdown_label(match.group(2))
        for match in _HEADING_PATTERN.finditer(markdown)
        if _plain_markdown_label(match.group(2))
    )
    if not headings:
        return markdown, 0
    pieces: list[str] = []
    cursor = 0
    changes = 0
    reference_matches = sorted(
        (
            *_TOC_REFERENCE_LINE_PATTERN.finditer(markdown),
            *_TOC_TABLE_LABEL_PATTERN.finditer(markdown),
        ),
        key=lambda match: match.start("label"),
    )
    for match in reference_matches:
        raw_label = match.group("label")
        label = _plain_markdown_label(raw_label)
        if not label:
            continue
        ranked = sorted(
            (
                _toc_heading_reference_score(label, heading),
                heading,
            )
            for heading in headings
            if _reference_number_tokens(label) == _reference_number_tokens(heading)
        )
        if not ranked or ranked[-1][0] < 0.96:
            continue
        best_score, best_heading = ranked[-1]
        if len(ranked) > 1 and ranked[-2][0] >= best_score - 0.02:
            continue
        if label == best_heading or (
            _bounded_text_distance(label.casefold(), best_heading.casefold(), 2) > 2
        ):
            continue
        replacement_label = _restore_label_emphasis(raw_label, best_heading)
        pieces.append(markdown[cursor : match.start("label")])
        pieces.append(replacement_label)
        cursor = match.end("label")
        changes += 1
    if not changes:
        return markdown, 0
    pieces.append(markdown[cursor:])
    return "".join(pieces), changes


def _toc_heading_reference_score(label: str, heading: str) -> float:
    normalized_label = re.sub(r"\s+", " ", label).strip().casefold()
    normalized_heading = re.sub(r"\s+", " ", heading).strip().casefold()
    direct = SequenceMatcher(
        None,
        normalized_label,
        normalized_heading,
        autojunk=False,
    ).ratio()
    compact_label = re.sub(r"[^\w]+", "", normalized_label, flags=re.UNICODE)
    compact_heading = re.sub(r"[^\w]+", "", normalized_heading, flags=re.UNICODE)
    compact = (
        SequenceMatcher(None, compact_label, compact_heading, autojunk=False).ratio()
        if min(len(compact_label), len(compact_heading)) >= 8
        else 0.0
    )
    return max(direct, compact)


def _toc_table_labels(markdown: str, *, level: int) -> tuple[str, ...]:
    labels: list[str] = []
    for match in _TOC_TABLE_LABEL_PATTERN.finditer(markdown):
        level_match = re.search(r"toc-level-(\d)", match.group("prefix"), re.IGNORECASE)
        if level_match is None or int(level_match.group(1)) != level:
            continue
        label = _plain_markdown_label(match.group("label"))
        if label:
            labels.append(label)
    return tuple(labels)


def _reconcile_fragmented_part_titles(markdown: str) -> tuple[str, int]:
    """Rejoin decorative part-title lines split by PDF column reading order."""

    donors: dict[str, list[str]] = {}
    for label in _toc_table_labels(markdown, level=0):
        match = re.match(r"(?i)^part\s+([ivxlcdm]+)\b", label)
        if match is not None:
            donors.setdefault(match.group(1).casefold(), []).append(label)
    unique_donors = {
        numeral: labels[0] for numeral, labels in donors.items() if len(set(labels)) == 1
    }
    changes = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal changes
        numeral = match.group("roman")
        donor = unique_donors.get(numeral.casefold())
        reconstructed = _merge_fragmented_title_words(
            match.group("initials"),
            match.group("suffix"),
        )
        if donor is None and reconstructed is None:
            return match.group(0)
        changes += 1
        title = donor or f"Part {numeral.upper()}: {reconstructed}"
        return f"# {title}"

    return _FRAGMENTED_PART_TITLE_PATTERN.sub(replace, markdown), changes


def _merge_fragmented_title_words(initials: str, suffix: str) -> str | None:
    leading = tuple(re.findall(r"[A-Z]", initials.upper()))
    words = suffix.upper().split()
    if not leading or not words:
        return None
    merged: list[str] = []
    word_index = 0
    connectors = {"A", "AN", "AND", "DE", "DEL", "EL", "LA", "OF", "THE", "Y"}
    for initial in leading:
        while word_index < len(words) and words[word_index] in connectors:
            merged.append(words[word_index])
            word_index += 1
        if word_index >= len(words):
            return None
        merged.append(f"{initial}{words[word_index]}")
        word_index += 1
    merged.extend(words[word_index:])
    visible = " ".join(merged)
    if len(visible) < 4 or not all(word.isalpha() for word in merged):
        return None
    return visible.title()


def _reconcile_truncated_container_headings(markdown: str) -> tuple[str, int]:
    """Expand a bare ``PII``-style heading only from one matching printed part title."""

    donors: dict[str, list[str]] = {}
    for label in _toc_table_labels(markdown, level=0):
        match = re.match(r"(?i)^part\s+([ivxlcdm]+)\b", label)
        if match is not None:
            donors.setdefault(match.group(1).casefold(), []).append(label)
    unique_donors = {
        numeral: labels[0] for numeral, labels in donors.items() if len(set(labels)) == 1
    }
    if not unique_donors:
        return markdown, 0

    changes = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal changes
        raw_title = match.group(2)
        plain_title = _plain_markdown_label(raw_title)
        marker = re.fullmatch(r"(?i)p\s*([ivxlcdm]+)", plain_title)
        if marker is None:
            return match.group(0)
        donor = unique_donors.get(marker.group(1).casefold())
        if donor is None:
            return match.group(0)
        changes += 1
        return f"{match.group(1)} {_restore_label_emphasis(raw_title, donor)}"

    return _HEADING_PATTERN.sub(replace, markdown), changes


def _reconcile_subtitle_only_container_headings(markdown: str) -> tuple[str, int]:
    """Restore a decorative part heading when only its unique subtitle survived extraction."""

    donors: dict[str, list[str]] = {}
    for label in _toc_table_labels(markdown, level=0):
        match = re.match(
            r"(?i)^(?:part|parte|book|libro|volume|volumen|tomo)\s+"
            r"(?:\d{1,3}|[ivxlcdm]+)\s*[:\u2013\u2014-]\s*(?P<subtitle>\S.*)$",
            label,
        )
        if match is None:
            continue
        key = re.sub(r"[^\w]+", "", match.group("subtitle"), flags=re.UNICODE).casefold()
        if len(key) >= 6:
            donors.setdefault(key, []).append(label)
    unique_donors = {key: labels[0] for key, labels in donors.items() if len(set(labels)) == 1}
    if not unique_donors:
        return markdown, 0

    front_toc_end = max(
        (match.end() for match in _DOCUMENT_TOC_TABLE_PATTERN.finditer(markdown)),
        default=0,
    )
    heading_matches: dict[str, list[re.Match[str]]] = {}
    for match in _HEADING_PATTERN.finditer(markdown):
        if match.start() <= front_toc_end:
            continue
        plain = _plain_markdown_label(match.group(2))
        key = re.sub(r"[^\w]+", "", plain, flags=re.UNICODE).casefold()
        if key in unique_donors:
            heading_matches.setdefault(key, []).append(match)
    promotable = {
        matches[0].start(): unique_donors[key]
        for key, matches in heading_matches.items()
        if len(matches) == 1
    }
    if not promotable:
        return markdown, 0

    changes = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal changes
        donor = promotable.get(match.start())
        if donor is None:
            return match.group(0)
        changes += 1
        return f"{match.group(1)} {_restore_label_emphasis(match.group(2), donor)}"

    return _HEADING_PATTERN.sub(replace, markdown), changes


def _promote_index_backed_numbered_headings(markdown: str) -> tuple[str, int]:
    """Recover chapter headings misread as list items when the printed index confirms them."""

    donors: dict[str, list[str]] = {}
    for label in _toc_table_labels(markdown, level=0):
        if re.match(r"^\d{1,3}\s*[.)]\s*\S", label) is None:
            continue
        key = re.sub(r"[^\w]+", "", label, flags=re.UNICODE).casefold()
        donors.setdefault(key, []).append(label)
    unique_keys = {key for key, labels in donors.items() if len(set(labels)) == 1}
    if not unique_keys:
        return markdown, 0

    pattern = re.compile(r"(?m)^(?P<indent>[ \t]{0,3})(?P<label>\d{1,3}[.)][ \t]+[^\r\n]+?)\s*$")
    matches_by_key: dict[str, list[re.Match[str]]] = {}
    for match in pattern.finditer(markdown):
        key = re.sub(
            r"[^\w]+",
            "",
            _plain_markdown_label(match.group("label")),
            flags=re.UNICODE,
        )
        key = key.casefold()
        if key in unique_keys:
            matches_by_key.setdefault(key, []).append(match)
    promotable_starts = {
        matches[0].start() for matches in matches_by_key.values() if len(matches) == 1
    }
    if not promotable_starts:
        return markdown, 0

    changes = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal changes
        if match.start() not in promotable_starts:
            return match.group(0)
        changes += 1
        return f"{match.group('indent')}## {match.group('label')}"

    return pattern.sub(replace, markdown), changes


def _strict_spelling_neighbor(dominant: str, rare: str) -> bool:
    left = dominant.casefold()
    right = rare.casefold()
    if abs(len(left) - len(right)) > 1 or left[:3] != right[:3] or left[-2:] != right[-2:]:
        return False
    if len(left) == len(right):
        differing = [index for index, (a, b) in enumerate(zip(left, right, strict=True)) if a != b]
        if len(differing) == 1:
            return True
        if len(differing) == 2 and differing[1] == differing[0] + 1:
            first, second = differing
            return left[first] == right[second] and left[second] == right[first]
    return _bounded_text_distance(left, right, 1) == 1


def _bounded_text_distance(left: str, right: str, limit: int) -> int:
    if abs(len(left) - len(right)) > limit:
        return limit + 1
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
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def _same_case_shape(left: str, right: str) -> bool:
    return (left.isupper() and right.isupper()) or (
        left[:1].isupper() and right[:1].isupper() and left[1:].islower() and right[1:].islower()
    )


def _plain_markdown_label(value: str) -> str:
    plain = html.unescape(re.sub(r"<[^>]+>", "", value))
    plain = re.sub(r"!?\[([^]]*)]\((?:<[^>]+>|[^)]+)\)", r"\1", plain)
    plain = re.sub(r"[*_`]+", "", plain).strip()
    return re.sub(r"\s+", " ", plain)


def _restore_label_emphasis(source: str, value: str) -> str:
    if "<" in source:
        text_nodes = tuple(re.finditer(r"(?<=>)[^<>]+(?=<)", source))
        if len(text_nodes) == 1:
            node = text_nodes[0]
            return f"{source[: node.start()]}{html.escape(value)}{source[node.end() :]}"
    stripped = source.strip()
    leading = source[: len(source) - len(source.lstrip())]
    trailing = source[len(source.rstrip()) :]
    for marker in ("***", "___", "**", "__", "*", "_"):
        if stripped.startswith(marker) and stripped.endswith(marker):
            return f"{leading}{marker}{value}{marker}{trailing}"
    return f"{leading}{value}{trailing}"


def _reference_number_tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\d+|\b[ivxlcdm]+\b", value, re.IGNORECASE))


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
    if re.search(r"<table\s+class=[\"']document-toc[\"']", stripped, re.IGNORECASE):
        return SemanticRole.TOC
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
        if role is SemanticRole.HEADING and entries >= 1:
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
        if block.position in toc_positions or base_role is SemanticRole.TOC:
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
