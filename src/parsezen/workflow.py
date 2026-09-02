"""Small public output-format contract used by the local processing facade."""

from __future__ import annotations

from enum import StrEnum


class OutputFormat(StrEnum):
    """User-visible format produced by a document workflow."""

    MARKDOWN = "markdown"
    EPUB = "epub"
