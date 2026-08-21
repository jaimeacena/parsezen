"""Qt-free document model shared by converters and output builders."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

RESOURCE_REFERENCE_PREFIX = "__parsezen_resources__/"


@dataclass(frozen=True, slots=True)
class ConvertedResource:
    """One local binary resource that must accompany converted Markdown."""

    relative_path: PurePosixPath
    content: bytes | None
    media_type: str
    content_path: Path | None = None
    _owner: object | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_path(
        cls,
        relative_path: PurePosixPath,
        content_path: Path,
        media_type: str,
        *,
        owner: object | None = None,
    ) -> ConvertedResource:
        """Reference a private temporary resource without loading it eagerly."""
        return cls(relative_path, None, media_type, content_path, owner)

    def read_content(self) -> bytes:
        """Read the resource once at a consumer boundary."""
        if self.content is not None:
            return self.content
        if self.content_path is None:
            raise ValueError("El recurso no tiene contenido local.")
        return self.content_path.read_bytes()


@dataclass(frozen=True, slots=True)
class ConvertedDocument:
    """Markdown plus any local resources required to render it faithfully."""

    markdown: str
    resources: tuple[ConvertedResource, ...] = ()
