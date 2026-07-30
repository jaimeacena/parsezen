"""Qt-free document model shared by converters and output builders."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

RESOURCE_REFERENCE_PREFIX = "__parsezen_resources__/"


@dataclass(frozen=True, slots=True)
class ConvertedResource:
    """One local binary resource that must accompany converted Markdown."""

    relative_path: PurePosixPath
    content: bytes
    media_type: str


@dataclass(frozen=True, slots=True)
class ConvertedDocument:
    """Markdown plus any local resources required to render it faithfully."""

    markdown: str
    resources: tuple[ConvertedResource, ...] = ()
