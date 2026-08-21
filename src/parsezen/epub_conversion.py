"""Conservative EPUB package reading built around MarkItDown's HTML converter."""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterator
from copy import copy, deepcopy
from dataclasses import dataclass
from difflib import SequenceMatcher
from io import BytesIO
from pathlib import Path, PurePosixPath
from tempfile import SpooledTemporaryFile
from typing import cast
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree as XmlElementTree
from zipfile import ZIP_STORED, BadZipFile, ZipFile, ZipInfo, is_zipfile

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from parsezen.cancellation import CancellationToken, check_cancelled
from parsezen.document_model import (
    RESOURCE_REFERENCE_PREFIX,
    ConvertedDocument,
    ConvertedResource,
)
from parsezen.errors import ConversionError
from parsezen.translation_quality import (
    NUMBER_PATTERN,
    RAW_URL_PATTERN,
    TITLE_ROMAN_REFERENCE_PATTERN,
    TranslationQualityError,
    TranslationQualityReport,
    build_aligned_translation_quality_report,
    detect_language_code,
    is_literal_work_title_translation,
    is_reference_or_catalogue_heading,
    numeric_tokens_are_conserved,
    validate_translation_content_coverage,
)

LOGGER = logging.getLogger(__name__)

_EPUB_MIMETYPE = b"application/epub+zip"
_CONTAINER_PATH = "META-INF/container.xml"
_OPF_NAMESPACE = "http://www.idpf.org/2007/opf"
_HTML_MEDIA_TYPES = frozenset({"application/xhtml+xml", "text/html"})
_NAVIGATION_MEDIA_TYPES = frozenset({"application/x-dtbncx+xml"})
_MAX_ARCHIVE_ENTRIES = 10_000
_MAX_ARCHIVE_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
_MAX_ARCHIVE_MEMBER_BYTES = 128 * 1024 * 1024
_MAX_ARCHIVE_COMPRESSION_RATIO = 1_000
_MAX_XML_BYTES = 10 * 1024 * 1024
_MAX_RESOURCE_BYTES = 100 * 1024 * 1024
_SPACE_PATTERN = re.compile(r"\s+")
_SENTENCE_END_PATTERN = re.compile(r"[.!?:;…][\"'’”»)]*$")
_LINK_LEAD_INS = frozenset({"available at", "disponible en", "see", "véase", "visit", "visita"})
_TRANSLATABLE_BLOCKS = frozenset(
    {
        "blockquote",
        "caption",
        "dd",
        "dt",
        "figcaption",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "p",
        "td",
        "th",
        "title",
    }
)
_FALLBACK_TRANSLATABLE_ELEMENTS = frozenset(
    {"a", "article", "body", "div", "em", "section", "small", "span", "strong"}
)
_UNSAFE_TRANSLATION_CONTENT = frozenset({"code", "math", "pre", "script", "style", "svg"})
_TRANSLATABLE_ATTRIBUTES = frozenset({"alt", "aria-label", "title"})
_TRANSLATABLE_METADATA = frozenset({"description", "subject", "title"})
_EPUB_TRANSLATION_MARKER_PREFIX = "PZDOC EPUB TRANSLATION UNIT"
_EPUB_XML_MARKER_PREFIX = "PZDOC EPUB XML"
_XML_TAG_PATTERN = re.compile(r"(?:<[^>]+>)+")
_BARE_XML_TEXT_AMPERSAND_PATTERN = re.compile(
    r"&(?!amp;|lt;|gt;|apos;|quot;|#\d+;|#x[0-9A-Fa-f]+;)"
)
_MAX_EPUB_TRANSLATION_PART_CHARACTERS = 8_000
_MIN_EPUB_TRANSLATION_PART_CHARACTERS = 1_000
_MIN_SOURCE_RESIDUAL_RETRY_SIMILARITY = 0.92
_XML_LANGUAGE_ATTRIBUTE = "{http://www.w3.org/XML/1998/namespace}lang"
_STANDARD_FONT_OBFUSCATION_ALGORITHMS = frozenset(
    {
        "http://www.idpf.org/2008/embedding",
        "http://ns.adobe.com/pdf/enc#RC",
    }
)
_FONT_MEDIA_TYPES = frozenset(
    {
        "application/font-sfnt",
        "application/vnd.ms-opentype",
        "font/otf",
        "font/ttf",
        "font/woff",
        "font/woff2",
    }
)
_XML_DECLARATION_ENCODING_PATTERN = re.compile(
    rb"<\?xml[^>]*\bencoding=[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _ManifestItem:
    item_id: str
    path: str
    media_type: str
    properties: frozenset[str]


@dataclass(frozen=True, slots=True)
class _NavigationEntry:
    label: str
    path: str
    fragment: str
    depth: int


@dataclass(frozen=True, slots=True)
class _Metadata:
    title: str | None
    authors: tuple[str, ...]
    language: str | None
    publisher: str | None
    date: str | None
    identifiers: tuple[str, ...]
    cover_item_id: str | None

    @property
    def identifier(self) -> str | None:
        """Return the primary identifier while retaining every declared value."""

        return self.identifiers[0] if self.identifiers else None


@dataclass(slots=True)
class _ContentDocument:
    path: str
    root: XmlElementTree.Element


@dataclass(frozen=True, slots=True)
class TranslatedEpub:
    """Validated EPUB bytes plus small diagnostics for the UI and tests."""

    content: bytes
    language_code: str
    translated_units: int
    preserved_binary_resources: int
    translation_parts: int
    resumed_parts: int
    checkpoint_degraded: bool
    quality_report: TranslationQualityReport


@dataclass(frozen=True, slots=True)
class EpubPackageMetadata:
    """Small, format-independent metadata needed by the normalized editor."""

    title: str | None
    authors: tuple[str, ...]
    language: str | None
    identifier: str | None
    cover_path: PurePosixPath | None
    identifiers: tuple[str, ...] = ()
    publisher: str | None = None
    publication_date: str | None = None


@dataclass(frozen=True, slots=True)
class EpubEditableChapter:
    """One real spine document retained for package-preserving local editing."""

    archive_path: str
    title: str
    xhtml: bytes


@dataclass(frozen=True, slots=True)
class EpubEditablePackage:
    """Bounded editable view plus the exact source package bytes."""

    content: bytes
    metadata: EpubPackageMetadata
    chapters: tuple[EpubEditableChapter, ...]


@dataclass(frozen=True, slots=True)
class _EpubTranslationUnit:
    archive_path: str
    element_path: tuple[int, ...]
    source: str
    attribute: str | None = None
    text_slot: str | None = None


@dataclass(frozen=True, slots=True)
class _EpubTranslationPart:
    key: str
    indexed_units: tuple[tuple[int, _EpubTranslationUnit], ...]


EpubTranslationProgress = Callable[[int, int], None]
EpubCheckpointLoader = Callable[[str], str | None]
EpubCheckpointSaver = Callable[[str, str], bool]
EpubTextTranslator = Callable[
    [str, EpubTranslationProgress | None, CancellationToken | None],
    str,
]
EpubTextRepairer = Callable[[str, str, str | None, CancellationToken | None], str]


def convert_epub(
    source_path: Path,
    *,
    cancellation: CancellationToken | None = None,
) -> ConvertedDocument:
    """Convert one local EPUB while retaining its reading order and images."""

    check_cancelled(cancellation)
    _validate_epub_container(source_path)
    try:
        with ZipFile(source_path) as archive:
            members = _validate_archive_limits(archive, source_path.name)
            opf_path = _read_rootfile_path(archive, members, source_path.name)
            opf_root = _parse_xml_member(
                archive,
                members,
                opf_path,
                "el paquete EPUB",
            )
            manifest = _read_manifest(opf_root, opf_path)
            spine_ids, toc_id = _read_spine(opf_root)
            metadata = _read_metadata(opf_root)
            documents = _read_spine_documents(
                archive,
                members,
                manifest,
                spine_ids,
                source_path.name,
                cancellation,
            )
            navigation = _read_navigation(
                archive,
                members,
                manifest,
                toc_id,
                documents,
            )
            markdown, referenced_resources = _convert_documents(
                documents,
                navigation,
                manifest,
                metadata,
                cancellation,
            )
            resources = _read_resources(
                archive,
                members,
                manifest,
                referenced_resources,
                cancellation,
            )
    except (BadZipFile, OSError) as exc:
        raise ConversionError(f"No se pudo abrir el EPUB {source_path.name}.") from exc

    if not markdown.strip():
        raise ConversionError(f"{source_path.name} no contiene texto EPUB convertible.")
    return ConvertedDocument(markdown=markdown, resources=resources)


def inspect_epub_package(source_path: Path) -> EpubPackageMetadata:
    """Read safe publication metadata without exposing parser internals."""

    _validate_epub_container(source_path)
    try:
        with ZipFile(source_path) as archive:
            members = _validate_archive_limits(archive, source_path.name)
            opf_path = _read_rootfile_path(archive, members, source_path.name)
            opf_root = _parse_xml_member(
                archive,
                members,
                opf_path,
                "el paquete EPUB",
            )
            manifest = _read_manifest(opf_root, opf_path)
            metadata = _read_metadata(opf_root)
            cover = _cover_manifest_item(metadata, manifest)
    except (BadZipFile, OSError) as exc:
        raise ConversionError(f"No se pudo abrir el EPUB {source_path.name}.") from exc
    return EpubPackageMetadata(
        title=metadata.title,
        authors=metadata.authors,
        language=metadata.language,
        identifier=metadata.identifier,
        cover_path=PurePosixPath(cover.path) if cover is not None else None,
        identifiers=metadata.identifiers,
        publisher=metadata.publisher,
        publication_date=metadata.date,
    )


def read_editable_epub_package(
    source_path: Path,
    *,
    cancellation: CancellationToken | None = None,
) -> EpubEditablePackage:
    """Read exact package bytes and one bounded editable record per real spine document."""

    check_cancelled(cancellation)
    _validate_epub_container(source_path)
    try:
        content = source_path.read_bytes()
        with ZipFile(BytesIO(content)) as archive:
            members = _validate_archive_limits(archive, source_path.name)
            opf_path = _read_rootfile_path(archive, members, source_path.name)
            opf_root = _parse_xml_member(archive, members, opf_path, "el paquete EPUB")
            manifest = _read_manifest(opf_root, opf_path)
            _validate_epub_encryption(archive, members, manifest)
            spine_ids, toc_id = _read_spine(opf_root)
            documents = _read_spine_documents(
                archive,
                members,
                manifest,
                spine_ids,
                source_path.name,
                cancellation,
            )
            navigation = _read_navigation(
                archive,
                members,
                manifest,
                toc_id,
                documents,
            )
            labels = {
                entry.path: entry.label
                for entry in navigation
                if entry.path and entry.label and not entry.fragment
            }
            chapters = tuple(
                EpubEditableChapter(
                    document.path,
                    labels.get(document.path)
                    or _document_title(document.root)
                    or PurePosixPath(document.path).stem,
                    archive.read(members[document.path]),
                )
                for document in documents
            )
            metadata = _read_metadata(opf_root)
            cover = _cover_manifest_item(metadata, manifest)
    except (BadZipFile, OSError) as exc:
        raise ConversionError(f"No se pudo abrir el EPUB {source_path.name}.") from exc
    return EpubEditablePackage(
        content=content,
        metadata=EpubPackageMetadata(
            title=metadata.title,
            authors=metadata.authors,
            language=metadata.language,
            identifier=metadata.identifier,
            cover_path=PurePosixPath(cover.path) if cover is not None else None,
            identifiers=metadata.identifiers,
            publisher=metadata.publisher,
            publication_date=metadata.date,
        ),
        chapters=chapters,
    )


def patch_epub_xhtml_package(
    content: bytes,
    replacements: dict[str, bytes],
    *,
    cancellation: CancellationToken | None = None,
) -> bytes:
    """Replace edited spine bodies while preserving every other package member byte-for-byte."""

    if not replacements:
        return content
    check_cancelled(cancellation)
    try:
        with ZipFile(BytesIO(content)) as archive:
            members = _validate_archive_limits(archive, "el EPUB editable")
            opf_path = _read_rootfile_path(archive, members, "el EPUB editable")
            opf_root = _parse_xml_member(archive, members, opf_path, "el paquete EPUB")
            manifest = _read_manifest(opf_root, opf_path)
            spine_ids, _toc_id = _read_spine(opf_root)
            editable_paths = {
                manifest[item_id].path
                for item_id in spine_ids
                if item_id in manifest and _is_html_item(manifest[item_id])
            }
            normalized_replacements = {
                _normalize_archive_path(path): payload for path, payload in replacements.items()
            }
            if (
                len(normalized_replacements) != len(replacements)
                or not normalized_replacements.keys() <= editable_paths
            ):
                raise ConversionError("La edición no coincide con capítulos reales del EPUB.")
            merged = {
                path: _merge_edited_epub_body(
                    archive.read(members[path]),
                    payload,
                )
                for path, payload in normalized_replacements.items()
            }
            rebuilt, _preserved = _rebuild_epub(
                archive,
                members,
                merged,
                cancellation,
            )
    except (BadZipFile, OSError) as exc:
        raise ConversionError("No se pudo aplicar la edición al paquete EPUB.") from exc
    return rebuilt


def _merge_edited_epub_body(original: bytes, edited: bytes) -> bytes:
    try:
        original_root = _parse_xml_payload(original)
        edited_root = _parse_xml_payload(edited)
    except (XmlElementTree.ParseError, DefusedXmlException) as exc:
        raise ConversionError("La edición produjo XHTML no válido.") from exc
    original_body = next(
        (element for element in original_root.iter() if _local_name(element) == "body"),
        None,
    )
    edited_body = next(
        (element for element in edited_root.iter() if _local_name(element) == "body"),
        None,
    )
    if original_body is None or edited_body is None:
        raise ConversionError("La edición no contiene un cuerpo XHTML válido.")
    original_body.text = edited_body.text
    for child in tuple(original_body):
        original_body.remove(child)
    for child in edited_body:
        original_body.append(deepcopy(child))
    return _serialize_xml_document(original_root, original)


def replace_epub_metadata(
    content: bytes,
    *,
    title: str | None = None,
    author: str | None = None,
    cancellation: CancellationToken | None = None,
) -> bytes:
    """Replace publication metadata without normalizing the original EPUB package."""

    if title is None and author is None:
        return content
    check_cancelled(cancellation)
    try:
        with ZipFile(BytesIO(content)) as archive:
            members = _validate_archive_limits(archive, "el EPUB transformado")
            opf_path = _read_rootfile_path(
                archive,
                members,
                "el EPUB transformado",
            )
            opf_root = _parse_xml_member(
                archive,
                members,
                opf_path,
                "el paquete EPUB",
            )
            original_payload = archive.read(members[opf_path])
            _replace_publication_metadata(opf_root, title=title, author=author)
            replacement = _serialize_xml_document(opf_root, original_payload)
            rebuilt, _binary_resources = _rebuild_epub(
                archive,
                members,
                {opf_path: replacement},
                cancellation,
            )
    except (BadZipFile, OSError) as exc:
        raise ConversionError("No se pudieron actualizar los metadatos del EPUB.") from exc
    return rebuilt


def _replace_publication_metadata(
    opf_root: XmlElementTree.Element,
    *,
    title: str | None,
    author: str | None,
) -> None:
    metadata = next(
        (element for element in opf_root.iter() if _local_name(element) == "metadata"),
        None,
    )
    if metadata is None:
        raise ConversionError("El EPUB no contiene metadatos editables.")
    if title is not None and title.strip():
        _replace_dc_value(metadata, "title", title.strip())
    if author is not None:
        _replace_dc_value(metadata, "creator", author.strip())


def _replace_dc_value(
    metadata: XmlElementTree.Element,
    local_name: str,
    value: str,
) -> None:
    matching = [child for child in list(metadata) if _local_name(child) == local_name]
    removed_ids: set[str] = set()
    if matching and value:
        matching[0].text = value
        discarded = matching[1:]
    else:
        discarded = matching
        if value:
            element = XmlElementTree.Element(f"{{http://purl.org/dc/elements/1.1/}}{local_name}")
            element.text = value
            metadata.append(element)
    for element in discarded:
        element_id = element.attrib.get("id", "").strip()
        if element_id:
            removed_ids.add(element_id)
        metadata.remove(element)
    if not removed_ids:
        return
    for element in list(metadata):
        refined_id = element.attrib.get("refines", "").strip().removeprefix("#")
        if _local_name(element) == "meta" and refined_id in removed_ids:
            metadata.remove(element)


def translate_epub(
    source_path: Path,
    target_language_code: str,
    translate_text: EpubTextTranslator,
    *,
    on_progress: EpubTranslationProgress | None = None,
    cancellation: CancellationToken | None = None,
    load_checkpoint: EpubCheckpointLoader | None = None,
    save_checkpoint: EpubCheckpointSaver | None = None,
    repair_text: EpubTextRepairer | None = None,
) -> TranslatedEpub:
    """Translate visible EPUB text while retaining the original package and resources."""

    check_cancelled(cancellation)
    if not re.fullmatch(r"[a-z]{2,3}(?:-[A-Z]{2})?", target_language_code):
        raise ConversionError("El idioma de destino del EPUB no es válido.")
    _validate_epub_container(source_path)
    try:
        with ZipFile(source_path) as archive:
            members = _validate_archive_limits(archive, source_path.name)
            opf_path = _read_rootfile_path(archive, members, source_path.name)
            opf_root = _parse_xml_member(archive, members, opf_path, "el paquete EPUB")
            manifest = _read_manifest(opf_root, opf_path)
            _validate_epub_encryption(archive, members, manifest)
            roots = _translation_roots(
                archive,
                members,
                manifest,
                opf_path,
                opf_root,
            )
            original_signatures = {
                path: _document_structure_signature(root) for path, root in roots.items()
            }
            units = _collect_epub_translation_units(roots, manifest, opf_path)
            source_language_code = _epub_source_language_code(opf_root, units)
            (
                translated_values,
                translation_parts,
                resumed_parts,
                checkpoint_degraded,
            ) = _translate_epub_units(
                units,
                translate_text,
                on_progress,
                cancellation,
                load_checkpoint,
                save_checkpoint,
                repair_text,
                source_language_code,
            )
            quality_report = build_aligned_translation_quality_report(
                (unit.source for unit in units),
                translated_values,
                source_language=source_language_code,
                target_language=target_language_code,
                literal_work_title_segments=_literal_epub_work_title_segments(
                    units,
                    translated_values,
                    source_language=source_language_code,
                    target_language=target_language_code,
                ),
            )
            changed_paths = _apply_epub_translations(roots, units, translated_values)
            _validate_translated_roots(roots, original_signatures)
            changed_paths.update(
                _set_epub_language(roots, manifest, opf_path, target_language_code)
            )
            original_payloads = {path: archive.read(members[path]) for path in changed_paths}
            replacements = {
                path: _serialize_xml_document(roots[path], original_payloads[path])
                for path in changed_paths
            }
            content, binary_resources = _rebuild_epub(
                archive,
                members,
                replacements,
                cancellation,
            )
    except (BadZipFile, OSError) as exc:
        raise ConversionError(f"No se pudo abrir el EPUB {source_path.name}.") from exc

    return TranslatedEpub(
        content=content,
        language_code=target_language_code,
        translated_units=len(units),
        preserved_binary_resources=binary_resources,
        translation_parts=translation_parts,
        resumed_parts=resumed_parts,
        checkpoint_degraded=checkpoint_degraded,
        quality_report=quality_report,
    )


def _translation_roots(
    archive: ZipFile,
    members: dict[str, ZipInfo],
    manifest: dict[str, _ManifestItem],
    opf_path: str,
    opf_root: XmlElementTree.Element,
) -> dict[str, XmlElementTree.Element]:
    roots = {opf_path: opf_root}
    for item in manifest.values():
        if item.path in roots or item.path not in members:
            continue
        if _is_html_item(item) or item.media_type in _NAVIGATION_MEDIA_TYPES:
            roots[item.path] = _parse_xml_member(
                archive,
                members,
                item.path,
                "un documento traducible del EPUB",
            )
    return roots


def _collect_epub_translation_units(
    roots: dict[str, XmlElementTree.Element],
    manifest: dict[str, _ManifestItem],
    opf_path: str,
) -> list[_EpubTranslationUnit]:
    media_types = {item.path: item.media_type for item in manifest.values()}
    units: list[_EpubTranslationUnit] = []
    for archive_path, root in roots.items():
        if archive_path == opf_path:
            allowed_elements = _TRANSLATABLE_METADATA
        elif media_types.get(archive_path) in _NAVIGATION_MEDIA_TYPES:
            allowed_elements = frozenset({"text"})
        else:
            allowed_elements = _TRANSLATABLE_BLOCKS
        translate_all_safe_text = (
            archive_path != opf_path
            and media_types.get(archive_path) not in _NAVIGATION_MEDIA_TYPES
        )
        for element_path, element, text_slot in _select_translation_elements(
            root,
            allowed_elements,
            translate_all_safe_text=translate_all_safe_text,
        ):
            source = (
                _translation_text_wrapper(
                    element.text if text_slot == "text" else element.tail or ""
                )
                if text_slot is not None
                else _serialize_xml_element(element)
            )
            units.append(
                _EpubTranslationUnit(
                    archive_path,
                    element_path,
                    source,
                    text_slot=text_slot,
                )
            )
        if archive_path == opf_path or media_types.get(archive_path) in _NAVIGATION_MEDIA_TYPES:
            continue
        for element_path, element in _walk_elements(root):
            for attribute, value in element.attrib.items():
                if attribute.rsplit("}", 1)[-1] in _TRANSLATABLE_ATTRIBUTES and any(
                    character.isalpha() for character in value
                ):
                    wrapper = XmlElementTree.Element("span")
                    wrapper.text = value
                    units.append(
                        _EpubTranslationUnit(
                            archive_path,
                            element_path,
                            XmlElementTree.tostring(wrapper, encoding="unicode"),
                            attribute=attribute,
                        )
                    )
    return units


def _literal_epub_work_title_segments(
    units: list[_EpubTranslationUnit],
    translated_values: list[str],
    *,
    source_language: str | None,
    target_language: str,
) -> frozenset[int]:
    """Identify fully emphasized bibliographic titles without hiding ordinary prose."""

    if source_language is None or source_language == target_language:
        return frozenset()
    reference_level: dict[str, int] = {}
    segments: set[int] = set()
    for segment_number, (unit, translated) in enumerate(
        zip(units, translated_values, strict=True),
        start=1,
    ):
        if unit.attribute is None and (heading_level := _epub_unit_heading_level(unit.source)):
            if is_reference_or_catalogue_heading(unit.source):
                reference_level[unit.archive_path] = heading_level
            elif heading_level <= reference_level.get(unit.archive_path, 0):
                reference_level.pop(unit.archive_path, None)
            continue
        if (
            unit.attribute is not None
            or unit.archive_path not in reference_level
            or not _epub_unit_is_fully_emphasized(unit.source)
        ):
            continue
        if is_literal_work_title_translation(
            unit.source,
            translated,
            source_language=source_language,
            target_language=target_language,
        ):
            segments.add(segment_number)
    return frozenset(segments)


def _epub_unit_heading_level(value: str) -> int | None:
    try:
        root = ElementTree.fromstring(value)
    except (DefusedXmlException, XmlElementTree.ParseError):
        return None
    name = _local_name(root).casefold()
    return int(name[1]) if re.fullmatch(r"h[1-6]", name) else None


def _epub_unit_is_fully_emphasized(value: str) -> bool:
    try:
        root = ElementTree.fromstring(value)
    except (DefusedXmlException, XmlElementTree.ParseError):
        return False
    children = list(root)
    if (
        _local_name(root).casefold() != "p"
        or len(children) != 1
        or _local_name(children[0]).casefold() not in {"em", "i"}
    ):
        return False
    return not (root.text or "").strip() and not (children[0].tail or "").strip()


def _select_translation_elements(
    root: XmlElementTree.Element,
    allowed_elements: frozenset[str],
    *,
    translate_all_safe_text: bool = False,
) -> Iterator[tuple[tuple[int, ...], XmlElementTree.Element, str | None]]:
    def visit(
        element: XmlElementTree.Element,
        path: tuple[int, ...],
        *,
        translate_direct_text: bool,
    ) -> Iterator[tuple[tuple[int, ...], XmlElementTree.Element, str | None]]:
        if not isinstance(element.tag, str):
            return
        name = _local_name(element).casefold()
        if name in _UNSAFE_TRANSLATION_CONTENT:
            return
        contains_unsafe_content = any(
            _local_name(descendant).casefold() in _UNSAFE_TRANSLATION_CONTENT
            for descendant in element.iter()
        )
        is_translation_container = name in allowed_elements or _is_fallback_translation_element(
            element,
            name,
        )
        contains_letters = any(character.isalpha() for character in _element_text(element))
        if is_translation_container and contains_letters and not contains_unsafe_content:
            yield path, element, None
            return
        translate_descendant_text = translate_direct_text or (
            is_translation_container and contains_letters
        )
        if translate_descendant_text and _contains_alphabetic_text(element.text):
            yield path, element, "text"
        for index, child in enumerate(element):
            child_path = (*path, index)
            yield from visit(
                child,
                child_path,
                translate_direct_text=translate_descendant_text,
            )
            if translate_descendant_text and _contains_alphabetic_text(child.tail):
                yield child_path, child, "tail"

    yield from visit(root, (), translate_direct_text=translate_all_safe_text)


def _contains_alphabetic_text(value: str | None) -> bool:
    return bool(value) and any(character.isalpha() for character in value)


def _translation_text_wrapper(value: str) -> str:
    wrapper = XmlElementTree.Element("span")
    wrapper.text = value
    return XmlElementTree.tostring(wrapper, encoding="unicode")


def _is_fallback_translation_element(
    element: XmlElementTree.Element,
    name: str,
) -> bool:
    if name not in _FALLBACK_TRANSLATABLE_ELEMENTS:
        return False
    direct_text = [element.text or "", *(child.tail or "" for child in element)]
    return any(character.isalpha() for character in " ".join(direct_text))


def _walk_elements(
    root: XmlElementTree.Element,
) -> Iterator[tuple[tuple[int, ...], XmlElementTree.Element]]:
    def visit(
        element: XmlElementTree.Element,
        path: tuple[int, ...],
    ) -> Iterator[tuple[tuple[int, ...], XmlElementTree.Element]]:
        yield path, element
        for index, child in enumerate(element):
            yield from visit(child, (*path, index))

    yield from visit(root, ())


def _translate_epub_units(
    units: list[_EpubTranslationUnit],
    translate_text: EpubTextTranslator,
    on_progress: EpubTranslationProgress | None,
    cancellation: CancellationToken | None,
    load_checkpoint: EpubCheckpointLoader | None,
    save_checkpoint: EpubCheckpointSaver | None,
    repair_text: EpubTextRepairer | None,
    source_language_code: str | None,
) -> tuple[list[str], int, int, bool]:
    if not units:
        raise ConversionError("El EPUB no contiene texto que se pueda traducir con seguridad.")
    parts = _plan_epub_translation_parts(units)
    translated_values: list[str | None] = [None] * len(units)
    resumed_parts = 0
    checkpoint_degraded = False
    for part_index, part in enumerate(parts, start=1):
        check_cancelled(cancellation)
        if on_progress is not None:
            on_progress(part_index, len(parts))
        translated_payload = load_checkpoint(part.key) if load_checkpoint is not None else None
        extracted: dict[int, str] | None = None
        checkpoint_needs_save = False
        retried_indexes: set[int] = set()
        source_residual_retry_indexes: set[int] = set()
        if translated_payload is not None:
            try:
                extracted = _validate_translated_part(part, translated_payload)
            except ConversionError:
                translated_payload = None
            else:
                resumed_parts += 1
        if translated_payload is None:
            model_payload, protected_markup = _translation_model_part_payload(part.indexed_units)
            translated_model_payload = translate_text(model_payload, None, cancellation)
            if (
                not isinstance(translated_model_payload, str)
                or not translated_model_payload.strip()
            ):
                raise ConversionError("El traductor devolvió una parte EPUB vacía.")
            translated_payload = ""
            try:
                translated_payload = _restore_translated_part_markup(
                    part,
                    translated_model_payload,
                    protected_markup,
                )
                extracted = _validate_translated_part(part, translated_payload)
            except ConversionError:
                translated_payload, forced_retries = _partially_restore_translated_part_markup(
                    part,
                    translated_model_payload,
                    protected_markup,
                )
                translated_payload, extracted, invalid_retries = _retry_invalid_translated_units(
                    part,
                    translated_payload,
                    translate_text,
                    cancellation,
                    forced_indexes=forced_retries,
                )
                retried_indexes.update(invalid_retries)
            if _translated_part_remains_source_language(
                part,
                extracted,
                source_language_code,
            ):
                residual_indexes = {index for index, _unit in part.indexed_units} - retried_indexes
                if not residual_indexes:
                    LOGGER.warning(
                        "epub_translation_source_language_residual phase=retry_exhausted "
                        "part=%d total=%d units=%d retried=%d",
                        part_index,
                        len(parts),
                        len(part.indexed_units),
                        len(retried_indexes),
                    )
                    raise ConversionError(
                        "El traductor conservó una parte EPUB completa en el idioma de origen."
                    )
                translated_payload, extracted, residual_retries = _retry_invalid_translated_units(
                    part,
                    translated_payload,
                    translate_text,
                    cancellation,
                    forced_indexes=residual_indexes,
                )
                retried_indexes.update(residual_retries)
                source_residual_retry_indexes.update(residual_retries)
                if _translated_part_remains_source_language(
                    part,
                    extracted,
                    source_language_code,
                ):
                    LOGGER.warning(
                        "epub_translation_source_language_residual phase=after_retry "
                        "part=%d total=%d units=%d retried=%d",
                        part_index,
                        len(parts),
                        len(part.indexed_units),
                        len(retried_indexes),
                    )
                    raise ConversionError(
                        "El traductor conservó una parte EPUB completa en el idioma de origen."
                    )
            checkpoint_needs_save = True
        if extracted is None:
            raise AssertionError("A validated EPUB part always has extracted units.")
        residual_unit_indexes = {
            index
            for index, unit in part.indexed_units
            if index not in retried_indexes
            and _translated_unit_remains_source_language(
                unit,
                extracted[index],
                source_language_code,
            )
        }
        if residual_unit_indexes:
            LOGGER.info(
                "epub_translation_unit_source_residual_retry part=%d total=%d units=%d",
                part_index,
                len(parts),
                len(residual_unit_indexes),
            )
            translated_payload, extracted, unit_retries = _retry_invalid_translated_units(
                part,
                translated_payload,
                translate_text,
                cancellation,
                forced_indexes=residual_unit_indexes,
            )
            retried_indexes.update(unit_retries)
            source_residual_retry_indexes.update(unit_retries)
            checkpoint_needs_save = True
        if repair_text is not None:
            repaired_values: dict[int, str] = {}
            for index, unit in part.indexed_units:
                if index in source_residual_retry_indexes:
                    repaired_values[index] = extracted[index]
                    continue
                protected_source, _source_markup = _protect_epub_xml_markup(
                    unit.source,
                    index,
                )
                protected_translated, translated_markup = _protect_epub_xml_markup(
                    extracted[index],
                    index,
                )
                repaired = repair_text(
                    protected_source,
                    protected_translated,
                    source_language_code,
                    cancellation,
                )
                if not isinstance(repaired, str) or not repaired.strip():
                    raise ConversionError("La reparación dejó vacío un fragmento EPUB.")
                repaired = _restore_epub_xml_markup(repaired, translated_markup)
                _parse_epub_translation(unit, repaired)
                repaired_values[index] = repaired
            if _translated_part_remains_source_language(
                part,
                repaired_values,
                source_language_code,
            ):
                LOGGER.warning(
                    "epub_translation_source_language_residual phase=after_repair "
                    "part=%d total=%d units=%d",
                    part_index,
                    len(parts),
                    len(part.indexed_units),
                )
                raise ConversionError(
                    "La reparación conservó una parte EPUB completa en el idioma de origen."
                )
            if repaired_values != extracted:
                extracted = repaired_values
                translated_payload = _translation_part_payload_from_values(
                    part.indexed_units,
                    extracted,
                )
                checkpoint_needs_save = True
        if checkpoint_needs_save and save_checkpoint is not None:
            if not save_checkpoint(part.key, translated_payload):
                checkpoint_degraded = True
        for index, translated in extracted.items():
            translated_values[index] = translated

    if any(value is None for value in translated_values):
        raise ConversionError("Faltan fragmentos al reconstruir el EPUB traducido.")
    return (
        [value for value in translated_values if value is not None],
        len(parts),
        resumed_parts,
        checkpoint_degraded,
    )


def _epub_source_language_code(
    opf_root: XmlElementTree.Element,
    units: list[_EpubTranslationUnit],
) -> str | None:
    detected = detect_language_code(
        "\n\n".join(unit.source for unit in units),
        minimum_letters=80,
    )
    declared = _read_metadata(opf_root).language
    if declared is not None:
        match = re.match(r"^[A-Za-z]{2,3}", declared.strip())
        if match is not None:
            declared_code = match.group(0).casefold()
            return detected or declared_code
    return detected


def _plan_epub_translation_parts(
    units: list[_EpubTranslationUnit],
) -> tuple[_EpubTranslationPart, ...]:
    """Group semantic text units without relying on EPUB chapter metadata."""

    batches: list[list[tuple[int, _EpubTranslationUnit]]] = []
    pending: list[tuple[int, _EpubTranslationUnit]] = []
    pending_length = 0
    for index, unit in enumerate(units):
        addition = len(unit.source) + 80
        if (
            pending
            and pending_length >= _MIN_EPUB_TRANSLATION_PART_CHARACTERS
            and pending_length + addition > _MAX_EPUB_TRANSLATION_PART_CHARACTERS
        ):
            batches.append(pending)
            pending = []
            pending_length = 0
        pending.append((index, unit))
        pending_length += addition
    if pending:
        batches.append(pending)
    if len(batches) > 1 and _indexed_units_length(batches[-1]) < (
        _MIN_EPUB_TRANSLATION_PART_CHARACTERS
    ):
        batches[-2].extend(batches.pop())
    return tuple(
        _EpubTranslationPart(
            key=hashlib.sha256(_translation_part_payload(tuple(batch)).encode("utf-8")).hexdigest(),
            indexed_units=tuple(batch),
        )
        for batch in batches
    )


def _indexed_units_length(indexed_units: list[tuple[int, _EpubTranslationUnit]]) -> int:
    return sum(len(unit.source) + 80 for _index, unit in indexed_units)


def _translation_part_payload(
    indexed_units: tuple[tuple[int, _EpubTranslationUnit], ...],
) -> str:
    return "\n\n".join(
        f"{_translation_marker(index)}\n{unit.source}" for index, unit in indexed_units
    )


def _translation_part_payload_from_values(
    indexed_units: tuple[tuple[int, _EpubTranslationUnit], ...],
    translated_values: dict[int, str],
) -> str:
    return "\n\n".join(
        f"{_translation_marker(index)}\n{translated_values[index]}"
        for index, _unit in indexed_units
    )


def _translation_model_part_payload(
    indexed_units: tuple[tuple[int, _EpubTranslationUnit], ...],
    *,
    retry: bool = False,
) -> tuple[str, dict[int, tuple[tuple[str, str], ...]]]:
    protected_markup: dict[int, tuple[tuple[str, str], ...]] = {}
    payloads: list[str] = []
    for index, unit in indexed_units:
        protected, markup = _protect_epub_xml_markup(unit.source, index, retry=retry)
        protected_markup[index] = markup
        payloads.append(f"{_translation_marker(index)}\n{protected}")
    return "\n\n".join(payloads), protected_markup


def _protect_epub_xml_markup(
    value: str,
    unit_index: int,
    *,
    retry: bool = False,
) -> tuple[str, tuple[tuple[str, str], ...]]:
    prefix = f"{_EPUB_XML_MARKER_PREFIX}{' RETRY' if retry else ''}"
    while prefix in value:
        prefix = f"Z{prefix}"
    spans = [
        (match.start(), match.end())
        for pattern in (
            _XML_TAG_PATTERN,
            RAW_URL_PATTERN,
            NUMBER_PATTERN,
            TITLE_ROMAN_REFERENCE_PATTERN,
        )
        for match in pattern.finditer(value)
    ]
    non_overlapping: list[tuple[int, int]] = []
    for start, end in sorted(spans, key=lambda span: (span[0], -(span[1] - span[0]))):
        if non_overlapping and start < non_overlapping[-1][1]:
            continue
        non_overlapping.append((start, end))

    protected_values: list[tuple[str, str]] = []
    for protected_index, (start, end) in enumerate(non_overlapping):
        marker = (
            f"<!-- {prefix.replace(' ', '_')}_{_alphabetic_index(unit_index)}_"
            f"{_alphabetic_index(protected_index)}_XZQ -->"
        )
        protected_values.append((marker, value[start:end]))

    protected = value
    for (start, end), (marker, _original) in reversed(
        list(zip(non_overlapping, protected_values, strict=True))
    ):
        protected = f"{protected[:start]}{marker}{protected[end:]}"
    return protected, tuple(protected_values)


def _restore_epub_xml_markup(
    value: str,
    protected_markup: tuple[tuple[str, str], ...],
) -> str:
    positions: list[int] = []
    for marker, _original in protected_markup:
        if value.count(marker) != 1:
            raise ConversionError("El traductor cambió el marcado protegido del EPUB.")
        positions.append(value.index(marker))
    if positions != sorted(positions):
        raise ConversionError("El traductor cambió el orden del marcado protegido del EPUB.")
    first_marker = protected_markup[0][0]
    last_marker = protected_markup[-1][0]
    envelope_start = value.index(first_marker)
    envelope_end = value.index(last_marker) + len(last_marker)
    if value[:envelope_start].strip() or value[envelope_end:].strip():
        LOGGER.info("epub_translation_external_envelope_removed")
    value = value[envelope_start:envelope_end]
    restored = _escape_epub_model_text(value, protected_markup)
    for marker, original in protected_markup:
        restored = restored.replace(marker, original)
    return restored


def _escape_epub_model_text(
    value: str,
    protected_markup: tuple[tuple[str, str], ...],
) -> str:
    pieces: list[str] = []
    cursor = 0
    for marker, _original in protected_markup:
        marker_start = value.index(marker, cursor)
        visible = value[cursor:marker_start]
        visible = _BARE_XML_TEXT_AMPERSAND_PATTERN.sub("&amp;", visible)
        visible = visible.replace("<", "&lt;").replace(">", "&gt;")
        pieces.extend((visible, marker))
        cursor = marker_start + len(marker)
    visible = value[cursor:]
    visible = _BARE_XML_TEXT_AMPERSAND_PATTERN.sub("&amp;", visible)
    pieces.append(visible.replace("<", "&lt;").replace(">", "&gt;"))
    return "".join(pieces)


def _restore_translated_part_markup(
    part: _EpubTranslationPart,
    translated_payload: str,
    protected_markup: dict[int, tuple[tuple[str, str], ...]],
) -> str:
    indexes = [index for index, _unit in part.indexed_units]
    extracted = _extract_translated_units(translated_payload, indexes)
    restored = {
        index: _restore_epub_xml_markup(extracted[index], protected_markup[index])
        for index in indexes
    }
    return _translation_part_payload_from_values(part.indexed_units, restored)


def _partially_restore_translated_part_markup(
    part: _EpubTranslationPart,
    translated_payload: str,
    protected_markup: dict[int, tuple[tuple[str, str], ...]],
) -> tuple[str, set[int]]:
    """Keep valid units from a rejected group so only damaged units are retried."""

    indexes = [index for index, _unit in part.indexed_units]
    try:
        extracted = _extract_translated_units(translated_payload, indexes)
    except ConversionError:
        return translated_payload, set(indexes)
    restored: dict[int, str] = {}
    invalid_indexes: set[int] = set()
    units = dict(part.indexed_units)
    for index in indexes:
        try:
            value = _restore_epub_xml_markup(extracted[index], protected_markup[index])
            _parse_epub_translation(units[index], value)
        except ConversionError:
            restored[index] = extracted[index]
            invalid_indexes.add(index)
        else:
            restored[index] = value
    return _translation_part_payload_from_values(part.indexed_units, restored), invalid_indexes


def _validate_translated_part(
    part: _EpubTranslationPart,
    translated_payload: str,
) -> dict[int, str]:
    indexes = [index for index, _unit in part.indexed_units]
    extracted = _extract_translated_units(translated_payload, indexes)
    for index, unit in part.indexed_units:
        _parse_epub_translation(unit, extracted[index])
    return extracted


def _retry_invalid_translated_units(
    part: _EpubTranslationPart,
    translated_payload: str,
    translate_text: EpubTextTranslator,
    cancellation: CancellationToken | None,
    *,
    forced_indexes: set[int] | None = None,
) -> tuple[str, dict[int, str], frozenset[int]]:
    """Retry once only the units that fail protected EPUB reconstruction."""

    indexes = [index for index, _unit in part.indexed_units]
    try:
        extracted = _extract_translated_units(translated_payload, indexes)
    except ConversionError:
        extracted = {}
    invalid_indexes = set(forced_indexes or ())
    for index, unit in part.indexed_units:
        value = extracted.get(index)
        if value is None:
            invalid_indexes.add(index)
            continue
        try:
            _parse_epub_translation(unit, value)
        except ConversionError:
            invalid_indexes.add(index)

    if not invalid_indexes:
        raise AssertionError("A rejected EPUB part always contains an invalid unit.")
    for index, unit in part.indexed_units:
        if index not in invalid_indexes:
            continue
        check_cancelled(cancellation)
        indexed_unit = ((index, unit),)
        retry_source, protected_markup = _translation_model_part_payload(
            indexed_unit,
            retry=True,
        )
        retried_model_payload = translate_text(retry_source, None, cancellation)
        if not isinstance(retried_model_payload, str) or not retried_model_payload.strip():
            raise ConversionError("El traductor dejó vacío un fragmento EPUB reintentado.")
        retried_part = _EpubTranslationPart(part.key, indexed_unit)
        retried_payload = _restore_translated_part_markup(
            retried_part,
            retried_model_payload,
            protected_markup,
        )
        retried = _validate_translated_part(retried_part, retried_payload)
        extracted[index] = retried[index]

    rebuilt_payload = _translation_part_payload_from_values(part.indexed_units, extracted)
    return (
        rebuilt_payload,
        _validate_translated_part(part, rebuilt_payload),
        frozenset(invalid_indexes),
    )


def _translated_part_remains_source_language(
    part: _EpubTranslationPart,
    translated_values: dict[int, str],
    source_language_code: str | None,
) -> bool:
    if source_language_code is None:
        return False
    source_text: list[str] = []
    translated_text: list[str] = []
    for index, unit in part.indexed_units:
        source_parsed, source_attribute = _parse_epub_translation(unit, unit.source)
        translated_parsed, translated_attribute = _parse_epub_translation(
            unit,
            translated_values[index],
        )
        source_text.append(source_attribute or _element_text(source_parsed))
        translated_text.append(translated_attribute or _element_text(translated_parsed))
    normalized_source = _SPACE_PATTERN.sub(" ", "\n".join(source_text)).strip().casefold()
    normalized_translated = _SPACE_PATTERN.sub(" ", "\n".join(translated_text)).strip().casefold()
    if not normalized_source:
        return False
    if detect_language_code(normalized_source, minimum_letters=80) != source_language_code:
        return False
    if normalized_source == normalized_translated:
        return True
    return detect_language_code(normalized_translated, minimum_letters=80) == source_language_code


def _translated_unit_remains_source_language(
    unit: _EpubTranslationUnit,
    translated_value: str,
    source_language_code: str | None,
) -> bool:
    """Find one substantial aligned unit still confidently written in the source language."""

    if source_language_code is None:
        return False
    source_parsed, source_attribute = _parse_epub_translation(unit, unit.source)
    translated_parsed, translated_attribute = _parse_epub_translation(unit, translated_value)
    source_text = _SPACE_PATTERN.sub(
        " ",
        source_attribute or _element_text(source_parsed),
    ).strip()
    translated_text = _SPACE_PATTERN.sub(
        " ",
        translated_attribute or _element_text(translated_parsed),
    ).strip()
    if sum(character.isalpha() for character in source_text) < 80:
        return False
    if detect_language_code(source_text, minimum_letters=80) != source_language_code:
        return False
    if source_text.casefold() == translated_text.casefold():
        return True
    if (
        sum(character.isalpha() for character in translated_text) < 80
        or detect_language_code(translated_text, minimum_letters=80) != source_language_code
    ):
        return False
    return (
        SequenceMatcher(
            None,
            source_text.casefold(),
            translated_text.casefold(),
            autojunk=False,
        ).ratio()
        >= _MIN_SOURCE_RESIDUAL_RETRY_SIMILARITY
    )


def _translation_marker(index: int) -> str:
    return f"<!-- {_EPUB_TRANSLATION_MARKER_PREFIX} {_alphabetic_index(index)} -->"


def _alphabetic_index(index: int) -> str:
    letters: list[str] = []
    value = index
    while True:
        value, remainder = divmod(value, 26)
        letters.append(chr(ord("A") + remainder))
        if value == 0:
            break
        value -= 1
    return "".join(reversed(letters))


def _extract_translated_units(payload: str, indexes: list[int]) -> dict[int, str]:
    markers = [(_translation_marker(index), index) for index in indexes]
    positions: list[tuple[int, int, int]] = []
    for marker, index in markers:
        if payload.count(marker) != 1:
            raise ConversionError("El traductor cambió los límites internos del EPUB.")
        start = payload.index(marker)
        positions.append((start, start + len(marker), index))
    positions.sort()
    if [index for _start, _end, index in positions] != indexes:
        raise ConversionError("El traductor cambió el orden interno del EPUB.")

    result: dict[int, str] = {}
    for position, (_start, content_start, index) in enumerate(positions):
        content_end = positions[position + 1][0] if position + 1 < len(positions) else len(payload)
        value = payload[content_start:content_end].strip()
        if not value or _EPUB_TRANSLATION_MARKER_PREFIX in value:
            raise ConversionError("El traductor devolvió un fragmento EPUB incompleto.")
        result[index] = value
    return result


def _apply_epub_translations(
    roots: dict[str, XmlElementTree.Element],
    units: list[_EpubTranslationUnit],
    translated_values: list[str],
) -> set[str]:
    changed_paths: set[str] = set()
    element_replacements: list[tuple[_EpubTranslationUnit, XmlElementTree.Element]] = []
    text_replacements: list[tuple[_EpubTranslationUnit, str]] = []
    for unit, translated in zip(units, translated_values, strict=True):
        parsed, translated_text = _parse_epub_translation(unit, translated)
        if translated_text is None:
            element_replacements.append((unit, parsed))
        else:
            text_replacements.append((unit, translated_text))

    for unit, replacement in element_replacements:
        root = roots[unit.archive_path]
        if not unit.element_path:
            raise ConversionError("El EPUB contiene un bloque traducible no válido.")
        parent = _element_at_path(root, unit.element_path[:-1])
        index = unit.element_path[-1]
        original = parent[index]
        replacement.tail = original.tail
        parent.remove(original)
        parent.insert(index, replacement)
        changed_paths.add(unit.archive_path)
    for unit, translated_text in text_replacements:
        root = roots[unit.archive_path]
        element = _element_at_path(root, unit.element_path)
        if unit.attribute is not None:
            if unit.attribute not in element.attrib:
                raise ConversionError("No se pudo restaurar un atributo traducido del EPUB.")
            element.attrib[unit.attribute] = translated_text
        elif unit.text_slot == "text":
            element.text = _restore_text_slot_whitespace(element.text or "", translated_text)
        elif unit.text_slot == "tail":
            element.tail = _restore_text_slot_whitespace(element.tail or "", translated_text)
        else:
            raise ConversionError("No se pudo restaurar un texto traducido del EPUB.")
        changed_paths.add(unit.archive_path)
    return changed_paths


def _parse_epub_translation(
    unit: _EpubTranslationUnit,
    translated: str,
) -> tuple[XmlElementTree.Element, str | None]:
    try:
        parsed = _parse_xml_payload(translated.encode("utf-8"))
    except (XmlElementTree.ParseError, DefusedXmlException) as exc:
        raise ConversionError("La traducción produjo XHTML no válido.") from exc
    if unit.attribute is None and unit.text_slot is None:
        source_element = _parse_xml_payload(unit.source.encode("utf-8"))
        if _document_structure_signature(parsed) != _document_structure_signature(source_element):
            raise ConversionError("La traducción cambió la estructura interna de una parte.")
        _validate_epub_conserved_text(
            _element_text(source_element),
            _element_text(parsed),
        )
        return parsed, None
    if _local_name(parsed) != "span" or len(parsed):
        raise ConversionError("La traducción cambió un texto aislado del EPUB.")
    source_element = _parse_xml_payload(unit.source.encode("utf-8"))
    source_text = _element_text(source_element)
    translated_text = _element_text(parsed)
    if not translated_text:
        raise ConversionError("La traducción dejó vacío un texto aislado del EPUB.")
    _validate_epub_conserved_text(source_text, translated_text)
    return parsed, translated_text


def _restore_text_slot_whitespace(source: str, translated: str) -> str:
    """Keep exact XHTML boundary whitespace around one independently translated text node."""

    leading_length = len(source) - len(source.lstrip())
    trailing_length = len(source) - len(source.rstrip())
    leading = source[:leading_length]
    trailing = source[len(source) - trailing_length :] if trailing_length else ""
    return f"{leading}{translated.strip()}{trailing}"


def _validate_epub_conserved_text(source: str, translated: str) -> None:
    """Reject valid-looking EPUB text that changed protected factual tokens."""

    if not numeric_tokens_are_conserved(source, translated):
        raise ConversionError("La traducción EPUB cambió u omitió números o fechas.")
    if Counter(TITLE_ROMAN_REFERENCE_PATTERN.findall(source)) != Counter(
        TITLE_ROMAN_REFERENCE_PATTERN.findall(translated)
    ):
        raise ConversionError("La traducción EPUB cambió una referencia romana.")
    if Counter(RAW_URL_PATTERN.findall(source)) != Counter(RAW_URL_PATTERN.findall(translated)):
        raise ConversionError("La traducción EPUB cambió u omitió una dirección web.")
    try:
        validate_translation_content_coverage(source, translated)
    except TranslationQualityError as exc:
        raise ConversionError(
            "La traducción EPUB omitió o duplicó una parte sustancial del contenido."
        ) from exc


def _element_at_path(
    root: XmlElementTree.Element,
    path: tuple[int, ...],
) -> XmlElementTree.Element:
    element = root
    try:
        for index in path:
            element = element[index]
    except IndexError as exc:
        raise ConversionError("No se pudo reconstruir la estructura del EPUB.") from exc
    return element


def _set_epub_language(
    roots: dict[str, XmlElementTree.Element],
    manifest: dict[str, _ManifestItem],
    opf_path: str,
    target_language_code: str,
) -> set[str]:
    changed_paths: set[str] = set()
    opf_root = roots[opf_path]
    metadata = next(
        (element for element in opf_root.iter() if _local_name(element) == "metadata"),
        None,
    )
    language_elements = (
        [element for element in metadata.iter() if _local_name(element) == "language"]
        if metadata is not None
        else []
    )
    if language_elements:
        for element in language_elements:
            if element.text != target_language_code:
                element.text = target_language_code
                changed_paths.add(opf_path)
    elif metadata is not None:
        language = XmlElementTree.Element("{http://purl.org/dc/elements/1.1/}language")
        language.text = target_language_code
        metadata.append(language)
        changed_paths.add(opf_path)

    html_paths = {item.path for item in manifest.values() if _is_html_item(item)}
    for path in html_paths:
        root = roots.get(path)
        if root is None:
            continue
        if root.attrib.get("lang") != target_language_code:
            root.attrib["lang"] = target_language_code
            changed_paths.add(path)
        if root.attrib.get(_XML_LANGUAGE_ATTRIBUTE) != target_language_code:
            root.attrib[_XML_LANGUAGE_ATTRIBUTE] = target_language_code
            changed_paths.add(path)
    return changed_paths


def _validate_translated_roots(
    roots: dict[str, XmlElementTree.Element],
    original_signatures: dict[str, tuple[tuple[str, tuple[tuple[str, str], ...]], ...]],
) -> None:
    for path, root in roots.items():
        signature = _document_structure_signature(root, ignore_translatable_attributes=True)
        original = tuple(
            (
                tag,
                tuple((key, value) for key, value in attributes if _kept_structure_attribute(key)),
            )
            for tag, attributes in original_signatures[path]
        )
        if signature != original:
            raise ConversionError("La traducción cambió enlaces o estructura del EPUB.")
        serialized = XmlElementTree.tostring(root, encoding="unicode")
        if _EPUB_TRANSLATION_MARKER_PREFIX in serialized or "\0" in serialized:
            raise ConversionError("El EPUB traducido contiene marcadores internos no válidos.")


def _document_structure_signature(
    root: XmlElementTree.Element,
    *,
    ignore_translatable_attributes: bool = False,
) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    return tuple(
        (
            element.tag if isinstance(element.tag, str) else "",
            tuple(
                sorted(
                    (key, value)
                    for key, value in element.attrib.items()
                    if not ignore_translatable_attributes or _kept_structure_attribute(key)
                )
            ),
        )
        for element in root.iter()
    )


def _kept_structure_attribute(attribute: str) -> bool:
    local_attribute = attribute.rsplit("}", 1)[-1]
    return local_attribute not in _TRANSLATABLE_ATTRIBUTES | {"lang"}


def _serialize_xml_element(element: XmlElementTree.Element) -> str:
    tail = element.tail
    element.tail = None
    try:
        return XmlElementTree.tostring(element, encoding="unicode")
    finally:
        element.tail = tail


def _serialize_xml_document(
    root: XmlElementTree.Element,
    original_payload: bytes | None = None,
) -> bytes:
    if isinstance(root.tag, str) and root.tag.startswith("{"):
        namespace = root.tag.partition("}")[0][1:]
        prefix = "opf" if namespace == _OPF_NAMESPACE else ""
        XmlElementTree.register_namespace(prefix, namespace)
    XmlElementTree.register_namespace("dc", "http://purl.org/dc/elements/1.1/")
    XmlElementTree.register_namespace("epub", "http://www.idpf.org/2007/ops")
    XmlElementTree.register_namespace("svg", "http://www.w3.org/2000/svg")
    XmlElementTree.register_namespace("xlink", "http://www.w3.org/1999/xlink")
    if original_payload is None:
        return cast(bytes, XmlElementTree.tostring(root, encoding="utf-8", xml_declaration=True))

    root_offset = _xml_root_offset(original_payload)
    if root_offset is None:
        return cast(bytes, XmlElementTree.tostring(root, encoding="utf-8", xml_declaration=True))
    prolog = original_payload[:root_offset]
    encoding_match = _XML_DECLARATION_ENCODING_PATTERN.search(prolog)
    encoding = (
        encoding_match.group(1).decode("ascii", "strict") if encoding_match is not None else "utf-8"
    )
    try:
        serialized_root = cast(
            bytes,
            XmlElementTree.tostring(
                root,
                encoding=encoding,
                xml_declaration=False,
            ),
        )
    except (LookupError, UnicodeError):
        return cast(bytes, XmlElementTree.tostring(root, encoding="utf-8", xml_declaration=True))
    trailing_whitespace = re.search(rb"\s*\Z", original_payload)
    suffix = trailing_whitespace.group(0) if trailing_whitespace is not None else b""
    return prolog + serialized_root + suffix


def _xml_root_offset(payload: bytes) -> int | None:
    if payload.startswith(b"\xef\xbb\xbf"):
        position = 3
    else:
        position = 0
    length = len(payload)
    while position < length:
        while position < length and payload[position : position + 1].isspace():
            position += 1
        if payload.startswith(b"<?", position):
            end = payload.find(b"?>", position + 2)
            if end < 0:
                return None
            position = end + 2
            continue
        if payload.startswith(b"<!--", position):
            end = payload.find(b"-->", position + 4)
            if end < 0:
                return None
            position = end + 3
            continue
        if payload[position : position + 9].upper() == b"<!DOCTYPE":
            position = _xml_declaration_end(payload, position + 9)
            if position < 0:
                return None
            continue
        return position if payload.startswith(b"<", position) else None
    return None


def _xml_declaration_end(payload: bytes, position: int) -> int:
    quote_character: int | None = None
    subset_depth = 0
    while position < len(payload):
        character = payload[position]
        if quote_character is not None:
            if character == quote_character:
                quote_character = None
        elif character in {ord('"'), ord("'")}:
            quote_character = character
        elif character == ord("["):
            subset_depth += 1
        elif character == ord("]") and subset_depth:
            subset_depth -= 1
        elif character == ord(">") and subset_depth == 0:
            return position + 1
        position += 1
    return -1


def _rebuild_epub(
    archive: ZipFile,
    members: dict[str, ZipInfo],
    replacements: dict[str, bytes],
    cancellation: CancellationToken | None,
) -> tuple[bytes, int]:
    binary_resources = 0
    preserved_source_digests: dict[str, bytes] = {}
    infos = archive.infolist()
    ordered_infos = sorted(infos, key=lambda info: 0 if info.filename == "mimetype" else 1)
    with SpooledTemporaryFile(max_size=16 * 1024 * 1024) as output:
        with ZipFile(output, "w") as rebuilt:
            rebuilt.comment = archive.comment
            for info in ordered_infos:
                check_cancelled(cancellation)
                normalized = _normalize_archive_path(info.filename)
                if normalized not in replacements and normalized != "mimetype":
                    binary_resources += 1
                compression = ZIP_STORED if normalized == "mimetype" else info.compress_type
                target_info = copy(info)
                target_info.compress_type = compression
                if normalized in replacements:
                    rebuilt.writestr(
                        target_info,
                        replacements[normalized],
                        compress_type=compression,
                    )
                    continue
                if normalized == "mimetype":
                    try:
                        rebuilt.writestr(
                            target_info,
                            archive.read(info),
                            compress_type=ZIP_STORED,
                        )
                    except (KeyError, OSError, RuntimeError) as exc:
                        raise ConversionError(
                            "No se pudo copiar un recurso del EPUB original."
                        ) from exc
                    continue
                try:
                    source_digest = hashlib.sha256()
                    with (
                        archive.open(info) as source,
                        rebuilt.open(
                            target_info,
                            "w",
                            force_zip64=True,
                        ) as target,
                    ):
                        while block := source.read(1024 * 1024):
                            check_cancelled(cancellation)
                            source_digest.update(block)
                            target.write(block)
                    preserved_source_digests[normalized] = source_digest.digest()
                except (KeyError, OSError, RuntimeError) as exc:
                    raise ConversionError(
                        "No se pudo copiar un recurso del EPUB original."
                    ) from exc
        output.seek(0)
        with ZipFile(output) as verification:
            if verification.namelist()[0] != "mimetype":
                raise ConversionError("No se pudo reconstruir un EPUB compatible.")
            if verification.read("mimetype").strip() != _EPUB_MIMETYPE:
                raise ConversionError("El EPUB reconstruido perdió su identificación.")
            if set(verification.namelist()) != {info.filename for info in infos}:
                raise ConversionError("El EPUB reconstruido perdió archivos internos.")
            for normalized, info in members.items():
                if normalized in replacements or normalized == "mimetype":
                    continue
                if _zip_member_digest(verification, info.filename) != preserved_source_digests.get(
                    normalized
                ):
                    raise ConversionError("El EPUB reconstruido cambió un recurso binario.")
        output.seek(0)
        content = output.read()
    return content, binary_resources


def _zip_member_digest(archive: ZipFile, filename: str) -> bytes:
    digest = hashlib.sha256()
    with archive.open(filename) as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.digest()


def _validate_epub_container(source_path: Path) -> None:
    try:
        valid_container = is_zipfile(source_path)
    except OSError as exc:
        raise ConversionError(f"No se pudo abrir el EPUB {source_path.name}.") from exc
    if not valid_container:
        raise ConversionError(f"{source_path.name} no es un documento EPUB válido.")


def _validate_epub_encryption(
    archive: ZipFile,
    members: dict[str, ZipInfo],
    manifest: dict[str, _ManifestItem],
) -> None:
    if "META-INF/encryption.xml" not in members:
        return
    root = _parse_xml_member(
        archive,
        members,
        "META-INF/encryption.xml",
        "la declaración de recursos protegidos",
    )
    manifest_by_path = {item.path: item for item in manifest.values()}
    for encrypted_data in (
        element for element in root.iter() if _local_name(element) == "EncryptedData"
    ):
        method = next(
            (
                element
                for element in encrypted_data.iter()
                if _local_name(element) == "EncryptionMethod"
            ),
            None,
        )
        reference = next(
            (
                element
                for element in encrypted_data.iter()
                if _local_name(element) == "CipherReference"
            ),
            None,
        )
        algorithm = method.attrib.get("Algorithm", "") if method is not None else ""
        uri = reference.attrib.get("URI", "") if reference is not None else ""
        if algorithm not in _STANDARD_FONT_OBFUSCATION_ALGORITHMS or not uri:
            raise ConversionError(
                "Este EPUB contiene recursos cifrados y no puede traducirse con seguridad."
            )
        parsed = urlsplit(uri)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            raise ConversionError(
                "Este EPUB contiene recursos cifrados y no puede traducirse con seguridad."
            )
        resource_path = _normalize_archive_path(unquote(parsed.path).lstrip("/"))
        item = manifest_by_path.get(resource_path)
        if item is None or resource_path not in members or not _is_font_resource(item):
            raise ConversionError(
                "Este EPUB contiene recursos cifrados y no puede traducirse con seguridad."
            )


def _is_font_resource(item: _ManifestItem) -> bool:
    return item.media_type in _FONT_MEDIA_TYPES or PurePosixPath(item.path).suffix.casefold() in {
        ".otf",
        ".ttf",
        ".woff",
        ".woff2",
    }


def _validate_archive_limits(archive: ZipFile, filename: str) -> dict[str, ZipInfo]:
    infos = archive.infolist()
    if len(infos) > _MAX_ARCHIVE_ENTRIES:
        raise ConversionError(f"{filename} contiene demasiados archivos internos.")
    total_size = sum(info.file_size for info in infos)
    if total_size > _MAX_ARCHIVE_UNCOMPRESSED_BYTES:
        raise ConversionError(f"{filename} es demasiado grande una vez descomprimido.")

    members: dict[str, ZipInfo] = {}
    for info in infos:
        if info.is_dir():
            continue
        if info.flag_bits & 0x1:
            raise ConversionError(f"{filename} contiene archivos cifrados.")
        if info.file_size > _MAX_ARCHIVE_MEMBER_BYTES:
            raise ConversionError(f"{filename} contiene un archivo interno demasiado grande.")
        unsafe_ratio = info.file_size and (
            not info.compress_size
            or info.file_size / info.compress_size > _MAX_ARCHIVE_COMPRESSION_RATIO
        )
        if unsafe_ratio:
            raise ConversionError(f"{filename} contiene datos con una compresi\u00f3n no segura.")
        normalized = _normalize_archive_path(info.filename)
        if not normalized or normalized in members:
            raise ConversionError(f"{filename} contiene rutas internas no válidas o repetidas.")
        members[normalized] = info

    mimetype_info = members.get("mimetype")
    if mimetype_info is None:
        raise ConversionError(f"{filename} no contiene la identificación mínima de un EPUB.")
    try:
        mimetype = archive.read(mimetype_info).strip()
    except (KeyError, OSError, RuntimeError) as exc:
        raise ConversionError(f"No se pudo leer la identificación EPUB de {filename}.") from exc
    if mimetype != _EPUB_MIMETYPE:
        raise ConversionError(f"{filename} no declara un tipo EPUB válido.")
    if _CONTAINER_PATH not in members:
        raise ConversionError(f"{filename} no contiene la estructura mínima de un EPUB.")
    return members


def _read_rootfile_path(
    archive: ZipFile,
    members: dict[str, ZipInfo],
    filename: str,
) -> str:
    root = _parse_xml_member(archive, members, _CONTAINER_PATH, "el contenedor EPUB")
    rootfile = next(
        (element for element in root.iter() if _local_name(element) == "rootfile"),
        None,
    )
    if rootfile is None or not rootfile.attrib.get("full-path"):
        raise ConversionError(f"{filename} no indica dónde se encuentra su contenido.")
    opf_path = _normalize_archive_path(rootfile.attrib["full-path"])
    if opf_path not in members:
        raise ConversionError(f"{filename} no contiene el paquete de contenido declarado.")
    return opf_path


def _read_manifest(
    opf_root: XmlElementTree.Element,
    opf_path: str,
) -> dict[str, _ManifestItem]:
    manifest: dict[str, _ManifestItem] = {}
    for element in opf_root.iter():
        if _local_name(element) != "item":
            continue
        item_id = element.attrib.get("id", "").strip()
        href = element.attrib.get("href", "").strip()
        if not item_id or not href:
            continue
        resolved = _resolve_internal_reference(opf_path, href)
        if resolved is None:
            continue
        path, _fragment = resolved
        manifest[item_id] = _ManifestItem(
            item_id=item_id,
            path=path,
            media_type=element.attrib.get("media-type", "").strip().lower(),
            properties=frozenset(element.attrib.get("properties", "").split()),
        )
    if not manifest:
        raise ConversionError("El EPUB no contiene un manifiesto de archivos válido.")
    return manifest


def _read_spine(opf_root: XmlElementTree.Element) -> tuple[tuple[str, ...], str | None]:
    spine = next((element for element in opf_root.iter() if _local_name(element) == "spine"), None)
    if spine is None:
        raise ConversionError("El EPUB no declara un orden de lectura.")
    item_ids = tuple(
        element.attrib["idref"].strip()
        for element in spine
        if _local_name(element) == "itemref" and element.attrib.get("idref", "").strip()
    )
    if not item_ids:
        raise ConversionError("El EPUB no contiene capítulos en su orden de lectura.")
    toc_id = spine.attrib.get("toc", "").strip() or None
    return item_ids, toc_id


def _read_metadata(opf_root: XmlElementTree.Element) -> _Metadata:
    metadata = next(
        (element for element in opf_root.iter() if _local_name(element) == "metadata"),
        None,
    )
    if metadata is None:
        return _Metadata(None, (), None, None, None, (), None)

    values: dict[str, list[str]] = {}
    cover_item_id: str | None = None
    for element in metadata.iter():
        name = _local_name(element)
        text = _element_text(element)
        if text:
            values.setdefault(name, []).append(text)
        if name == "meta" and element.attrib.get("name", "").casefold() == "cover":
            cover_item_id = element.attrib.get("content", "").strip() or None
    return _Metadata(
        title=_first(values, "title"),
        authors=tuple(values.get("creator", ())),
        language=_first(values, "language"),
        publisher=_first(values, "publisher"),
        date=_first(values, "date"),
        identifiers=tuple(values.get("identifier", ())),
        cover_item_id=cover_item_id,
    )


def _read_spine_documents(
    archive: ZipFile,
    members: dict[str, ZipInfo],
    manifest: dict[str, _ManifestItem],
    spine_ids: tuple[str, ...],
    filename: str,
    cancellation: CancellationToken | None,
) -> list[_ContentDocument]:
    documents: list[_ContentDocument] = []
    seen_paths: set[str] = set()
    for item_id in spine_ids:
        check_cancelled(cancellation)
        item = manifest.get(item_id)
        if item is None or item.path in seen_paths:
            continue
        if not _is_html_item(item):
            continue
        if item.path not in members:
            chapter_name = PurePosixPath(item.path).name
            raise ConversionError(f"{filename} no contiene el capítulo {chapter_name}.")
        root = _parse_xml_member(archive, members, item.path, "un capítulo EPUB")
        documents.append(_ContentDocument(item.path, root))
        seen_paths.add(item.path)
    if not documents:
        raise ConversionError(f"{filename} no contiene capítulos XHTML convertibles.")
    return documents


def _read_navigation(
    archive: ZipFile,
    members: dict[str, ZipInfo],
    manifest: dict[str, _ManifestItem],
    toc_id: str | None,
    documents: list[_ContentDocument],
) -> tuple[_NavigationEntry, ...]:
    nav_item = next((item for item in manifest.values() if "nav" in item.properties), None)
    if nav_item is not None and nav_item.path in members:
        root = _parse_xml_member(archive, members, nav_item.path, "el índice EPUB")
        entries = _read_epub3_navigation(root, nav_item.path)
        if entries:
            return entries

    ncx_item = manifest.get(toc_id) if toc_id else None
    if ncx_item is None:
        ncx_item = next(
            (item for item in manifest.values() if item.media_type in _NAVIGATION_MEDIA_TYPES),
            None,
        )
    if ncx_item is not None and ncx_item.path in members:
        root = _parse_xml_member(archive, members, ncx_item.path, "el índice EPUB")
        entries = _read_ncx_navigation(root, ncx_item.path)
        if entries:
            return entries

    return tuple(
        _NavigationEntry(
            label=_document_title(document.root) or PurePosixPath(document.path).stem,
            path=document.path,
            fragment="",
            depth=1,
        )
        for document in documents
    )


def _read_ncx_navigation(
    root: XmlElementTree.Element,
    navigation_path: str,
) -> tuple[_NavigationEntry, ...]:
    nav_map = next((element for element in root.iter() if _local_name(element) == "navMap"), None)
    if nav_map is None:
        return ()
    entries: list[_NavigationEntry] = []

    def visit(parent: XmlElementTree.Element, depth: int) -> None:
        for point in parent:
            if _local_name(point) != "navPoint":
                continue
            label_element = next(
                (element for element in point.iter() if _local_name(element) == "navLabel"),
                None,
            )
            content = next(
                (element for element in point if _local_name(element) == "content"),
                None,
            )
            label = _element_text(label_element) if label_element is not None else ""
            source = content.attrib.get("src", "") if content is not None else ""
            resolved = _resolve_internal_reference(navigation_path, source)
            if label and resolved is not None:
                path, fragment = resolved
                entries.append(_NavigationEntry(label, path, fragment, depth))
            visit(point, depth + 1)

    visit(nav_map, 1)
    return _deduplicate_navigation(entries)


def _read_epub3_navigation(
    root: XmlElementTree.Element,
    navigation_path: str,
) -> tuple[_NavigationEntry, ...]:
    nav_elements = [element for element in root.iter() if _local_name(element) == "nav"]
    toc = next(
        (
            element
            for element in nav_elements
            if "toc" in _attribute_by_local_name(element, "type").split()
        ),
        nav_elements[0] if nav_elements else None,
    )
    if toc is None:
        return ()
    ordered_list = next((element for element in toc if _local_name(element) == "ol"), None)
    if ordered_list is None:
        ordered_list = next(
            (element for element in toc.iter() if _local_name(element) == "ol"),
            None,
        )
    if ordered_list is None:
        return ()

    entries: list[_NavigationEntry] = []

    def visit(ordered: XmlElementTree.Element, depth: int) -> None:
        for item in ordered:
            if _local_name(item) != "li":
                continue
            link = next(
                (element for element in item if _local_name(element) in {"a", "span"}),
                None,
            )
            label = _element_text(link) if link is not None else ""
            href = link.attrib.get("href", "") if link is not None else ""
            resolved = _resolve_internal_reference(navigation_path, href)
            if label and resolved is not None:
                path, fragment = resolved
                entries.append(_NavigationEntry(label, path, fragment, depth))
            for child in item:
                if _local_name(child) == "ol":
                    visit(child, depth + 1)

    visit(ordered_list, 1)
    return _deduplicate_navigation(entries)


def _convert_documents(
    documents: list[_ContentDocument],
    navigation: tuple[_NavigationEntry, ...],
    manifest: dict[str, _ManifestItem],
    metadata: _Metadata,
    cancellation: CancellationToken | None,
) -> tuple[str, set[str]]:
    from markitdown import MarkItDown, MarkItDownException

    for document in documents:
        _repair_fragmented_paragraphs(document.root)
    anchor_map = _build_anchor_map(documents)
    document_paths = {document.path for document in documents}
    required_anchor_keys = _required_anchor_keys(
        documents,
        navigation,
        anchor_map,
        document_paths,
    )
    resource_paths = {
        item.path for item in manifest.values() if item.media_type.startswith("image/")
    }
    navigation_by_path: dict[str, list[_NavigationEntry]] = {}
    for entry in navigation:
        navigation_by_path.setdefault(entry.path, []).append(entry)

    converted_chapters: list[str] = []
    referenced_resources: set[str] = set()
    cover_item = _cover_manifest_item(metadata, manifest)
    cover_path = cover_item.path if cover_item is not None else None
    markdown_converter = MarkItDown(enable_plugins=False)
    for document in documents:
        check_cancelled(cancellation)
        if metadata.title:
            _demote_existing_headings(document.root)
        entries = navigation_by_path.get(document.path, [])
        preferred_markers = _promote_navigation_headings(document.root, entries)
        marker_replacements = _inject_anchor_markers(
            document.root,
            document.path,
            anchor_map,
            required_anchor_keys,
            preferred_markers,
        )
        referenced_resources.update(
            _rewrite_document_references(
                document.root,
                document.path,
                anchor_map,
                document_paths,
                resource_paths,
                cover_path,
            )
        )
        _strip_xml_namespaces(document.root)
        serialized = XmlElementTree.tostring(
            document.root,
            encoding="utf-8",
        )
        try:
            result = markdown_converter.convert_stream(
                BytesIO(serialized),
                file_extension=".html",
                strict=True,
            )
        except (MarkItDownException, OSError, RecursionError, ValueError) as exc:
            raise ConversionError(
                f"No se pudo convertir el capítulo {PurePosixPath(document.path).name}."
            ) from exc
        if not isinstance(result.markdown, str):
            raise ConversionError("MarkItDown devolvió un capítulo EPUB no válido.")
        chapter = result.markdown.strip()
        for marker, anchor_comment in marker_replacements.items():
            chapter = chapter.replace(marker, anchor_comment)
        if chapter:
            converted_chapters.append(chapter)

    metadata_markdown = _metadata_markdown(metadata)
    if cover_path is not None and cover_path not in referenced_resources:
        metadata_markdown.append(f"![Portada]({_resource_reference(cover_path)})")
        referenced_resources.add(cover_path)

    sections = [
        section for section in ["\n\n".join(metadata_markdown), *converted_chapters] if section
    ]
    return "\n\n".join(sections).strip() + "\n", referenced_resources


def _build_anchor_map(documents: list[_ContentDocument]) -> dict[tuple[str, str], str]:
    anchors: dict[tuple[str, str], str] = {}
    for document_index, document in enumerate(documents, start=1):
        anchors[(document.path, "")] = f"epub-document-{document_index}"
        anchor_index = 0
        for element in document.root.iter():
            fragment = element.attrib.get("id") or element.attrib.get("name")
            if not fragment or (document.path, fragment) in anchors:
                continue
            anchor_index += 1
            hint = _anchor_hint(fragment)
            suffix = f"-{hint}" if hint else ""
            anchors[(document.path, fragment)] = (
                f"epub-document-{document_index}-anchor-{anchor_index}{suffix}"
            )
    return anchors


def _required_anchor_keys(
    documents: list[_ContentDocument],
    navigation: tuple[_NavigationEntry, ...],
    anchor_map: dict[tuple[str, str], str],
    document_paths: set[str],
) -> frozenset[tuple[str, str]]:
    required: set[tuple[str, str]] = set()

    def require(path: str, fragment: str) -> None:
        if path not in document_paths:
            return
        exact = (path, fragment)
        required.add(exact if exact in anchor_map else (path, ""))

    for entry in navigation:
        require(entry.path, entry.fragment)
    for document in documents:
        for element in document.root.iter():
            if _local_name(element) != "a" or not element.attrib.get("href"):
                continue
            resolved = _resolve_internal_reference(document.path, element.attrib["href"])
            if resolved is not None:
                require(*resolved)
    return frozenset(required)


def _promote_navigation_headings(
    root: XmlElementTree.Element,
    entries: list[_NavigationEntry],
) -> dict[str, XmlElementTree.Element]:
    body = next((element for element in root.iter() if _local_name(element) == "body"), root)
    preferred: dict[str, XmlElementTree.Element] = {}
    used_targets: set[tuple[str, str]] = set()
    for entry in entries:
        target = _find_fragment_element(root, entry.fragment) if entry.fragment else None
        block = _top_level_block(target, body) if target is not None else None
        if block is None:
            children = list(body)
            block = children[0] if children else None
        target_key = (entry.fragment, entry.label.casefold())
        if block is None or target_key in used_targets:
            continue
        used_targets.add(target_key)
        matched_blocks = _matching_title_blocks(body, block, entry.label)
        heading_level = min(6, max(2, entry.depth + 1))
        if matched_blocks:
            heading = matched_blocks[0]
            retained_ids = _element_identifiers(matched_blocks)
            tail = heading.tail
            heading.clear()
            heading.tag = _qualified_tag(root, f"h{heading_level}")
            heading.text = entry.label
            heading.tail = tail
            for redundant in matched_blocks[1:]:
                body.remove(redundant)
            for identifier in retained_ids:
                preferred.setdefault(identifier, heading)
        else:
            heading = XmlElementTree.Element(_qualified_tag(root, f"h{heading_level}"))
            heading.text = entry.label
            index = list(body).index(block)
            body.insert(index, heading)
        if entry.fragment:
            preferred[entry.fragment] = heading
        else:
            preferred[""] = heading
    return preferred


def _matching_title_blocks(
    body: XmlElementTree.Element,
    first_block: XmlElementTree.Element,
    label: str,
) -> list[XmlElementTree.Element]:
    children = list(body)
    try:
        start = children.index(first_block)
    except ValueError:
        return []
    target = _comparison_text(label)
    candidates: list[XmlElementTree.Element] = []
    combined = ""
    for candidate in children[start : start + 4]:
        if _contains_complex_content(candidate):
            return []
        candidates.append(candidate)
        combined += _comparison_text(_element_text(candidate))
        if combined == target:
            return candidates
        if combined and not target.startswith(combined):
            break
    return []


def _inject_anchor_markers(
    root: XmlElementTree.Element,
    document_path: str,
    anchor_map: dict[tuple[str, str], str],
    required_anchor_keys: frozenset[tuple[str, str]],
    preferred: dict[str, XmlElementTree.Element],
) -> dict[str, str]:
    body = next((element for element in root.iter() if _local_name(element) == "body"), root)
    children = list(body)
    actual: dict[str, XmlElementTree.Element] = {}
    for element in root.iter():
        identifier = element.attrib.get("id") or element.attrib.get("name")
        if identifier:
            actual.setdefault(identifier, element)

    markers_by_block: dict[XmlElementTree.Element, list[tuple[str, str]]] = {}
    for (path, fragment), anchor in anchor_map.items():
        if path != document_path or (path, fragment) not in required_anchor_keys:
            continue
        if fragment:
            target = preferred.get(fragment)
            if target is None:
                target = actual.get(fragment)
        else:
            target = preferred.get("")
            if target is None:
                target = children[0] if children else body
        if target is None:
            continue
        block = _top_level_block(target, body)
        if block is None:
            block = children[0] if children else body
        marker = f"PZDOCEPUBANCHOR{anchor.replace('-', '').upper()}XZQ"
        markers_by_block.setdefault(block, []).append((marker, anchor))

    replacements: dict[str, str] = {}
    for block in list(body):
        for marker, anchor in markers_by_block.get(block, []):
            marker_element = XmlElementTree.Element(_qualified_tag(root, "p"))
            marker_element.text = marker
            body.insert(list(body).index(block), marker_element)
            replacements[marker] = f"<!-- PZDOC EPUB ANCHOR {anchor} -->"
    if (
        not list(body)
        and (document_path, "") in anchor_map
        and (document_path, "") in required_anchor_keys
    ):
        anchor = anchor_map[(document_path, "")]
        marker = f"PZDOCEPUBANCHOR{anchor.replace('-', '').upper()}XZQ"
        marker_element = XmlElementTree.Element(_qualified_tag(root, "p"))
        marker_element.text = marker
        body.append(marker_element)
        replacements[marker] = f"<!-- PZDOC EPUB ANCHOR {anchor} -->"
    return replacements


def _rewrite_document_references(
    root: XmlElementTree.Element,
    document_path: str,
    anchor_map: dict[tuple[str, str], str],
    document_paths: set[str],
    resource_paths: set[str],
    cover_path: str | None,
) -> set[str]:
    referenced_resources: set[str] = set()
    for element in root.iter():
        name = _local_name(element)
        if name == "svg":
            svg_resource = _replace_simple_svg_image(
                root,
                element,
                document_path,
                resource_paths,
                cover_path,
            )
            if svg_resource is not None:
                referenced_resources.add(svg_resource)
            continue
        if name == "img":
            source_attribute = "src" if element.attrib.get("src") else "data-src"
            source = element.attrib.get(source_attribute, "")
            resolved = _resolve_internal_reference(document_path, source)
            if resolved is not None and resolved[0] in resource_paths:
                path, _fragment = resolved
                element.attrib[source_attribute] = _resource_reference(path)
                referenced_resources.add(path)
        if name != "a" or not element.attrib.get("href"):
            continue
        resolved = _resolve_internal_reference(document_path, element.attrib["href"])
        if resolved is None:
            continue
        path, fragment = resolved
        if path in document_paths:
            anchor = anchor_map.get((path, fragment)) or anchor_map.get((path, ""))
            if anchor is not None:
                element.attrib["href"] = f"#{anchor}"
        elif path in resource_paths:
            element.attrib["href"] = _resource_reference(path)
            referenced_resources.add(path)
    return referenced_resources


def _replace_simple_svg_image(
    root: XmlElementTree.Element,
    svg: XmlElementTree.Element,
    document_path: str,
    resource_paths: set[str],
    cover_path: str | None,
) -> str | None:
    descendants = list(svg.iter())
    images = [element for element in descendants if _local_name(element) == "image"]
    simple_elements = {"svg", "image", "title", "desc"}
    if len(images) != 1 or any(
        _local_name(element) not in simple_elements for element in descendants
    ):
        return None
    image = images[0]
    href = _attribute_by_local_name(image, "href")
    resolved = _resolve_internal_reference(document_path, href)
    if resolved is None or resolved[0] not in resource_paths:
        return None
    path, _fragment = resolved
    title_element = next(
        (element for element in svg.iter() if _local_name(element) == "title"),
        None,
    )
    alt = (
        image.attrib.get("alt", "").strip()
        or svg.attrib.get("aria-label", "").strip()
        or (_element_text(title_element) if title_element is not None else "")
        or ("Portada" if path == cover_path else "Imagen")
    )
    tail = svg.tail
    svg.clear()
    svg.tag = _qualified_tag(root, "img")
    svg.attrib["src"] = _resource_reference(path)
    svg.attrib["alt"] = alt
    svg.tail = tail
    return path


def _read_resources(
    archive: ZipFile,
    members: dict[str, ZipInfo],
    manifest: dict[str, _ManifestItem],
    referenced_resources: set[str],
    cancellation: CancellationToken | None,
) -> tuple[ConvertedResource, ...]:
    media_types = {item.path: item.media_type for item in manifest.values()}
    resources: list[ConvertedResource] = []
    casefolded_paths: set[str] = set()
    for path in sorted(referenced_resources):
        check_cancelled(cancellation)
        info = members.get(path)
        if info is None:
            raise ConversionError(f"El EPUB no contiene el recurso {PurePosixPath(path).name}.")
        file_size = getattr(info, "file_size", _MAX_RESOURCE_BYTES + 1)
        if file_size > _MAX_RESOURCE_BYTES:
            raise ConversionError(f"El recurso {PurePosixPath(path).name} es demasiado grande.")
        folded = path.casefold()
        if folded in casefolded_paths:
            raise ConversionError("El EPUB contiene recursos con nombres incompatibles en Windows.")
        casefolded_paths.add(folded)
        try:
            content = archive.read(info)
        except (KeyError, OSError, RuntimeError) as exc:
            raise ConversionError(f"No se pudo leer {PurePosixPath(path).name} del EPUB.") from exc
        resources.append(
            ConvertedResource(
                relative_path=PurePosixPath(path),
                content=content,
                media_type=media_types.get(path, "application/octet-stream"),
            )
        )
    return tuple(resources)


def _metadata_markdown(metadata: _Metadata) -> list[str]:
    lines: list[str] = []
    if metadata.title:
        lines.append(f"# {metadata.title}")
    if metadata.authors:
        lines.append(f"**Autor:** {', '.join(metadata.authors)}")
    if metadata.language:
        lines.append(f"**Idioma declarado:** {metadata.language}")
    if metadata.publisher:
        lines.append(f"**Editorial:** {metadata.publisher}")
    if metadata.date:
        lines.append(f"**Fecha:** {metadata.date}")
    if metadata.identifier:
        lines.append(f"**Identificador:** {metadata.identifier}")
    return lines


def _cover_manifest_item(
    metadata: _Metadata,
    manifest: dict[str, _ManifestItem],
) -> _ManifestItem | None:
    if metadata.cover_item_id and metadata.cover_item_id in manifest:
        item = manifest[metadata.cover_item_id]
        if item.media_type.startswith("image/"):
            return item
    return next(
        (
            item
            for item in manifest.values()
            if "cover-image" in item.properties and item.media_type.startswith("image/")
        ),
        None,
    )


def _repair_fragmented_paragraphs(root: XmlElementTree.Element) -> None:
    """Join obvious page-break fragments without guessing across real paragraphs."""
    body = next((element for element in root.iter() if _local_name(element) == "body"), root)
    index = 0
    while index + 1 < len(body):
        current = body[index]
        following = body[index + 1]
        current_text = _element_text(current)
        following_text = _element_text(following)
        current_contains_link = any(
            _local_name(element) == "a" and element.attrib.get("href") for element in current.iter()
        )
        following_starts_with_link = any(
            _local_name(element) == "a" and element.attrib.get("href")
            for element in following.iter()
        )
        prose_continuation = len(current_text) >= 60 and _starts_with_lowercase(following_text)
        short_link_lead_in = (
            not current_contains_link
            and current_text.casefold().rstrip(":") in _LINK_LEAD_INS
            and following_starts_with_link
        )
        should_join = (
            _local_name(current) == "p"
            and _local_name(following) == "p"
            and bool(current_text)
            and not _SENTENCE_END_PATTERN.search(current_text)
            and (prose_continuation or short_link_lead_in)
            and not _contains_complex_content(current)
            and not _contains_complex_content(following)
        )
        if not should_join:
            index += 1
            continue
        _append_paragraph(current, following, root)
        body.remove(following)


def _append_paragraph(
    destination: XmlElementTree.Element,
    source: XmlElementTree.Element,
    root: XmlElementTree.Element,
) -> None:
    source_children = list(source)
    same_split_link = (
        len(destination) > 0
        and source_children
        and _equivalent_plain_links(destination[-1], source_children[0])
        and not (source.text or "").strip()
    )
    separator = "" if same_split_link or _element_text(destination).endswith(("-", "‐")) else " "
    _append_text(destination, separator + (source.text or ""))

    for attribute in ("id", "name"):
        identifier = source.attrib.get(attribute)
        if identifier:
            anchor = XmlElementTree.Element(_qualified_tag(root, "a"), {attribute: identifier})
            destination.append(anchor)
    for child in source_children:
        source.remove(child)
        destination.append(child)
    _merge_adjacent_equivalent_links(destination)


def _append_text(element: XmlElementTree.Element, value: str) -> None:
    if not value:
        return
    if len(element):
        element[-1].tail = (element[-1].tail or "") + value
    else:
        element.text = (element.text or "") + value


def _merge_adjacent_equivalent_links(paragraph: XmlElementTree.Element) -> None:
    index = 0
    while index + 1 < len(paragraph):
        first = paragraph[index]
        second = paragraph[index + 1]
        if not _equivalent_plain_links(first, second) or (first.tail or "").strip():
            index += 1
            continue
        first.text = (first.text or "") + (second.text or "")
        first.tail = second.tail
        paragraph.remove(second)


def _equivalent_plain_links(
    first: XmlElementTree.Element,
    second: XmlElementTree.Element,
) -> bool:
    return (
        _local_name(first) == "a"
        and _local_name(second) == "a"
        and len(first) == 0
        and len(second) == 0
        and bool(first.attrib.get("href"))
        and first.attrib.get("href") == second.attrib.get("href")
    )


def _starts_with_lowercase(value: str) -> bool:
    first_letter = next((character for character in value if character.isalpha()), "")
    return bool(first_letter) and first_letter.islower()


def _parse_xml_member(
    archive: ZipFile,
    members: dict[str, ZipInfo],
    path: str,
    description: str,
) -> XmlElementTree.Element:
    info = members.get(path)
    if info is None:
        raise ConversionError(f"El EPUB no contiene {description}.")
    if getattr(info, "file_size", _MAX_XML_BYTES + 1) > _MAX_XML_BYTES:
        raise ConversionError(f"El EPUB contiene {description} demasiado grande.")
    try:
        payload = archive.read(info)
        return _parse_xml_payload(payload)
    except (KeyError, OSError, RuntimeError, XmlElementTree.ParseError, DefusedXmlException) as exc:
        raise ConversionError(f"No se pudo interpretar {description}.") from exc


def _parse_xml_payload(payload: bytes) -> XmlElementTree.Element:
    parser = ElementTree.DefusedXMLParser(
        target=XmlElementTree.TreeBuilder(insert_comments=True, insert_pis=True),
        forbid_dtd=False,
        forbid_entities=True,
        forbid_external=True,
    )
    return XmlElementTree.fromstring(payload, parser=parser)


def _resolve_internal_reference(base_path: str, reference: str) -> tuple[str, str] | None:
    if not reference:
        return None
    try:
        parsed = urlsplit(reference)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc:
        return None
    raw_path = unquote(parsed.path)
    if raw_path:
        if raw_path.startswith("/"):
            combined = raw_path.lstrip("/")
        else:
            combined = f"{PurePosixPath(base_path).parent.as_posix()}/{raw_path}"
    else:
        combined = base_path
    try:
        path = _normalize_archive_path(combined)
    except ConversionError:
        return None
    return path, unquote(parsed.fragment)


def _normalize_archive_path(raw_path: str) -> str:
    if not raw_path or "\\" in raw_path or "\0" in raw_path:
        raise ConversionError("El EPUB contiene una ruta interna no válida.")
    parts: list[str] = []
    for part in PurePosixPath(raw_path).parts:
        if part in {"", ".", "/"}:
            continue
        if part == "..":
            if not parts:
                raise ConversionError("El EPUB contiene una ruta fuera de su contenedor.")
            parts.pop()
            continue
        parts.append(part)
    if not parts:
        raise ConversionError("El EPUB contiene una ruta interna vacía.")
    return PurePosixPath(*parts).as_posix()


def _deduplicate_navigation(
    entries: list[_NavigationEntry],
) -> tuple[_NavigationEntry, ...]:
    result: list[_NavigationEntry] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in entries:
        key = (entry.path, entry.fragment, entry.label.casefold())
        if key not in seen:
            result.append(entry)
            seen.add(key)
    return tuple(result)


def _document_title(root: XmlElementTree.Element) -> str | None:
    title = next((element for element in root.iter() if _local_name(element) == "title"), None)
    text = _element_text(title) if title is not None else ""
    return text or None


def _find_fragment_element(
    root: XmlElementTree.Element,
    fragment: str,
) -> XmlElementTree.Element | None:
    return next(
        (
            element
            for element in root.iter()
            if element.attrib.get("id") == fragment or element.attrib.get("name") == fragment
        ),
        None,
    )


def _top_level_block(
    element: XmlElementTree.Element | None,
    body: XmlElementTree.Element,
) -> XmlElementTree.Element | None:
    if element is None:
        return None
    if element is body:
        return list(body)[0] if list(body) else body
    parents = {child: parent for parent in body.iter() for child in parent}
    current = element
    while current in parents and parents[current] is not body:
        current = parents[current]
    return current if current in list(body) else None


def _element_identifiers(elements: list[XmlElementTree.Element]) -> tuple[str, ...]:
    result: list[str] = []
    for element in elements:
        for descendant in element.iter():
            identifier = descendant.attrib.get("id") or descendant.attrib.get("name")
            if identifier and identifier not in result:
                result.append(identifier)
    return tuple(result)


def _contains_complex_content(element: XmlElementTree.Element) -> bool:
    return any(
        _local_name(descendant) in {"img", "svg", "table", "audio", "video"}
        for descendant in element.iter()
    )


def _comparison_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _anchor_hint(fragment: str) -> str:
    normalized = unicodedata.normalize("NFKD", fragment)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    hint = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    return hint[:40]


def _resource_reference(path: str) -> str:
    return f"{RESOURCE_REFERENCE_PREFIX}{quote(path, safe='/._-~')}"


def _qualified_tag(root: XmlElementTree.Element, local_name: str) -> str:
    if isinstance(root.tag, str) and root.tag.startswith("{"):
        namespace = root.tag.partition("}")[0][1:]
        return f"{{{namespace}}}{local_name}"
    return local_name


def _demote_existing_headings(root: XmlElementTree.Element) -> None:
    """Reserve level one for the book title while retaining source heading hierarchy."""
    for element in root.iter():
        name = _local_name(element)
        if len(name) == 2 and name[0] == "h" and name[1] in "12345":
            element.tag = _qualified_tag(root, f"h{int(name[1]) + 1}")


def _strip_xml_namespaces(root: XmlElementTree.Element) -> None:
    """Present sanitized XHTML as ordinary HTML to MarkItDown's public HTML converter."""
    for element in root.iter():
        if isinstance(element.tag, str):
            element.tag = element.tag.rsplit("}", 1)[-1]
        element.attrib = {key.rsplit("}", 1)[-1]: value for key, value in element.attrib.items()}


def _local_name(element: XmlElementTree.Element) -> str:
    tag = element.tag
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _attribute_by_local_name(element: XmlElementTree.Element, name: str) -> str:
    for key, value in element.attrib.items():
        if key.rsplit("}", 1)[-1] == name:
            return value
    return ""


def _element_text(element: XmlElementTree.Element) -> str:
    return _SPACE_PATTERN.sub(" ", "".join(element.itertext())).strip()


def _first(values: dict[str, list[str]], key: str) -> str | None:
    entries = values.get(key, [])
    return entries[0] if entries else None


def _is_html_item(item: _ManifestItem) -> bool:
    return item.media_type in _HTML_MEDIA_TYPES or PurePosixPath(item.path).suffix.lower() in {
        ".html",
        ".htm",
        ".xhtml",
    }
