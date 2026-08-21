"""Shared types for conservative local-AI document improvement."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

MAX_LOCAL_AI_OUTPUT_CHARACTERS = 100_000


class ImprovementMode(StrEnum):
    """Supported conservative transformation modes."""

    CLEAN = "clean"
    TRANSLATE = "translate"
    CLEAN_AND_TRANSLATE = "clean_and_translate"
    REVIEW_CONTENT = "review_content"
    REVIEW_STRUCTURE = "review_structure"


ChunkProgressCallback = Callable[[int, int], None]
TranslationPreservedCallback = Callable[[int, int], None]
CheckpointLoader = Callable[[str], str | None]
CheckpointSaver = Callable[[str, str], bool]


@dataclass(frozen=True, slots=True)
class _MarkdownPart:
    text: str
    should_improve: bool
    separator_before: str = ""


@dataclass(frozen=True, slots=True)
class _TranslationContext:
    source_language: str | None
    target_language: str
    preserve_paragraphs: bool
    preserved_segments: list[str] = field(default_factory=list, compare=False)


@dataclass(frozen=True, slots=True)
class _ProtectedMarkdown:
    text: str
    values: tuple[_ProtectedValue, ...]


@dataclass(frozen=True, slots=True)
class _ProtectedValue:
    token: str
    value: str
    expected_count: int = 1
    paragraph: bool = False
