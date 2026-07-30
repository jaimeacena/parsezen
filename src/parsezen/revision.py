"""Reviewable document revisions built from stable Markdown blocks."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum

from parsezen.errors import ImprovementError

_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_MAX_REVIEW_MARKDOWN_CHARACTERS = 2_000_000


class RevisionKind(StrEnum):
    """User-visible categories for proposed changes."""

    CONTENT = "content"
    STRUCTURE = "structure"


class RevisionDecision(StrEnum):
    """One explicit decision over a proposed change."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"


class RevisionRisk(StrEnum):
    """Whether a proposal is conservative enough to recommend automatically."""

    LOW = "low"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class MarkdownBlock:
    """One stable review unit separated at a safe Markdown paragraph boundary."""

    identifier: str
    markdown: str
    position: int


@dataclass(frozen=True, slots=True)
class RevisionChange:
    """One independently reviewable replacement between Markdown block ranges."""

    identifier: str
    kind: RevisionKind
    original_start: int
    original_end: int
    original_markdown: str
    proposed_markdown: str
    summary: str
    risk: RevisionRisk = RevisionRisk.LOW
    risk_reason: str | None = None

    @property
    def recommended_decision(self) -> RevisionDecision:
        return (
            RevisionDecision.REJECTED
            if self.risk is RevisionRisk.HIGH
            else RevisionDecision.ACCEPTED
        )


@dataclass(frozen=True, slots=True)
class RevisionDraft:
    """Original and proposed Markdown plus independently applicable changes."""

    original_markdown: str
    proposed_markdown: str
    changes: tuple[RevisionChange, ...]
    kinds: frozenset[RevisionKind]

    def render(
        self,
        decisions: dict[str, RevisionDecision] | None = None,
    ) -> str:
        """Render accepted proposals while retaining rejected original ranges."""
        if not self.changes:
            return self.proposed_markdown
        resolved = decisions or {}
        original_blocks = split_markdown_blocks(self.original_markdown)
        output: list[str] = []
        cursor = 0
        for change in self.changes:
            output.extend(
                block.markdown for block in original_blocks[cursor : change.original_start]
            )
            decision = resolved.get(change.identifier, change.recommended_decision)
            if decision is RevisionDecision.ACCEPTED:
                output.append(change.proposed_markdown)
            else:
                output.append(change.original_markdown)
            cursor = change.original_end
        output.extend(block.markdown for block in original_blocks[cursor:])
        return "".join(output)


def split_markdown_blocks(markdown: str) -> tuple[MarkdownBlock, ...]:
    """Split Markdown at blank lines outside fenced code blocks."""
    if not isinstance(markdown, str) or "\0" in markdown:
        raise ImprovementError("El documento para revisar no es válido.")
    if len(markdown) > _MAX_REVIEW_MARKDOWN_CHARACTERS:
        raise ImprovementError("El documento es demasiado grande para revisarlo localmente.")
    if not markdown:
        return ()

    pieces: list[str] = []
    current: list[str] = []
    fence: str | None = None
    for line in markdown.splitlines(keepends=True):
        stripped = line.lstrip()
        marker = (
            "```" if stripped.startswith("```") else "~~~" if stripped.startswith("~~~") else None
        )
        if marker is not None:
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
        current.append(line)
        if fence is None and not line.strip():
            pieces.append("".join(current))
            current = []
    if current:
        pieces.append("".join(current))

    blocks: list[MarkdownBlock] = []
    occurrences: Counter[str] = Counter()
    for position, piece in enumerate(pieces):
        content_digest = hashlib.sha256(piece.encode("utf-8", errors="strict")).hexdigest()
        occurrence = occurrences[content_digest]
        occurrences[content_digest] += 1
        digest = hashlib.sha256(
            f"markdown-block-v2\0{content_digest}\0{occurrence}".encode(
                "utf-8",
                errors="strict",
            )
        ).hexdigest()[:20]
        blocks.append(MarkdownBlock(digest, piece, position))
    return tuple(blocks)


def build_revision_draft(
    original_markdown: str,
    proposed_markdown: str,
    *,
    kinds: frozenset[RevisionKind],
) -> RevisionDraft:
    """Build compact independently reviewable replacements from two documents."""
    original_blocks = split_markdown_blocks(original_markdown)
    proposed_blocks = split_markdown_blocks(proposed_markdown)
    matcher = SequenceMatcher(
        a=[block.markdown for block in original_blocks],
        b=[block.markdown for block in proposed_blocks],
        autojunk=False,
    )
    changes: list[RevisionChange] = []
    default_kind = (
        RevisionKind.STRUCTURE
        if kinds == frozenset({RevisionKind.STRUCTURE})
        else RevisionKind.CONTENT
    )
    for tag, original_start, original_end, proposed_start, proposed_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        original = "".join(block.markdown for block in original_blocks[original_start:original_end])
        proposed = "".join(block.markdown for block in proposed_blocks[proposed_start:proposed_end])
        kind = _change_kind(original, proposed, default_kind, kinds)
        risk, risk_reason = _revision_risk(original, proposed, kind)
        original_identity = ",".join(
            block.identifier for block in original_blocks[original_start:original_end]
        )
        proposed_identity = ",".join(
            block.identifier for block in proposed_blocks[proposed_start:proposed_end]
        )
        identifier = hashlib.sha256(
            (
                f"revision-change-v2\0{kind.value}\0"
                f"{original_identity}\0{proposed_identity}\0{original}\0{proposed}"
            ).encode("utf-8", errors="strict")
        ).hexdigest()[:24]
        changes.append(
            RevisionChange(
                identifier=identifier,
                kind=kind,
                original_start=original_start,
                original_end=original_end,
                original_markdown=original,
                proposed_markdown=proposed,
                summary=_change_summary(tag, original, proposed, kind),
                risk=risk,
                risk_reason=risk_reason,
            )
        )
    return RevisionDraft(
        original_markdown=original_markdown,
        proposed_markdown=proposed_markdown,
        changes=tuple(changes),
        kinds=kinds,
    )


def markdown_headings(markdown: str) -> tuple[tuple[int, str], ...]:
    """Return the editable heading outline in document order."""
    headings: list[tuple[int, str]] = []
    fence: str | None = None
    for line in markdown.splitlines():
        stripped = line.lstrip()
        marker = (
            "```" if stripped.startswith("```") else "~~~" if stripped.startswith("~~~") else None
        )
        if marker is not None:
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is not None:
            continue
        match = _HEADING_PATTERN.fullmatch(stripped)
        if match is not None:
            headings.append((len(match.group(1)), match.group(2).strip()))
    return tuple(headings)


def set_heading_level(markdown: str, line_number: int, level: int | None) -> str:
    """Promote, demote or clear the heading marker on one one-based line."""
    if level is not None and not 1 <= level <= 6:
        raise ValueError("level must be between 1 and 6")
    lines = markdown.splitlines(keepends=True)
    if not 1 <= line_number <= len(lines):
        return markdown
    line = lines[line_number - 1]
    ending = "\n" if line.endswith("\n") else ""
    visible = line[:-1] if ending else line
    indent = visible[: len(visible) - len(visible.lstrip())]
    content = re.sub(r"^#{1,6}\s+", "", visible.lstrip()).strip()
    if not content:
        return markdown
    lines[line_number - 1] = (
        f"{indent}{'#' * level} {content}{ending}"
        if level is not None
        else f"{indent}{content}{ending}"
    )
    return "".join(lines)


def _change_kind(
    original: str,
    proposed: str,
    default: RevisionKind,
    kinds: frozenset[RevisionKind],
) -> RevisionKind:
    if RevisionKind.STRUCTURE not in kinds:
        return default
    if _without_heading_markers(original) == _without_heading_markers(proposed):
        return RevisionKind.STRUCTURE
    return default


def _without_heading_markers(markdown: str) -> str:
    return re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", markdown).strip()


def _change_summary(
    tag: str,
    original: str,
    proposed: str,
    kind: RevisionKind,
) -> str:
    if kind is RevisionKind.STRUCTURE:
        return "Cambio de títulos o estructura"
    if tag == "delete" or (original.strip() and not proposed.strip()):
        return "Contenido propuesto para eliminar"
    if tag == "insert" or (proposed.strip() and not original.strip()):
        return "Contenido propuesto para añadir"
    return "Corrección de contenido"


_REVISION_NUMBER_PATTERN = re.compile(r"(?<!\w)\d+(?:[.,:/-]\d+)*(?!\w)")
_REVISION_WORD_PATTERN = re.compile(r"[^\W_]+(?:[’'-][^\W_]+)*", re.UNICODE)


def _revision_risk(
    original: str,
    proposed: str,
    kind: RevisionKind,
) -> tuple[RevisionRisk, str | None]:
    if kind is RevisionKind.STRUCTURE:
        original_levels = tuple(level for level, _title in markdown_headings(original))
        proposed_levels = tuple(level for level, _title in markdown_headings(proposed))
        if len(original_levels) == len(proposed_levels) and any(
            abs(original_level - proposed_level) > 1
            for original_level, proposed_level in zip(
                original_levels,
                proposed_levels,
                strict=True,
            )
        ):
            return (
                RevisionRisk.HIGH,
                "La propuesta cambia la jerarquía de títulos de forma demasiado brusca.",
            )
        if not original_levels and any(level > 3 for level in proposed_levels):
            return (
                RevisionRisk.HIGH,
                "La propuesta crea un título profundo sin una jerarquía previa demostrable.",
            )
        return RevisionRisk.LOW, None
    original_text = original.strip()
    proposed_text = proposed.strip()
    if not original_text or not proposed_text:
        return (
            RevisionRisk.HIGH,
            "La propuesta añade o elimina un bloque completo; "
            "se conserva el original por seguridad.",
        )
    if Counter(_REVISION_NUMBER_PATTERN.findall(original)) != Counter(
        _REVISION_NUMBER_PATTERN.findall(proposed)
    ):
        return RevisionRisk.HIGH, "La propuesta cambia números o fechas."
    if _protected_revision_terms(original) != _protected_revision_terms(proposed):
        return RevisionRisk.HIGH, "La propuesta cambia nombres propios o siglas."
    if _heading_only(original) and _heading_only(proposed):
        return RevisionRisk.LOW, None
    if len(split_markdown_blocks(original)) != len(split_markdown_blocks(proposed)):
        return RevisionRisk.HIGH, "La propuesta cambia la cantidad de párrafos."

    original_words = _revision_words(original)
    proposed_words = _revision_words(proposed)
    if len(original_words) >= 8:
        word_ratio = len(proposed_words) / len(original_words)
        if not 0.8 <= word_ratio <= 1.25:
            return (
                RevisionRisk.HIGH,
                "La propuesta añade o elimina una parte sustancial del texto.",
            )
        if len(original_words) >= 10:
            similarity = SequenceMatcher(
                a=original_words,
                b=proposed_words,
                autojunk=False,
            ).ratio()
            if similarity < 0.55:
                return RevisionRisk.HIGH, "La propuesta reescribe gran parte del contenido."
    return RevisionRisk.LOW, None


def _revision_words(markdown: str) -> tuple[str, ...]:
    return tuple(match.group(0).casefold() for match in _REVISION_WORD_PATTERN.finditer(markdown))


def _protected_revision_terms(markdown: str) -> Counter[str]:
    protected: Counter[str] = Counter()
    for match in _REVISION_WORD_PATTERN.finditer(markdown):
        token = match.group(0)
        if len(token) < 2:
            continue
        preceding = markdown[: match.start()].rstrip(" \t")
        starts_sentence = not preceding or preceding[-1] in ".!?\n:#"
        if token.isupper() or (token[0].isupper() and not starts_sentence):
            protected[token.casefold()] += 1
    return protected


def _heading_only(markdown: str) -> bool:
    lines = tuple(line.strip() for line in markdown.splitlines() if line.strip())
    return bool(lines) and all(_HEADING_PATTERN.fullmatch(line) is not None for line in lines)
