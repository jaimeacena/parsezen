"""Origin-independent EPUB book editing and publication services."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from html import escape
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import cast
from urllib.parse import quote, unquote, urlsplit
from uuid import UUID, uuid4
from zipfile import ZipFile

from lxml import etree, html

from parsezen.application.artifact_repository import ArtifactRepository
from parsezen.document_model import ConvertedResource
from parsezen.domain.books import BookDocument, BookMetadata, BookResource, BookSection
from parsezen.epub_builder import (
    EpubBookMetadata,
    EpubChapterPlan,
    EpubNavigationNode,
    EpubOutlineEntry,
    build_epub,
    build_epub_from_xhtml,
    chapter_filename,
    plan_epub,
)
from parsezen.epub_conversion import read_editable_epub_package
from parsezen.errors import ConversionError

_FORBIDDEN_ELEMENTS = frozenset({"applet", "embed", "form", "iframe", "object", "script"})
_UNSAFE_URL = re.compile(r"^\s*(?:javascript|data:text/html)\s*:", re.IGNORECASE)
_REMOTE_URL = re.compile(r"^\s*(?://|(?:https?|ftp)\s*:)", re.IGNORECASE)
_EMBEDDED_URL = re.compile(r"^\s*data\s*:", re.IGNORECASE)
_CSS_IMPORT = re.compile(r"@import\b[^;]*(?:;|$)", re.IGNORECASE)
_CSS_REMOTE_URL = re.compile(
    r"url\(\s*(['\"]?)(?:https?|ftp|javascript|data):.*?\1\s*\)",
    re.IGNORECASE,
)
_COVER_MEDIA_TYPES = {
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}
_MAX_COVER_BYTES = 20 * 1024 * 1024
_NAVIGATION_HINT_ATTRIBUTE = "data-parsezen-navigation"
_NAVIGATION_LEVEL_ATTRIBUTE = "data-parsezen-navigation-level"
SectionTuple = tuple[BookSection, ...]


@dataclass(slots=True)
class _MutableBookNavigationNode:
    title: str
    fragment: str
    children: list[_MutableBookNavigationNode]


class BookEditor:
    """Safe immutable operations over a normalized reflowable book."""

    def __init__(self, book: BookDocument, artifacts: ArtifactRepository, *, job_id: str) -> None:
        self.book = book
        self.artifacts = artifacts
        self.job_id = job_id

    def editable_html(self, section_id: str) -> str:
        xhtml = self.artifacts.read_text(
            self.job_id,
            self.book.section(section_id).xhtml_artifact_id,
        )
        return _body_inner_html(xhtml)

    def rename(self, section_id: str, title: str) -> BookDocument:
        cleaned = _clean_title(title)
        return self._replace_section(section_id, lambda section: replace(section, title=cleaned))

    def update_metadata(
        self,
        *,
        title: str,
        author: str | None,
        language: str,
    ) -> BookDocument:
        """Update bounded publication metadata without touching book content."""

        metadata = BookMetadata(
            title=_clean_title(title),
            language=_clean_language(language),
            author=_clean_optional_text(author),
            identifier=self.book.metadata.identifier,
            identifiers=self.book.metadata.identifiers,
            publisher=self.book.metadata.publisher,
            publication_date=self.book.metadata.publication_date,
        )
        return replace(self.book, metadata=metadata)

    def remove_cover(self) -> BookDocument:
        """Remove the cover designation while preserving the underlying resource."""

        return replace(self.book, cover_resource_id=None)

    def replace_cover(self, filename: str, payload: bytes) -> BookDocument:
        """Store a validated local image and designate it as the book cover."""

        suffix = PurePosixPath(filename).suffix.casefold()
        media_type = _COVER_MEDIA_TYPES.get(suffix)
        if media_type is None:
            raise ValueError("La portada debe ser PNG, JPG, WEBP, GIF o SVG.")
        if not payload or len(payload) > _MAX_COVER_BYTES:
            raise ValueError("La imagen de portada está vacía o supera los 20 MB.")
        record = self.artifacts.put(
            job_id=self.job_id,
            payload=payload,
            media_type=media_type,
        )
        href = f"images/cover{suffix}"
        current = tuple(
            resource for resource in self.book.resources if resource.href.casefold() != href
        )
        resource = BookResource(uuid4().hex, href, media_type, record.id)
        return replace(
            self.book,
            resources=(*current, resource),
            cover_resource_id=resource.id,
        )

    def update_content(self, section_id: str, body_html: str) -> BookDocument:
        section = self.book.section(section_id)
        normalized = _xhtml_document(
            section.title,
            _sanitize_html_fragment(body_html),
            self.book.metadata.language,
        )
        current_xhtml = self.artifacts.read_text(
            self.job_id,
            section.xhtml_artifact_id,
        )
        if _xhtml_semantic_signature(normalized) == _xhtml_semantic_signature(current_xhtml):
            return self.book
        record = self.artifacts.put_text(
            job_id=self.job_id,
            text=normalized,
            media_type="application/xhtml+xml",
        )
        return self._replace_section(
            section_id,
            lambda current: replace(current, xhtml_artifact_id=record.id),
        )

    def add_after(self, section_id: str, title: str) -> BookDocument:
        cleaned = _clean_title(title)
        new_id = uuid4().hex
        record = self.artifacts.put_text(
            job_id=self.job_id,
            text=_xhtml_document(
                cleaned,
                f"<h1>{escape(cleaned)}</h1>",
                self.book.metadata.language,
            ),
            media_type="application/xhtml+xml",
        )
        created = BookSection(
            new_id,
            cleaned,
            record.id,
            source_filename=f"section-{new_id}.xhtml",
        )
        sections, inserted = _insert_after(self.book.sections, section_id, created)
        if not inserted:
            raise KeyError(section_id)
        return replace(self.book, sections=sections, spine=_spine(sections))

    def split(
        self,
        section_id: str,
        first_body_html: str,
        second_body_html: str,
        second_title: str,
    ) -> BookDocument:
        if not _visible_text(first_body_html) or not _visible_text(second_body_html):
            raise ValueError("Ambas partes deben conservar contenido.")
        first = self.update_content(section_id, first_body_html)
        return BookEditor(first, self.artifacts, job_id=self.job_id)._insert_split_section(
            section_id,
            second_body_html,
            second_title,
        )

    def merge_with_previous(self, section_id: str) -> BookDocument:
        position = self.book.spine.index(section_id)
        if position == 0:
            raise ValueError("El primer capítulo no tiene una división anterior.")
        previous_id = self.book.spine[position - 1]
        merged = (
            self.editable_html(previous_id).rstrip()
            + "\n"
            + self.editable_html(section_id).lstrip()
        )
        updated = self.update_content(previous_id, merged)
        sections, removed = _remove_section(updated.sections, section_id)
        if not removed:
            raise KeyError(section_id)
        return replace(updated, sections=sections, spine=_spine(sections))

    def move(self, section_id: str, offset: int) -> BookDocument:
        sections, moved = _move_sibling(self.book.sections, section_id, offset)
        return (
            self.book
            if not moved
            else replace(self.book, sections=sections, spine=_spine(sections))
        )

    def indent(self, section_id: str) -> BookDocument:
        sections, changed = _indent_section(self.book.sections, section_id)
        return (
            self.book
            if not changed
            else replace(self.book, sections=sections, spine=_spine(sections))
        )

    def outdent(self, section_id: str) -> BookDocument:
        sections, changed = _outdent_section(self.book.sections, section_id)
        return (
            self.book
            if not changed
            else replace(self.book, sections=sections, spine=_spine(sections))
        )

    def _insert_split_section(
        self,
        section_id: str,
        body_html: str,
        title: str,
    ) -> BookDocument:
        cleaned = _clean_title(title)
        new_id = uuid4().hex
        record = self.artifacts.put_text(
            job_id=self.job_id,
            text=_xhtml_document(
                cleaned,
                _sanitize_html_fragment(body_html),
                self.book.metadata.language,
            ),
            media_type="application/xhtml+xml",
        )
        created = BookSection(
            new_id,
            cleaned,
            record.id,
            source_filename=f"section-{new_id}.xhtml",
        )
        sections, inserted = _insert_after(self.book.sections, section_id, created)
        if not inserted:
            raise KeyError(section_id)
        return replace(self.book, sections=sections, spine=_spine(sections))

    def _replace_section(
        self,
        section_id: str,
        transform: Callable[[BookSection], BookSection],
    ) -> BookDocument:
        sections, changed = _map_section(self.book.sections, section_id, transform)
        if not changed:
            raise KeyError(section_id)
        return replace(self.book, sections=sections)


def create_book_from_markdown(
    markdown: str,
    resources: tuple[ConvertedResource, ...],
    metadata: EpubBookMetadata,
    artifacts: ArtifactRepository,
    *,
    job_id: str,
) -> BookDocument:
    """Normalize every EPUB-capable source through the same EPUB 3 representation."""

    built = build_epub(markdown, resources, metadata)
    plan = plan_epub(markdown, metadata.title)
    sections: list[BookSection] = []
    book_resources: list[BookResource] = []
    outline_by_chapter: dict[int, list[EpubOutlineEntry]] = {}
    for entry in plan.outline:
        outline_by_chapter.setdefault(entry.chapter_number, []).append(entry)
    with ZipFile(BytesIO(built.content)) as archive:
        for index, chapter in enumerate(plan.chapters, start=1):
            xhtml = archive.read(f"EPUB/text/{chapter.filename}")
            record = artifacts.put(
                job_id=job_id,
                payload=_with_navigation_hints(
                    xhtml.decode("utf-8"),
                    tuple(outline_by_chapter.get(index, ())),
                ).encode("utf-8"),
                media_type="application/xhtml+xml",
            )
            sections.append(
                BookSection(
                    f"section-{index:04d}",
                    chapter.title,
                    record.id,
                    source_filename=chapter.filename,
                )
            )
    for index, resource in enumerate(resources, start=1):
        record = artifacts.put(
            job_id=job_id,
            payload=resource.read_content(),
            media_type=resource.media_type,
        )
        book_resources.append(
            BookResource(
                f"resource-{index:04d}",
                resource.relative_path.as_posix(),
                resource.media_type,
                record.id,
            )
        )
    roles_by_id = {
        section.id: chapter.role for section, chapter in zip(sections, plan.chapters, strict=True)
    }
    nested_sections = _nest_explicit_chapter_sections(
        _nest_toc_sections(
            tuple(sections),
            tuple(chapter.toc_level for chapter in plan.chapters),
        ),
        roles_by_id,
    )
    book = BookDocument(
        metadata=BookMetadata(
            _clean_title(metadata.title),
            _clean_language(metadata.language),
            _clean_optional_text(metadata.author),
            str(uuid4()),
            tuple(dict.fromkeys(metadata.identifiers)),
            _clean_optional_text(metadata.publisher),
            _clean_optional_text(metadata.publication_date),
        ),
        sections=nested_sections,
        spine=_spine(nested_sections),
        resources=tuple(book_resources),
        cover_resource_id=next(
            (
                resource.id
                for resource in book_resources
                if metadata.cover_resource is not None
                and resource.href == metadata.cover_resource.as_posix()
            ),
            None,
        ),
        source_fingerprint=book_source_fingerprint(markdown),
    )
    return replace(
        book,
        baseline_fingerprint=book_content_fingerprint(
            book,
            artifacts,
            job_id=job_id,
        ),
    )


def create_book_from_epub_package(
    source_path: Path,
    reviewed_markdown: str,
    resources: tuple[ConvertedResource, ...],
    metadata: EpubBookMetadata,
    artifacts: ArtifactRepository,
    *,
    job_id: str,
) -> BookDocument:
    """Create an editor model bound to real spine files and the exact local EPUB package."""

    package = read_editable_epub_package(source_path)
    package_record = artifacts.put(
        job_id=job_id,
        payload=package.content,
        media_type="application/epub+zip",
    )
    sections: list[BookSection] = []
    for index, chapter in enumerate(package.chapters, start=1):
        parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
        try:
            root = etree.fromstring(chapter.xhtml, parser)
        except etree.XMLSyntaxError as exc:
            raise ConversionError("Un capítulo EPUB contiene XHTML no válido.") from exc
        bodies = root.xpath("//*[local-name()='body']")
        if not bodies:
            raise ConversionError("Un capítulo EPUB no contiene cuerpo editable.")
        body = bodies[0]
        body_html = (body.text or "") + "".join(
            etree.tostring(child, encoding="unicode", method="html") for child in body
        )
        editable_xhtml = _xhtml_document(
            _clean_title(chapter.title),
            _sanitize_html_fragment(body_html),
            _clean_language(metadata.language),
        )
        record = artifacts.put_text(
            job_id=job_id,
            text=editable_xhtml,
            media_type="application/xhtml+xml",
        )
        sections.append(
            BookSection(
                f"section-{index:04d}",
                _clean_title(chapter.title),
                record.id,
                source_filename=chapter_filename(index),
                source_archive_path=chapter.archive_path,
                source_xhtml_artifact_id=record.id,
            )
        )
    book_resources: list[BookResource] = []
    for index, resource in enumerate(resources, start=1):
        record = artifacts.put(
            job_id=job_id,
            payload=resource.read_content(),
            media_type=resource.media_type,
        )
        book_resources.append(
            BookResource(
                f"resource-{index:04d}",
                resource.relative_path.as_posix(),
                resource.media_type,
                record.id,
            )
        )
    book = BookDocument(
        metadata=BookMetadata(
            _clean_title(metadata.title),
            _clean_language(metadata.language),
            _clean_optional_text(metadata.author),
            str(uuid4()),
            tuple(dict.fromkeys(metadata.identifiers)),
            _clean_optional_text(metadata.publisher),
            _clean_optional_text(metadata.publication_date),
        ),
        sections=tuple(sections),
        spine=tuple(section.id for section in sections),
        resources=tuple(book_resources),
        cover_resource_id=next(
            (
                resource.id
                for resource in book_resources
                if metadata.cover_resource is not None
                and resource.href == metadata.cover_resource.as_posix()
            ),
            None,
        ),
        source_fingerprint=book_source_fingerprint(reviewed_markdown),
        source_package_artifact_id=package_record.id,
    )
    structure_fingerprint = book_package_structure_fingerprint(
        book,
        artifacts,
        job_id=job_id,
    )
    return replace(
        book,
        baseline_fingerprint=book_content_fingerprint(book, artifacts, job_id=job_id),
        package_structure_fingerprint=structure_fingerprint,
    )


def book_source_fingerprint(markdown: str) -> str:
    """Bind a durable editor draft to the reviewed text it was built from."""

    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def book_content_fingerprint(
    book: BookDocument,
    artifacts: ArtifactRepository,
    *,
    job_id: str,
) -> str:
    """Hash the complete editable book state without retaining or logging its contents."""

    return _book_state_fingerprint(
        book,
        artifacts,
        job_id=job_id,
        include_section_content=True,
    )


def book_package_structure_fingerprint(
    book: BookDocument,
    artifacts: ArtifactRepository,
    *,
    job_id: str,
) -> str:
    """Hash every package-relevant edit except replaceable XHTML body content."""

    return _book_state_fingerprint(
        book,
        artifacts,
        job_id=job_id,
        include_section_content=False,
    )


def _book_state_fingerprint(
    book: BookDocument,
    artifacts: ArtifactRepository,
    *,
    job_id: str,
    include_section_content: bool,
) -> str:

    digest = hashlib.sha256()

    def add(value: str | None) -> None:
        payload = ("<none>" if value is None else value).encode("utf-8")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)

    def add_artifact(identifier: str) -> None:
        payload = artifacts.read(job_id, identifier)
        add(hashlib.sha256(payload).hexdigest())

    metadata = book.metadata
    for value in (
        metadata.title,
        metadata.language,
        metadata.author,
        metadata.identifier,
        *metadata.identifiers,
        metadata.publisher,
        metadata.publication_date,
    ):
        add(value)

    def add_section(section: BookSection) -> None:
        add("section")
        add(section.id)
        add(section.title)
        add(section.source_filename)
        add(section.source_archive_path)
        add(section.source_xhtml_artifact_id)
        if include_section_content:
            add_artifact(section.xhtml_artifact_id)
        for child in section.children:
            add_section(child)
        add("end-section")

    for section in book.sections:
        add_section(section)
    for section_id in book.spine:
        add(section_id)
    for resource in book.resources:
        add(resource.id)
        add(resource.href)
        add(resource.media_type)
        add_artifact(resource.payload_artifact_id)
    for stylesheet_id in book.stylesheet_artifact_ids:
        add_artifact(stylesheet_id)
    add(book.cover_resource_id)
    add(book.source_fingerprint)
    return digest.hexdigest()


def publish_book(
    book: BookDocument,
    artifacts: ArtifactRepository,
    *,
    job_id: str,
) -> bytes:
    """Generate a validated EPUB archive from the normalized editor model."""

    sections_by_id = {section.id: section for root in book.sections for section in root.walk()}
    resource_paths = frozenset(resource.href.casefold() for resource in book.resources)
    chapters: list[EpubChapterPlan] = []
    rendered: list[str] = []
    filenames = {
        section_id: chapter_filename(index) for index, section_id in enumerate(book.spine, start=1)
    }
    source_filenames = _source_filename_index(sections_by_id)
    raw_xhtml = {
        section_id: artifacts.read_text(
            job_id,
            sections_by_id[section_id].xhtml_artifact_id,
        )
        for section_id in book.spine
    }
    anchors = _book_anchor_index(raw_xhtml, sections_by_id)
    rendered_by_section: dict[str, str] = {}
    for section_id in book.spine:
        section = sections_by_id[section_id]
        filename = filenames[section_id]
        chapters.append(EpubChapterPlan(filename, section.title, ""))
        validated = _validated_xhtml(
            raw_xhtml[section_id],
            section.title,
            resource_paths,
            section_id=section_id,
            filenames=filenames,
            source_filenames=source_filenames,
            anchors=anchors,
        )
        prepared = _ensure_navigation_heading_ids(validated)
        rendered.append(prepared)
        rendered_by_section[section_id] = prepared
    resources = tuple(
        ConvertedResource(
            PurePosixPath(resource.href),
            artifacts.read(job_id, resource.payload_artifact_id),
            resource.media_type,
        )
        for resource in book.resources
    )
    cover = (
        PurePosixPath(
            next(
                resource.href
                for resource in book.resources
                if resource.id == book.cover_resource_id
            )
        )
        if book.cover_resource_id is not None
        else None
    )
    navigation = _navigation(book.sections, filenames, rendered_by_section)
    published_rendered = tuple(_without_navigation_hints(xhtml) for xhtml in rendered)
    built = build_epub_from_xhtml(
        tuple(chapters),
        published_rendered,
        resources,
        EpubBookMetadata(
            book.metadata.title,
            book.metadata.language,
            book.metadata.author,
            cover,
            identifiers=book.metadata.identifiers,
            publisher=book.metadata.publisher,
            publication_date=book.metadata.publication_date,
        ),
        navigation=navigation,
        identifier=(
            UUID(book.metadata.identifier) if book.metadata.identifier is not None else None
        ),
        stylesheet=_book_stylesheet(book, artifacts, job_id=job_id),
    )
    return built.content


def _navigation(
    sections: tuple[BookSection, ...],
    filenames: dict[str, str],
    xhtml_by_section: dict[str, str],
) -> tuple[EpubNavigationNode, ...]:
    navigation: list[EpubNavigationNode] = []
    for section in sections:
        xhtml = xhtml_by_section[section.id]
        children = (
            *_heading_navigation(section, filenames[section.id], xhtml),
            *_navigation(section.children, filenames, xhtml_by_section),
        )
        if _section_navigation_is_excluded(xhtml):
            navigation.extend(children)
            continue
        navigation.append(
            EpubNavigationNode(
                section.title,
                filenames[section.id],
                children,
            )
        )
    return tuple(navigation)


def _section_navigation_is_excluded(xhtml: str) -> bool:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
    root = etree.fromstring(xhtml.encode("utf-8"), parser)
    for element in root.iter():
        local_name = (
            etree.QName(element).localname.casefold() if isinstance(element.tag, str) else ""
        )
        if re.fullmatch(r"h[1-6]", local_name) is not None:
            return element.attrib.get(_NAVIGATION_HINT_ATTRIBUTE) == "exclude"
    return False


def _ensure_navigation_heading_ids(xhtml: str) -> str:
    """Add stable local targets only to headings that do not already have one."""

    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
    root = etree.fromstring(xhtml.encode("utf-8"), parser)
    used_ids = {anchor for element in root.iter() if (anchor := element.attrib.get("id"))}
    heading_number = 0
    for element in root.iter():
        local_name = (
            etree.QName(element).localname.casefold() if isinstance(element.tag, str) else ""
        )
        if re.fullmatch(r"h[1-6]", local_name) is None:
            continue
        heading_number += 1
        if element.attrib.get("id"):
            continue
        candidate_number = heading_number
        while f"nav-heading-{candidate_number:04d}" in used_ids:
            candidate_number += 1
        anchor = f"nav-heading-{candidate_number:04d}"
        element.attrib["id"] = anchor
        used_ids.add(anchor)
    return cast(str, etree.tostring(root, encoding="unicode", xml_declaration=False))


def _with_navigation_hints(
    xhtml: str,
    outline: tuple[EpubOutlineEntry, ...],
) -> str:
    """Keep the reviewed menu selection inside the encrypted editor draft only."""

    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
    root = etree.fromstring(xhtml.encode("utf-8"), parser)
    headings = [
        element
        for element in root.iter()
        if isinstance(element.tag, str)
        and re.fullmatch(r"h[1-6]", etree.QName(element).localname.casefold()) is not None
    ]
    if len(headings) != len(outline):
        return xhtml
    for element, entry in zip(headings, outline, strict=True):
        element.attrib[_NAVIGATION_HINT_ATTRIBUTE] = (
            "include" if entry.include_in_navigation else "exclude"
        )
        element.attrib[_NAVIGATION_LEVEL_ATTRIBUTE] = str(entry.navigation_level or entry.level)
    return cast(str, etree.tostring(root, encoding="unicode", xml_declaration=False))


def _without_navigation_hints(xhtml: str) -> str:
    """Remove private planning hints from the user-facing EPUB."""

    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
    root = etree.fromstring(xhtml.encode("utf-8"), parser)
    for element in root.iter():
        element.attrib.pop(_NAVIGATION_HINT_ATTRIBUTE, None)
        element.attrib.pop(_NAVIGATION_LEVEL_ATTRIBUTE, None)
    return cast(str, etree.tostring(root, encoding="unicode", xml_declaration=False))


def _heading_navigation(
    section: BookSection,
    filename: str,
    xhtml: str,
) -> tuple[EpubNavigationNode, ...]:
    """Build the in-document outline that belongs below one spine section."""

    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
    root = etree.fromstring(xhtml.encode("utf-8"), parser)
    entries: list[tuple[int, str, str]] = []
    seen_titles = {_normalized_navigation_title(section.title)}
    for element in root.iter():
        local_name = (
            etree.QName(element).localname.casefold() if isinstance(element.tag, str) else ""
        )
        if re.fullmatch(r"h[1-6]", local_name) is None:
            continue
        title = " ".join("".join(element.itertext()).split()).strip()
        fragment = element.attrib.get("id", "").strip()
        normalized_title = _normalized_navigation_title(title)
        repeated_chapter_marker = (
            _bare_navigation_chapter_marker(title) is not None
            and _bare_navigation_chapter_marker(title) == _navigation_chapter_marker(section.title)
            and normalized_title != _normalized_navigation_title(section.title)
        )
        if (
            title
            and fragment
            and element.attrib.get(_NAVIGATION_HINT_ATTRIBUTE) != "exclude"
            and normalized_title not in seen_titles
            and not repeated_chapter_marker
        ):
            hinted_level = element.attrib.get(_NAVIGATION_LEVEL_ATTRIBUTE, "")
            navigation_level = (
                int(hinted_level) if re.fullmatch(r"[1-6]", hinted_level) else int(local_name[1])
            )
            entries.append((navigation_level, title[:500], fragment))
            seen_titles.add(normalized_title)

    mutable_roots: list[_MutableBookNavigationNode] = []
    mutable_stack: list[tuple[int, _MutableBookNavigationNode]] = []
    for level, title, fragment in entries:
        node = _MutableBookNavigationNode(title, fragment, [])
        while mutable_stack and mutable_stack[-1][0] >= level:
            mutable_stack.pop()
        if mutable_stack:
            mutable_stack[-1][1].children.append(node)
        else:
            mutable_roots.append(node)
        mutable_stack.append((level, node))

    def freeze(node: _MutableBookNavigationNode) -> EpubNavigationNode:
        return EpubNavigationNode(
            node.title,
            filename,
            tuple(freeze(child) for child in node.children),
            node.fragment,
        )

    return tuple(freeze(node) for node in mutable_roots)


def _normalized_navigation_title(value: str) -> str:
    return re.sub(r"[^\w]+", " ", value, flags=re.UNICODE).strip().casefold()


def _navigation_chapter_marker(value: str) -> str | None:
    normalized = _normalized_navigation_title(value)
    match = re.match(
        r"^(?:chapter|chapitre|capitulo|capítulo|cap)\s+"
        r"(?:\d{1,3}|[ivxlcdm]+|one|two|three|four|five|six|seven|eight|nine|ten|"
        r"uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez)\b",
        normalized,
    )
    return match.group(0) if match is not None else None


def _bare_navigation_chapter_marker(value: str) -> str | None:
    marker = _navigation_chapter_marker(value)
    return marker if marker == _normalized_navigation_title(value) else None


def _xhtml_document(title: str, body: str, language: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        f'xml:lang="{escape(language, quote=True)}" '
        f'lang="{escape(language, quote=True)}">'
        f"<head><title>{escape(title)}</title>"
        '<meta charset="utf-8" />'
        '<link rel="stylesheet" type="text/css" href="../styles/book.css" />'
        f"</head><body>{body}</body></html>"
    )


def _body_inner_html(xhtml: str) -> str:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
    try:
        root = etree.fromstring(xhtml.encode("utf-8"), parser)
    except etree.XMLSyntaxError as exc:
        raise ConversionError("Un capítulo editable contiene XHTML no válido.") from exc
    bodies = root.xpath("//*[local-name()='body']")
    if not bodies:
        raise ConversionError("Un capítulo editable no contiene cuerpo.")
    body = bodies[0]
    return (body.text or "") + "".join(
        etree.tostring(child, encoding="unicode", method="html") for child in body
    )


def _xhtml_semantic_signature(xhtml: str) -> tuple[object, ...]:
    """Compare sanitized XHTML semantics without treating namespace prefixes as edits."""

    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
    try:
        root = etree.fromstring(xhtml.encode("utf-8"), parser)
    except etree.XMLSyntaxError as exc:
        raise ConversionError("Un capítulo editable contiene XHTML no válido.") from exc

    def insignificant_layout(value: str | None, container: str) -> str | None:
        return (
            None
            if value is not None
            and not value.strip()
            and container
            in {
                "html",
                "head",
                "body",
            }
            else value
        )

    def signature(
        element: etree._Element,
        *,
        parent_name: str = "",
    ) -> tuple[object, ...]:
        tag = etree.QName(element).text if isinstance(element.tag, str) else "#comment"
        local_name = (
            etree.QName(element).localname.casefold() if isinstance(element.tag, str) else ""
        )
        attributes = tuple(
            sorted((etree.QName(name).text, value) for name, value in element.attrib.items())
        )
        return (
            tag,
            attributes,
            insignificant_layout(element.text, local_name),
            tuple(signature(child, parent_name=local_name) for child in element),
            insignificant_layout(element.tail, parent_name),
        )

    return signature(root)


def _sanitize_html_fragment(fragment: str) -> str:
    if "\0" in fragment or len(fragment) > 5_000_000:
        raise ValueError("El contenido del capítulo no es válido.")
    container = html.fragment_fromstring(fragment or "<p></p>", create_parent="div")
    for element in tuple(container.iterdescendants()):
        tag = element.tag.casefold() if isinstance(element.tag, str) else ""
        if tag in _FORBIDDEN_ELEMENTS:
            element.drop_tree()
            continue
        for attribute in tuple(element.attrib):
            value = element.attrib[attribute]
            if attribute.casefold().startswith("on"):
                del element.attrib[attribute]
            elif attribute.casefold() in {"href", "src"} and _UNSAFE_URL.match(value):
                del element.attrib[attribute]
            elif attribute.casefold() == "src" and _REMOTE_URL.match(value):
                del element.attrib[attribute]
            elif attribute.casefold() == "src" and _EMBEDDED_URL.match(value):
                del element.attrib[attribute]
    return (container.text or "") + "".join(
        etree.tostring(child, encoding="unicode", method="xml") for child in container
    )


def _visible_text(fragment: str) -> str:
    return " ".join(html.fromstring(f"<div>{fragment}</div>").text_content().split())


def _clean_title(title: str) -> str:
    cleaned = " ".join(title.replace("\0", "").split()).strip()
    if not cleaned:
        raise ValueError("El título no puede estar vacío.")
    return cleaned[:500]


def _clean_optional_text(value: str | None) -> str | None:
    cleaned = " ".join((value or "").replace("\0", "").split()).strip()
    return cleaned[:500] or None


def _clean_language(language: str) -> str:
    cleaned = language.strip().replace("_", "-").casefold()
    if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*|und", cleaned):
        raise ValueError("El idioma debe usar un código como es, en o pt-BR.")
    return cleaned


def _validated_xhtml(
    xhtml: str,
    title: str,
    resource_paths: frozenset[str],
    *,
    section_id: str,
    filenames: dict[str, str],
    source_filenames: dict[str, str],
    anchors: dict[str, tuple[str, ...]],
) -> str:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
    try:
        root = etree.fromstring(xhtml.encode("utf-8"), parser)
    except (UnicodeEncodeError, etree.XMLSyntaxError) as exc:
        raise ConversionError(f'El capítulo "{title}" contiene XHTML no válido.') from exc
    if etree.QName(root).localname.casefold() != "html":
        raise ConversionError(f'El capítulo "{title}" no contiene un documento XHTML.')
    bodies = root.xpath("//*[local-name()='body']")
    if len(bodies) != 1:
        raise ConversionError(f'El capítulo "{title}" no contiene un cuerpo único.')
    unresolved_links: list[etree._Element] = []
    for element in root.iter():
        local_name = (
            etree.QName(element).localname.casefold() if isinstance(element.tag, str) else ""
        )
        if local_name in _FORBIDDEN_ELEMENTS:
            raise ConversionError(f'El capítulo "{title}" contiene contenido activo no permitido.')
        for attribute, value in tuple(element.attrib.items()):
            name = etree.QName(attribute).localname.casefold()
            if name.startswith("on") or (name in {"href", "src"} and _UNSAFE_URL.match(value)):
                raise ConversionError(f'El capítulo "{title}" contiene un enlace no seguro.')
            if name == "src" and _REMOTE_URL.match(value):
                raise ConversionError(f'El capítulo "{title}" contiene una imagen remota.')
            if name == "src" and _EMBEDDED_URL.match(value):
                raise ConversionError(f'El capítulo "{title}" contiene una imagen incrustada.')
            if local_name == "img" and name == "src":
                resolved = _book_resource_path(value)
                if resolved is None or resolved not in resource_paths:
                    raise ConversionError(
                        f'El capítulo "{title}" referencia una imagen que no está disponible.'
                    )
            if name == "href":
                rewritten = _rewritten_book_href(
                    value,
                    title=title,
                    section_id=section_id,
                    filenames=filenames,
                    source_filenames=source_filenames,
                    anchors=anchors,
                )
                if rewritten is None:
                    del element.attrib[attribute]
                    if local_name == "a":
                        unresolved_links.append(element)
                else:
                    element.attrib[attribute] = rewritten
    for element in unresolved_links:
        _unwrap_element(element)
    return cast(str, etree.tostring(root, encoding="unicode", xml_declaration=False))


def _unwrap_element(element: etree._Element) -> None:
    """Remove one inert link element without removing its visible descendants."""

    parent = element.getparent()
    if parent is None:
        return
    previous = element.getprevious()
    if element.text:
        if previous is None:
            parent.text = f"{parent.text or ''}{element.text}"
        else:
            previous.tail = f"{previous.tail or ''}{element.text}"
    insertion_index = parent.index(element)
    for child in tuple(element):
        element.remove(child)
        parent.insert(insertion_index, child)
        insertion_index += 1
        previous = child
    if element.tail:
        if previous is None:
            parent.text = f"{parent.text or ''}{element.tail}"
        else:
            previous.tail = f"{previous.tail or ''}{element.tail}"
    parent.remove(element)


def _book_anchor_index(
    xhtml_by_section: dict[str, str],
    sections_by_id: dict[str, BookSection],
) -> dict[str, tuple[str, ...]]:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
    collected: dict[str, list[str]] = {}
    for section_id, xhtml in xhtml_by_section.items():
        try:
            root = etree.fromstring(xhtml.encode("utf-8"), parser)
        except (UnicodeEncodeError, etree.XMLSyntaxError) as exc:
            title = sections_by_id[section_id].title
            raise ConversionError(f'El capítulo "{title}" contiene XHTML no válido.') from exc
        local_ids: set[str] = set()
        for element in root.iter():
            anchor = element.attrib.get("id")
            if not anchor:
                continue
            if anchor in local_ids:
                title = sections_by_id[section_id].title
                raise ConversionError(f'El capítulo "{title}" contiene anclas duplicadas.')
            local_ids.add(anchor)
            collected.setdefault(anchor, []).append(section_id)
    return {anchor: tuple(section_ids) for anchor, section_ids in collected.items()}


def _source_filename_index(sections_by_id: dict[str, BookSection]) -> dict[str, str]:
    indexed = {
        section.source_filename.casefold(): section.id
        for section in sections_by_id.values()
        if section.source_filename is not None
    }
    for section in sections_by_id.values():
        legacy = re.fullmatch(r"section-(\d{4})", section.id, re.IGNORECASE)
        if legacy is None:
            continue
        number = int(legacy.group(1))
        try:
            canonical = chapter_filename(number)
        except ValueError:
            continue
        indexed.setdefault(f"chapter-{number:03d}.xhtml", section.id)
        indexed.setdefault(canonical, section.id)
    return indexed


def _rewritten_book_href(
    value: str,
    *,
    title: str,
    section_id: str,
    filenames: dict[str, str],
    source_filenames: dict[str, str],
    anchors: dict[str, tuple[str, ...]],
) -> str | None:
    try:
        parsed = urlsplit(value.strip())
    except ValueError as exc:
        raise ConversionError(f'El capítulo "{title}" contiene un enlace no válido.') from exc
    if parsed.scheme or parsed.netloc:
        return value
    path = unquote(parsed.path).replace("\\", "/")
    if path and PurePosixPath(path).suffix.casefold() not in {".html", ".xhtml"}:
        return value
    target_from_path = source_filenames.get(PurePosixPath(path).name.casefold()) if path else None
    fragment = unquote(parsed.fragment)
    if not fragment:
        if not path:
            return value
        if target_from_path is None:
            return None
        return filenames[target_from_path]

    candidates = anchors.get(fragment, ())
    target_id: str | None = None
    if target_from_path is not None and target_from_path in candidates:
        target_id = target_from_path
    elif not path and section_id in candidates:
        target_id = section_id
    elif len(candidates) == 1:
        target_id = candidates[0]
    if target_id is None:
        return None
    encoded_fragment = quote(fragment, safe="!$&'()*+,;=:@-._~")
    if target_id == section_id:
        return f"#{encoded_fragment}"
    return f"{filenames[target_id]}#{encoded_fragment}"


def _book_resource_path(value: str) -> str | None:
    normalized = value.strip().replace("\\", "/").split("#", 1)[0].split("?", 1)[0]
    for prefix in ("../images/", "EPUB/images/", "images/"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    normalized = normalized.lstrip("/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        return None
    return path.as_posix().casefold()


def _book_stylesheet(
    book: BookDocument,
    artifacts: ArtifactRepository,
    *,
    job_id: str,
) -> str | None:
    if not book.stylesheet_artifact_ids:
        return None
    styles = "\n\n".join(
        artifacts.read_text(job_id, artifact_id) for artifact_id in book.stylesheet_artifact_ids
    )
    if "\0" in styles or len(styles) > 1_000_000:
        raise ConversionError("Los estilos del libro no son válidos.")
    styles = _CSS_IMPORT.sub("", styles)
    styles = _CSS_REMOTE_URL.sub("url()", styles)
    if re.search(r"\bexpression\s*\(", styles, re.IGNORECASE):
        raise ConversionError("Los estilos del libro contienen expresiones no seguras.")
    return styles.strip() or None


def _spine(sections: tuple[BookSection, ...]) -> tuple[str, ...]:
    return tuple(section.id for root in sections for section in root.walk())


def _nest_explicit_chapter_sections(
    sections: tuple[BookSection, ...],
    roles_by_id: dict[str, str | None],
) -> tuple[BookSection, ...]:
    """Nest a container only around two or more unambiguous chapter siblings."""

    sections = tuple(
        replace(
            section,
            children=_nest_explicit_chapter_sections(section.children, roles_by_id),
        )
        for section in sections
    )
    roots: list[BookSection] = []
    index = 0
    while index < len(sections):
        if roles_by_id.get(sections[index].id) != "container" or sections[index].children:
            roots.append(sections[index])
            index += 1
            continue
        end = index + 1
        while end < len(sections) and roles_by_id.get(sections[end].id) == "chapter":
            end += 1
        if end - index - 1 >= 2:
            roots.append(replace(sections[index], children=sections[index + 1 : end]))
            index = end
            continue
        roots.append(sections[index])
        index += 1
    return tuple(roots)


def _nest_toc_sections(
    sections: tuple[BookSection, ...],
    toc_levels: tuple[int | None, ...],
) -> tuple[BookSection, ...]:
    """Apply exact printed-contents depth only across consecutive matched spine sections."""

    if len(sections) != len(toc_levels):
        raise ValueError("Las secciones y los niveles del índice deben tener la misma longitud.")
    children_by_id: dict[str, list[str]] = {section.id: [] for section in sections}
    sections_by_id = {section.id: section for section in sections}
    roots: list[str] = []
    stack: list[tuple[int, str]] = []
    nested = False
    for section, level in zip(sections, toc_levels, strict=True):
        if level is None:
            roots.append(section.id)
            stack.clear()
            continue
        while stack and stack[-1][0] >= level:
            stack.pop()
        if stack:
            children_by_id[stack[-1][1]].append(section.id)
            nested = True
        else:
            roots.append(section.id)
        stack.append((level, section.id))
    if not nested:
        return sections

    def freeze(section_id: str) -> BookSection:
        section = sections_by_id[section_id]
        return replace(
            section,
            children=tuple(freeze(child_id) for child_id in children_by_id[section_id]),
        )

    return tuple(freeze(section_id) for section_id in roots)


def _map_section(
    sections: SectionTuple,
    target_id: str,
    transform: Callable[[BookSection], BookSection],
) -> tuple[SectionTuple, bool]:
    changed = False
    output: list[BookSection] = []
    for section in sections:
        current = section
        if section.id == target_id:
            current = transform(section)
            changed = True
        else:
            children, child_changed = _map_section(section.children, target_id, transform)
            if child_changed:
                current = replace(section, children=children)
                changed = True
        output.append(current)
    return tuple(output), changed


def _insert_after(
    sections: SectionTuple,
    target_id: str,
    created: BookSection,
) -> tuple[SectionTuple, bool]:
    output: list[BookSection] = []
    inserted = False
    for section in sections:
        output.append(section)
        if section.id == target_id:
            output.append(created)
            inserted = True
            continue
        children, child_inserted = _insert_after(section.children, target_id, created)
        if child_inserted:
            output[-1] = replace(section, children=children)
            inserted = True
    return tuple(output), inserted


def _remove_section(
    sections: SectionTuple,
    target_id: str,
) -> tuple[SectionTuple, bool]:
    output: list[BookSection] = []
    removed = False
    for section in sections:
        if section.id == target_id:
            output.extend(section.children)
            removed = True
            continue
        children, child_removed = _remove_section(section.children, target_id)
        output.append(replace(section, children=children) if child_removed else section)
        removed = removed or child_removed
    return tuple(output), removed


def _move_sibling(
    sections: SectionTuple,
    target_id: str,
    offset: int,
) -> tuple[SectionTuple, bool]:
    items = list(sections)
    for index, section in enumerate(items):
        if section.id == target_id:
            target = index + offset
            if not 0 <= target < len(items):
                return sections, False
            item = items.pop(index)
            items.insert(target, item)
            return tuple(items), True
        children, moved = _move_sibling(section.children, target_id, offset)
        if moved:
            items[index] = replace(section, children=children)
            return tuple(items), True
    return sections, False


def _indent_section(
    sections: SectionTuple,
    target_id: str,
) -> tuple[SectionTuple, bool]:
    items = list(sections)
    for index, section in enumerate(items):
        if section.id == target_id:
            if index == 0:
                return sections, False
            previous = items[index - 1]
            items[index - 1] = replace(previous, children=(*previous.children, section))
            items.pop(index)
            return tuple(items), True
        children, changed = _indent_section(section.children, target_id)
        if changed:
            items[index] = replace(section, children=children)
            return tuple(items), True
    return sections, False


def _outdent_section(
    sections: SectionTuple,
    target_id: str,
) -> tuple[SectionTuple, bool]:
    items = list(sections)
    for parent_index, parent in enumerate(items):
        children = list(parent.children)
        for child_index, child in enumerate(children):
            if child.id == target_id:
                children.pop(child_index)
                items[parent_index] = replace(parent, children=tuple(children))
                items.insert(parent_index + 1, child)
                return tuple(items), True
        nested, changed = _outdent_section(parent.children, target_id)
        if changed:
            items[parent_index] = replace(parent, children=nested)
            return tuple(items), True
    return sections, False
