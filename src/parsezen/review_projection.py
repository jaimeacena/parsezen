"""Qt-free, reversible projections for private review syntax."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from parsezen.document_model import RESOURCE_REFERENCE_PREFIX

_INTERNAL_COMMENT_PATTERN = re.compile(r"<!--(?P<body>[\s\S]*?)-->")
_PRIVATE_LINE_PATTERN = re.compile(
    r"(?mi)^[ \t]*(?:"
    r"PZDOC(?:[ \t]*:[^\r\n]*|[ \t]+(?:PDF|EPUB)\b[^\r\n]*)|"
    r"Comentario[ \t]+interno[ \t]*:[^\r\n]*"
    r")[ \t]*(?:\r\n|\n|\r|$)"
)
_PRIVATE_RESOURCE_PATTERN = re.compile(
    re.escape(RESOURCE_REFERENCE_PREFIX) + r"[^\s)>'\"]+",
)
_PRIVATE_IMAGE_PATTERN = re.compile(
    r"!\[(?P<alt>(?:\\.|[^\]\\])*)\]"
    r"\(\s*(?:<)?" + re.escape(RESOURCE_REFERENCE_PREFIX) + r"[^\s)>\"']+(?:>)?[^)]*\)",
)
_MAX_DETAILED_ALIGNMENT_CELLS = 4_000_000


@dataclass(frozen=True, slots=True)
class _HiddenFragment:
    visible_offset: int
    value: str


@dataclass(frozen=True, slots=True)
class ReviewProjection:
    """Visible review text plus private fragments that must survive editing."""

    source_text: str
    visible_text: str
    hidden_fragments: tuple[_HiddenFragment, ...]

    def restore(self, edited_text: str) -> str:
        """Restore private fragments at their aligned positions after a user edit."""

        if _contains_private_syntax(edited_text):
            raise ValueError("La edición contiene sintaxis privada no permitida.")
        if edited_text == self.visible_text:
            return self.source_text
        positions = _map_visible_boundaries(self.visible_text, edited_text)
        restored = edited_text
        for fragment in reversed(self.hidden_fragments):
            position = positions[min(fragment.visible_offset, len(positions) - 1)]
            restored = f"{restored[:position]}{fragment.value}{restored[position:]}"
        return restored

    def private_content(self) -> str:
        """Return only private comments and resource-bearing images."""

        return _private_content_only(self.source_text)


def project_review_text(text: str) -> ReviewProjection:
    """Hide internal comments and resource destinations without touching source text."""

    fragments: list[tuple[int, int, str]] = []
    image_ranges: list[tuple[int, int]] = []
    for match in _PRIVATE_IMAGE_PATTERN.finditer(text):
        fragments.append((match.start(), match.end(), match.group(0)))
        image_ranges.append((match.start(), match.end()))
    for match in _INTERNAL_COMMENT_PATTERN.finditer(text):
        if _is_internal_comment(match.group(0)) and not any(
            start <= match.start() < end for start, end in image_ranges
        ):
            fragments.append((match.start(), match.end(), match.group(0)))
    private_ranges = [(start, end) for start, end, _value in fragments]
    for match in _PRIVATE_LINE_PATTERN.finditer(text):
        if not any(start < match.end() and match.start() < end for start, end in private_ranges):
            fragments.append((match.start(), match.end(), match.group(0)))
            private_ranges.append((match.start(), match.end()))
    for match in _PRIVATE_RESOURCE_PATTERN.finditer(text):
        if not any(start < match.end() and match.start() < end for start, end in private_ranges):
            fragments.append((match.start(), match.end(), match.group(0)))
            private_ranges.append((match.start(), match.end()))
    fragments.sort(key=lambda item: item[0])

    visible_parts: list[str] = []
    hidden: list[_HiddenFragment] = []
    source_offset = 0
    visible_offset = 0
    for start, end, value in fragments:
        visible_part = text[source_offset:start]
        visible_parts.append(visible_part)
        visible_offset += len(visible_part)
        hidden.append(_HiddenFragment(visible_offset, value))
        source_offset = end
    visible_parts.append(text[source_offset:])
    return ReviewProjection(text, "".join(visible_parts), tuple(hidden))


def restore_review_text(projection: ReviewProjection, edited_text: str) -> str:
    """Functional wrapper for callers that do not need the projection methods."""

    return projection.restore(edited_text)


def private_review_content(text: str) -> str:
    """Keep anchors and resource-bearing images while dropping OCR prose."""

    return _private_content_only(text)


def _private_content_only(text: str) -> str:
    candidates: list[tuple[int, int, str]] = []
    image_ranges: list[tuple[int, int]] = []
    comment_ranges: list[tuple[int, int]] = []
    for match in _PRIVATE_IMAGE_PATTERN.finditer(text):
        candidates.append((match.start(), match.end(), match.group(0)))
        image_ranges.append((match.start(), match.end()))
    for match in _INTERNAL_COMMENT_PATTERN.finditer(text):
        if _is_internal_comment(match.group(0)):
            candidates.append((match.start(), match.end(), match.group(0)))
            comment_ranges.append((match.start(), match.end()))
    private_ranges = [*image_ranges, *comment_ranges]
    for match in _PRIVATE_LINE_PATTERN.finditer(text):
        if not any(start < match.end() and match.start() < end for start, end in private_ranges):
            candidates.append((match.start(), match.end(), match.group(0)))
            private_ranges.append((match.start(), match.end()))
    for match in _PRIVATE_RESOURCE_PATTERN.finditer(text):
        if not any(start < match.end() and match.start() < end for start, end in private_ranges):
            candidates.append((match.start(), match.end(), match.group(0)))
    candidates.sort(key=lambda item: item[0])
    return "\n\n".join(value for _start, _end, value in candidates)


def _is_internal_comment(comment: str) -> bool:
    body = comment.casefold()
    return "pzdoc" in body or "comentario interno" in body


def _contains_private_syntax(text: str) -> bool:
    return bool(
        _PRIVATE_RESOURCE_PATTERN.search(text)
        or _PRIVATE_LINE_PATTERN.search(text)
        or any(
            _is_internal_comment(match.group(0))
            for match in _INTERNAL_COMMENT_PATTERN.finditer(text)
        )
    )


def _map_visible_boundaries(source: str, edited: str) -> list[int]:
    """Map source boundaries without diffing unchanged prefixes or suffixes."""

    mapping = [0] * (len(source) + 1)
    prefix_length = _common_prefix_length(source, edited)
    suffix_length = _common_suffix_length(source, edited, prefix_length)
    source_middle_end = len(source) - suffix_length
    edited_middle_end = len(edited) - suffix_length

    for offset in range(prefix_length + 1):
        mapping[offset] = offset

    source_middle_length = source_middle_end - prefix_length
    edited_middle_length = edited_middle_end - prefix_length
    _interpolate_boundaries(
        mapping,
        prefix_length,
        source_middle_end,
        prefix_length,
        edited_middle_end,
    )
    if (
        source_middle_length
        and edited_middle_length
        and source_middle_length * edited_middle_length <= _MAX_DETAILED_ALIGNMENT_CELLS
    ):
        _align_middle_boundaries(
            mapping,
            source,
            edited,
            prefix_length,
            source_middle_end,
            edited_middle_end,
        )

    for offset in range(suffix_length + 1):
        mapping[source_middle_end + offset] = edited_middle_end + offset
    mapping[-1] = len(edited)
    return mapping


def _align_middle_boundaries(
    mapping: list[int],
    source: str,
    edited: str,
    middle_start: int,
    source_middle_end: int,
    edited_middle_end: int,
) -> None:
    matcher = SequenceMatcher(
        None,
        source[middle_start:source_middle_end],
        edited[middle_start:edited_middle_end],
        autojunk=True,
    )
    for opcode in matcher.get_opcodes():
        (
            tag,
            relative_source_start,
            relative_source_end,
            relative_edited_start,
            relative_edited_end,
        ) = opcode
        source_start = middle_start + relative_source_start
        source_end = middle_start + relative_source_end
        edited_start = middle_start + relative_edited_start
        edited_end = middle_start + relative_edited_end
        if tag == "equal":
            for offset in range(source_start, source_end + 1):
                mapping[offset] = edited_start + offset - source_start
        elif tag == "delete":
            for offset in range(source_start, source_end + 1):
                mapping[offset] = edited_start
        else:
            _interpolate_boundaries(mapping, source_start, source_end, edited_start, edited_end)


def _interpolate_boundaries(
    mapping: list[int],
    source_start: int,
    source_end: int,
    edited_start: int,
    edited_end: int,
) -> None:
    span = max(1, source_end - source_start)
    for offset in range(source_start, source_end + 1):
        fraction = (offset - source_start) / span
        mapping[offset] = round(edited_start + fraction * (edited_end - edited_start))


def _common_prefix_length(left: str, right: str) -> int:
    maximum = min(len(left), len(right))
    for index in range(maximum):
        if left[index] != right[index]:
            return index
    return maximum


def _common_suffix_length(left: str, right: str, prefix_length: int) -> int:
    maximum = min(len(left), len(right)) - prefix_length
    for offset in range(1, maximum + 1):
        if left[-offset] != right[-offset]:
            return offset - 1
    return maximum
