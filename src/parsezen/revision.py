"""Reviewable document revisions built from stable Markdown blocks."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum

from parsezen.document_model import RESOURCE_REFERENCE_PREFIX
from parsezen.errors import ImprovementError
from parsezen.translation_quality import (
    TranslationQualityError,
    detect_language_code,
    resolve_language_code,
    validate_translation_quality,
)

_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_MAX_REVIEW_MARKDOWN_CHARACTERS = 2_000_000
_INTERNAL_COMMENT_PATTERN = re.compile(r"<!--(?P<body>[\s\S]*?)-->")
_PRIVATE_RESOURCE_PATTERN = re.compile(
    re.escape(RESOURCE_REFERENCE_PREFIX) + r"[^\s)>'\"]+",
)
_PRIVATE_IMAGE_PATTERN = re.compile(
    r"!\[(?P<alt>(?:\\.|[^\]\\])*)\]"
    r"\(\s*(?:<)?" + re.escape(RESOURCE_REFERENCE_PREFIX) + r"[^\s)>\"']+(?:>)?[^)]*\)",
)
_PZDOC_TEXT_PATTERN = re.compile(r"PZDOC", re.IGNORECASE)
_PZDOC_TOKEN_PATTERN = re.compile(r"\bPZDOC[^\s<>()\]]*", re.IGNORECASE)
_INTERNAL_COMMENT_TEXT_PATTERN = re.compile(r"comentario\s+interno", re.IGNORECASE)


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
    proposal_selectable: bool = True

    @property
    def recommended_decision(self) -> RevisionDecision:
        return (
            RevisionDecision.REJECTED
            if self.risk is RevisionRisk.HIGH or not self.proposal_selectable
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
            if not change.proposal_selectable:
                decision = RevisionDecision.REJECTED
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
    translation_source_markdown: str | None = None,
    translation_target_language: str | None = None,
    protected_translation_terms: tuple[str, ...] = (),
) -> RevisionDraft:
    """Build compact independently reviewable replacements from two documents."""
    original_blocks = split_markdown_blocks(original_markdown)
    proposed_blocks = split_markdown_blocks(proposed_markdown)
    translation_source_blocks = (
        split_markdown_blocks(translation_source_markdown)
        if translation_source_markdown is not None
        else ()
    )
    translation_alignment_available = bool(
        translation_source_blocks
        and len(translation_source_blocks) == len(original_blocks)
        and resolve_language_code(translation_target_language) is not None
    )
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
        validated_translation_correction = False
        if translation_alignment_available and kind is RevisionKind.CONTENT:
            translation_source = "".join(
                block.markdown for block in translation_source_blocks[original_start:original_end]
            )
            validated_translation_correction = _validated_translation_correction(
                translation_source,
                original,
                proposed,
                target_language=translation_target_language,
                protected_terms=protected_translation_terms,
            )
        risk, risk_reason = _revision_risk(
            original,
            proposed,
            kind,
            validated_translation_correction=validated_translation_correction,
        )
        proposal_selectable = True
        if kind is RevisionKind.CONTENT:
            try:
                validate_review_content_candidate(original, proposed)
            except ImprovementError as exc:
                proposal_selectable = False
                risk = RevisionRisk.HIGH
                risk_reason = risk_reason or f"La propuesta no es seleccionable: {exc}"
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
                proposal_selectable=proposal_selectable,
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
    *,
    validated_translation_correction: bool = False,
) -> tuple[RevisionRisk, str | None]:
    if Counter(_REVISION_NUMBER_PATTERN.findall(original)) != Counter(
        _REVISION_NUMBER_PATTERN.findall(proposed)
    ):
        return RevisionRisk.HIGH, "La propuesta cambia números o fechas."
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
    if not validated_translation_correction and _protected_revision_terms(
        original
    ) != _protected_revision_terms(proposed):
        return RevisionRisk.HIGH, "La propuesta cambia nombres propios o siglas."
    if not validated_translation_correction and _heading_only(original) and _heading_only(proposed):
        return (
            RevisionRisk.HIGH,
            "La propuesta cambia palabras de un título; se conserva el original por seguridad.",
        )
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


def validate_revision_selection(draft: RevisionDraft, reviewed_markdown: str) -> None:
    """Reject a mixed review that invents or silently loses numeric values.

    A user may choose either the original or proposed occurrence count for a
    value. Combining independent changes must never produce a count outside
    that closed interval, which catches shifted or ambiguously aligned blocks
    without preventing an explicitly proposed numeric correction.
    """

    original = Counter(_REVISION_NUMBER_PATTERN.findall(draft.original_markdown))
    proposed = Counter(_REVISION_NUMBER_PATTERN.findall(draft.proposed_markdown))
    reviewed = Counter(_REVISION_NUMBER_PATTERN.findall(reviewed_markdown))
    for value in original.keys() | proposed.keys() | reviewed.keys():
        lower = min(original[value], proposed[value])
        upper = max(original[value], proposed[value])
        if not lower <= reviewed[value] <= upper:
            raise ImprovementError(
                "La selección de revisión combina cambios numéricos de forma insegura."
            )


def _validated_translation_correction(
    source: str,
    original: str,
    proposed: str,
    *,
    target_language: str | None,
    protected_terms: tuple[str, ...],
) -> bool:
    target_code = resolve_language_code(target_language)
    if target_code is None or not source.strip() or not proposed.strip():
        return False
    source_content = _translation_content_view(source)
    proposed_content = _translation_content_view(proposed)
    try:
        validate_translation_quality(
            source_content,
            proposed_content,
            source_language=detect_language_code(source_content),
            target_language=target_code,
            preserve_paragraphs=True,
        )
    except TranslationQualityError:
        return False
    if not _protected_translation_terms_are_conserved(original, proposed, protected_terms):
        return False
    return _shared_source_names_are_conserved(source, original, proposed)


def _translation_content_view(markdown: str) -> str:
    """Ignore independently validated heading markers in a mixed review proposal."""

    return re.sub(r"(?m)^(\s{0,3})#{1,6}[ \t]+", r"\1", markdown)


def _protected_translation_terms_are_conserved(
    original: str,
    proposed: str,
    protected_terms: tuple[str, ...],
) -> bool:
    for term in protected_terms:
        normalized = term.strip()
        if len(normalized) < 2:
            continue
        pattern = re.compile(rf"(?<!\w){re.escape(normalized)}(?!\w)", re.IGNORECASE)
        if len(pattern.findall(original)) != len(pattern.findall(proposed)):
            return False
    return True


def _shared_source_names_are_conserved(source: str, original: str, proposed: str) -> bool:
    phrases = {
        match.group(0)
        for match in re.finditer(
            r"(?<!\w)(?:[A-ZÁÉÍÓÚÜÑ][^\W\d_]+(?:\s+|$)){2,4}",
            source,
            re.UNICODE,
        )
    }
    acronyms = {match.group(0) for match in re.finditer(r"(?<!\w)[A-ZÁÉÍÓÚÜÑ]{2,}(?!\w)", source)}
    for value in phrases | acronyms:
        normalized = value.strip()
        if not normalized:
            continue
        pattern = re.compile(rf"(?<!\w){re.escape(normalized)}(?!\w)", re.IGNORECASE)
        original_count = len(pattern.findall(original))
        if original_count and len(pattern.findall(proposed)) != original_count:
            return False
    return True


def _revision_words(markdown: str) -> tuple[str, ...]:
    return tuple(match.group(0).casefold() for match in _REVISION_WORD_PATTERN.finditer(markdown))


def validate_review_content_candidate(original: str, proposed: str) -> None:
    """Fail closed when a content-review proposal is not a bounded edit.

    Content review may correct an aligned typo, but it is not allowed to act as
    a summarizer or generator. This guard is deliberately independent of the
    model response validator so old drafts and materialized caches receive the
    same treatment.
    """

    if not original.strip() or not proposed.strip():
        raise ImprovementError("La propuesta añade o elimina un bloque completo.")

    original_blocks = split_markdown_blocks(original)
    proposed_blocks = split_markdown_blocks(proposed)
    if len(original_blocks) != len(proposed_blocks):
        raise ImprovementError("La propuesta cambia el número de bloques o párrafos.")

    _validate_private_content(original, proposed)
    for original_block, proposed_block in zip(original_blocks, proposed_blocks, strict=True):
        original_words = _revision_words(_without_private_content(original_block.markdown))
        proposed_words = _revision_words(_without_private_content(proposed_block.markdown))
        if not original_words:
            if proposed_words:
                raise ImprovementError("La propuesta inventa contenido en un bloque vacío.")
            continue

        if len(proposed_words) > len(original_words) + max(4, round(len(original_words) * 0.35)):
            raise ImprovementError("La propuesta expande sustancialmente el contenido.")
        if len(original_words) >= 5 and len(proposed_words) < round(len(original_words) * 0.60):
            raise ImprovementError("La propuesta omite una parte sustancial del contenido.")
        if len(original_words) >= 5:
            similarity = SequenceMatcher(
                a=original_words,
                b=proposed_words,
                autojunk=False,
            ).ratio()
            if similarity < 0.60:
                raise ImprovementError("La propuesta reescribe demasiado contenido.")


def _without_private_content(markdown: str) -> str:
    without_comments = _INTERNAL_COMMENT_PATTERN.sub("", markdown)
    return _PRIVATE_RESOURCE_PATTERN.sub("", without_comments)


def _validate_private_content(original: str, proposed: str) -> None:
    original_comments = Counter(
        match.group(0)
        for match in _INTERNAL_COMMENT_PATTERN.finditer(original)
        if _is_private_comment(match.group(0))
    )
    proposed_comments = Counter(
        match.group(0)
        for match in _INTERNAL_COMMENT_PATTERN.finditer(proposed)
        if _is_private_comment(match.group(0))
    )
    if original_comments != proposed_comments:
        raise ImprovementError("La propuesta añadió o modificó comentarios internos.")

    original_resources = Counter(_PRIVATE_RESOURCE_PATTERN.findall(original))
    proposed_resources = Counter(_PRIVATE_RESOURCE_PATTERN.findall(proposed))
    if original_resources != proposed_resources:
        raise ImprovementError("La propuesta añadió o modificó recursos privados.")

    original_images = Counter(match.group(0) for match in _PRIVATE_IMAGE_PATTERN.finditer(original))
    proposed_images = Counter(match.group(0) for match in _PRIVATE_IMAGE_PATTERN.finditer(proposed))
    if original_images != proposed_images:
        raise ImprovementError("La propuesta modificó la sintaxis de una imagen privada.")

    original_visible = _INTERNAL_COMMENT_PATTERN.sub("", original)
    proposed_visible = _INTERNAL_COMMENT_PATTERN.sub("", proposed)
    if _visible_private_marker_lines(original_visible) != _visible_private_marker_lines(
        proposed_visible
    ):
        raise ImprovementError("La propuesta añadió o filtró una marca PZDOC.")
    if Counter(_INTERNAL_COMMENT_TEXT_PATTERN.findall(original_visible)) != Counter(
        _INTERNAL_COMMENT_TEXT_PATTERN.findall(proposed_visible)
    ):
        raise ImprovementError("La propuesta añadió o modificó un comentario interno.")


def _visible_private_marker_lines(markdown: str) -> Counter[str]:
    return Counter(
        line.strip()
        for line in markdown.splitlines()
        if _PZDOC_TOKEN_PATTERN.search(line) or _INTERNAL_COMMENT_TEXT_PATTERN.search(line)
    )


def _is_private_comment(comment: str) -> bool:
    return bool(
        _PZDOC_TEXT_PATTERN.search(comment) or _INTERNAL_COMMENT_TEXT_PATTERN.search(comment)
    )


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
