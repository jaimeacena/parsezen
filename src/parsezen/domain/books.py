"""Normalized reflowable book model shared by every EPUB-capable source."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from uuid import UUID

_LANGUAGE_PATTERN = re.compile(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*|und")
_MAX_BOOK_SECTIONS = 250
_MAX_BOOK_DEPTH = 12


@dataclass(frozen=True, slots=True)
class BookMetadata:
    title: str
    language: str = "und"
    author: str | None = None
    identifier: str | None = None
    identifiers: tuple[str, ...] = ()
    publisher: str | None = None
    publication_date: str | None = None

    def __post_init__(self) -> None:
        if not self.title.strip() or "\0" in self.title or len(self.title) > 500:
            raise ValueError("Book title must be a non-empty bounded string.")
        if not _LANGUAGE_PATTERN.fullmatch(self.language):
            raise ValueError("Book language must be a normalized language code.")
        if self.author is not None and ("\0" in self.author or len(self.author) > 500):
            raise ValueError("Book author must be a bounded string.")
        if self.identifier is not None:
            UUID(self.identifier)
        if len(self.identifiers) > 50 or any(
            not value.strip() or "\0" in value or len(value) > 500 for value in self.identifiers
        ):
            raise ValueError("Book publication identifiers must be bounded strings.")
        if len(self.identifiers) != len(set(self.identifiers)):
            raise ValueError("Book publication identifiers must be unique.")
        if self.publisher is not None and ("\0" in self.publisher or len(self.publisher) > 500):
            raise ValueError("Book publisher must be a bounded string.")
        if self.publication_date is not None and (
            "\0" in self.publication_date or len(self.publication_date) > 100
        ):
            raise ValueError("Book publication date must be a bounded string.")


@dataclass(frozen=True, slots=True)
class BookResource:
    id: str
    href: str
    media_type: str
    payload_artifact_id: str

    def __post_init__(self) -> None:
        path = PurePosixPath(self.href)
        if (
            not self.id
            or not self.payload_artifact_id
            or not self.media_type
            or not self.href
            or path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
            or "\\" in self.href
            or any(character in self.href for character in "?#\0")
        ):
            raise ValueError("Book resources must use safe relative paths.")


@dataclass(frozen=True, slots=True)
class BookSection:
    id: str
    title: str
    xhtml_artifact_id: str
    children: tuple[BookSection, ...] = ()
    source_filename: str | None = None
    source_archive_path: str | None = None
    source_xhtml_artifact_id: str | None = None

    def __post_init__(self) -> None:
        if not self.id or not self.xhtml_artifact_id:
            raise ValueError("Book sections must reference stable artifacts.")
        if not self.title.strip() or "\0" in self.title or len(self.title) > 500:
            raise ValueError("Book section titles must be non-empty bounded strings.")
        if self.source_filename is not None:
            path = PurePosixPath(self.source_filename)
            if (
                path.name != self.source_filename
                or path.suffix.casefold() != ".xhtml"
                or any(character in self.source_filename for character in "?#\\\0")
            ):
                raise ValueError("Book source filenames must be safe XHTML basenames.")
        if self.source_archive_path is not None:
            archive_path = PurePosixPath(self.source_archive_path)
            if (
                archive_path.is_absolute()
                or not archive_path.parts
                or any(part in {"", ".", ".."} for part in archive_path.parts)
                or archive_path.suffix.casefold() not in {".xhtml", ".html", ".htm"}
                or any(character in self.source_archive_path for character in "?#\\\0")
            ):
                raise ValueError("Book source archive paths must be safe XHTML paths.")
        if self.source_xhtml_artifact_id is not None and not self.source_xhtml_artifact_id:
            raise ValueError("Book source XHTML artifacts must use stable identifiers.")
        if (self.source_archive_path is None) != (self.source_xhtml_artifact_id is None):
            raise ValueError("Book package source references must be complete.")

    def walk(self) -> tuple[BookSection, ...]:
        return (self, *(child for item in self.children for child in item.walk()))


@dataclass(frozen=True, slots=True)
class BookDocument:
    metadata: BookMetadata
    sections: tuple[BookSection, ...]
    spine: tuple[str, ...]
    resources: tuple[BookResource, ...] = ()
    stylesheet_artifact_ids: tuple[str, ...] = ()
    cover_resource_id: str | None = None
    source_fingerprint: str | None = None
    baseline_fingerprint: str | None = None
    source_package_artifact_id: str | None = None
    package_structure_fingerprint: str | None = None

    def __post_init__(self) -> None:
        pending = [(section, 1) for section in reversed(self.sections)]
        sections: list[BookSection] = []
        while pending:
            section, depth = pending.pop()
            if depth > _MAX_BOOK_DEPTH:
                raise ValueError("The book hierarchy is too deeply nested.")
            sections.append(section)
            pending.extend((child, depth + 1) for child in reversed(section.children))
            if len(sections) > _MAX_BOOK_SECTIONS:
                raise ValueError("The book contains too many sections.")
        all_sections = tuple(sections)
        if not all_sections:
            raise ValueError("A book must contain at least one section.")
        section_ids = tuple(section.id for section in all_sections)
        if len(section_ids) != len(set(section_ids)):
            raise ValueError("Book section ids must be unique.")
        source_filenames = tuple(
            section.source_filename.casefold()
            for section in all_sections
            if section.source_filename is not None
        )
        if len(source_filenames) != len(set(source_filenames)):
            raise ValueError("Book source filenames must be unique.")
        source_archive_paths = tuple(
            section.source_archive_path.casefold()
            for section in all_sections
            if section.source_archive_path is not None
        )
        if len(source_archive_paths) != len(set(source_archive_paths)):
            raise ValueError("Book source archive paths must be unique.")
        if self.spine != section_ids:
            raise ValueError("The spine must follow the complete section tree exactly once.")
        resource_ids = tuple(resource.id for resource in self.resources)
        if len(resource_ids) != len(set(resource_ids)):
            raise ValueError("Book resource ids must be unique.")
        resource_paths = tuple(resource.href.casefold() for resource in self.resources)
        if len(resource_paths) != len(set(resource_paths)):
            raise ValueError("Book resource paths must be unique.")
        if len(self.stylesheet_artifact_ids) != len(set(self.stylesheet_artifact_ids)):
            raise ValueError("Book stylesheets must be unique.")
        if self.cover_resource_id is not None and self.cover_resource_id not in resource_ids:
            raise ValueError("The cover must reference a known resource.")
        if self.source_fingerprint is not None and not re.fullmatch(
            r"[0-9a-f]{64}",
            self.source_fingerprint,
        ):
            raise ValueError("The book source fingerprint must be a SHA-256 digest.")
        if self.baseline_fingerprint is not None and not re.fullmatch(
            r"[0-9a-f]{64}",
            self.baseline_fingerprint,
        ):
            raise ValueError("The book baseline fingerprint must be a SHA-256 digest.")
        if self.source_package_artifact_id is not None and not self.source_package_artifact_id:
            raise ValueError("The source package must reference a stable artifact.")
        if self.package_structure_fingerprint is not None and not re.fullmatch(
            r"[0-9a-f]{64}",
            self.package_structure_fingerprint,
        ):
            raise ValueError("The package structure fingerprint must be a SHA-256 digest.")

    def section(self, section_id: str) -> BookSection:
        for root in self.sections:
            for section in root.walk():
                if section.id == section_id:
                    return section
        raise KeyError(section_id)
