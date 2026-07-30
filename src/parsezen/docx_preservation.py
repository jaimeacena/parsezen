"""Format-preserving DOCX text transformation without a Markdown round-trip."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from lxml import etree

from parsezen.cancellation import CancellationToken, check_cancelled
from parsezen.conversion import validate_docx_container
from parsezen.errors import ConversionError, TranslationError

_WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_XML_NAMESPACE = "http://www.w3.org/XML/1998/namespace"
_TEXT_TAG = f"{{{_WORD_NAMESPACE}}}t"
_PARAGRAPH_TAG = f"{{{_WORD_NAMESPACE}}}p"
_SPACE_ATTRIBUTE = f"{{{_XML_NAMESPACE}}}space"
_TRANSFORMABLE_PART = re.compile(
    r"^word/(?:document|footnotes|endnotes|comments|glossary/document|header\d+|footer\d+)\.xml$"
)
_MARKER_TEMPLATE = "<!-- PZDOCDOCX{index:06d}XZQ -->"

TextTransform = Callable[[str], str]


@dataclass(frozen=True, slots=True)
class DocxTransformation:
    """A rebuilt DOCX plus marker-free text used for local quality reporting."""

    content: bytes
    source_text: str
    transformed_text: str
    paragraph_count: int
    preserved_resources: int = 0
    source_paragraphs: tuple[str, ...] = ()
    transformed_paragraphs: tuple[str, ...] = ()


@dataclass(slots=True)
class _Paragraph:
    nodes: list[etree._Element]
    source: str


@dataclass(slots=True)
class _XmlPart:
    name: str
    root: etree._Element
    paragraphs: list[_Paragraph]


def transform_docx(
    source_path: Path,
    transform: TextTransform,
    *,
    cancellation: CancellationToken | None = None,
) -> DocxTransformation:
    """Transform visible Word text while copying every non-text resource unchanged."""
    validate_docx_container(source_path)
    check_cancelled(cancellation)
    try:
        with ZipFile(source_path) as source_archive:
            preserved_resources = sum(
                not info.is_dir() and info.filename.replace("\\", "/").startswith("word/media/")
                for info in source_archive.infolist()
            )
            parts = _read_xml_parts(source_archive, cancellation)
            paragraphs = [paragraph for part in parts for paragraph in part.paragraphs]
            if not paragraphs:
                raise ConversionError(f"{source_path.name} no contiene texto editable.")
            payload, markers = _build_payload(paragraphs)
            check_cancelled(cancellation)
            transformed_payload = transform(payload)
            check_cancelled(cancellation)
            transformed_paragraphs = _split_transformed_payload(transformed_payload, markers)
            for paragraph, transformed_text in zip(
                paragraphs,
                transformed_paragraphs,
                strict=True,
            ):
                _redistribute_text(paragraph.nodes, paragraph.source, transformed_text)
            content = _rebuild_archive(source_archive, parts, cancellation)
    except (BadZipFile, OSError) as exc:
        raise ConversionError(
            f"No se pudo reconstruir el documento DOCX {source_path.name}."
        ) from exc
    return DocxTransformation(
        content=content,
        source_text="\n\n".join(paragraph.source for paragraph in paragraphs),
        transformed_text="\n\n".join(transformed_paragraphs),
        paragraph_count=len(paragraphs),
        preserved_resources=preserved_resources,
        source_paragraphs=tuple(paragraph.source for paragraph in paragraphs),
        transformed_paragraphs=tuple(transformed_paragraphs),
    )


def rebuild_docx_with_paragraphs(
    source_path: Path,
    paragraphs_text: tuple[str, ...],
    *,
    cancellation: CancellationToken | None = None,
) -> bytes:
    """Rebuild a DOCX from reviewed text while retaining its original package."""
    validate_docx_container(source_path)
    check_cancelled(cancellation)
    try:
        with ZipFile(source_path) as source_archive:
            parts = _read_xml_parts(source_archive, cancellation)
            paragraphs = [paragraph for part in parts for paragraph in part.paragraphs]
            if len(paragraphs_text) != len(paragraphs):
                raise ConversionError(
                    "La revisión cambió el número de párrafos de Word. "
                    "Restaura el texto o revisa el documento como Markdown."
                )
            for paragraph, reviewed_text in zip(
                paragraphs,
                paragraphs_text,
                strict=True,
            ):
                _redistribute_text(paragraph.nodes, paragraph.source, reviewed_text)
            return _rebuild_archive(source_archive, parts, cancellation)
    except (BadZipFile, OSError) as exc:
        raise ConversionError(
            f"No se pudo reconstruir el documento DOCX {source_path.name}."
        ) from exc


def split_docx_payload(payload: str, paragraph_count: int) -> tuple[str, ...]:
    """Recover visible paragraphs from the marker-preserving transform payload."""
    if paragraph_count < 1:
        return ()
    markers = tuple(_MARKER_TEMPLATE.format(index=index) for index in range(paragraph_count - 1))
    return tuple(_split_transformed_payload(payload, markers))


def _read_xml_parts(
    archive: ZipFile,
    cancellation: CancellationToken | None,
) -> list[_XmlPart]:
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        recover=False,
        remove_blank_text=False,
        huge_tree=False,
    )
    parts: list[_XmlPart] = []
    for info in archive.infolist():
        check_cancelled(cancellation)
        name = info.filename.replace("\\", "/")
        if not _TRANSFORMABLE_PART.fullmatch(name):
            continue
        try:
            root = etree.fromstring(archive.read(info), parser=parser)
        except (etree.XMLSyntaxError, ValueError) as exc:
            raise ConversionError(
                f"La parte interna {Path(name).name} del DOCX no es válida."
            ) from exc
        paragraphs: list[_Paragraph] = []
        for paragraph_node in root.iter(_PARAGRAPH_TAG):
            text_nodes = list(paragraph_node.iter(_TEXT_TAG))
            source = "".join(node.text or "" for node in text_nodes)
            if source:
                paragraphs.append(_Paragraph(text_nodes, source))
        parts.append(_XmlPart(name, root, paragraphs))
    return parts


def _build_payload(paragraphs: list[_Paragraph]) -> tuple[str, tuple[str, ...]]:
    markers = tuple(_MARKER_TEMPLATE.format(index=index) for index in range(len(paragraphs) - 1))
    pieces: list[str] = []
    for index, paragraph in enumerate(paragraphs):
        if index:
            pieces.extend(("\n\n", markers[index - 1], "\n\n"))
        pieces.append(paragraph.source)
    return "".join(pieces), markers


def _split_transformed_payload(payload: str, markers: tuple[str, ...]) -> list[str]:
    if not isinstance(payload, str) or not payload.strip() or "\0" in payload:
        raise TranslationError("La transformación devolvió texto Word no válido.")
    for marker in markers:
        if payload.count(marker) != 1:
            raise TranslationError(
                "La transformación no pudo conservar la estructura interna del documento Word."
            )
    paragraphs = [payload]
    for marker in markers:
        next_parts: list[str] = []
        for part in paragraphs:
            if marker in part:
                left, right = part.split(marker, 1)
                next_parts.extend((left.rstrip("\r\n"), right.lstrip("\r\n")))
            else:
                next_parts.append(part)
        paragraphs = next_parts
    return paragraphs


def _redistribute_text(
    nodes: list[etree._Element],
    source: str,
    transformed: str,
) -> None:
    if not nodes:
        return
    if len(nodes) == 1 or not source:
        pieces = [transformed, *("" for _ in nodes[1:])]
    else:
        pieces = _proportional_pieces(nodes, source, transformed)
    for node, piece in zip(nodes, pieces, strict=True):
        node.text = piece
        if piece[:1].isspace() or piece[-1:].isspace():
            node.set(_SPACE_ATTRIBUTE, "preserve")
        elif node.get(_SPACE_ATTRIBUTE) == "preserve":
            node.attrib.pop(_SPACE_ATTRIBUTE, None)


def _proportional_pieces(
    nodes: list[etree._Element],
    source: str,
    transformed: str,
) -> list[str]:
    original_lengths = [len(node.text or "") for node in nodes]
    source_length = max(len(source), 1)
    pieces: list[str] = []
    cursor = 0
    cumulative = 0
    for length in original_lengths[:-1]:
        cumulative += length
        desired = round(len(transformed) * cumulative / source_length)
        boundary = _nearest_word_boundary(transformed, desired, cursor)
        pieces.append(transformed[cursor:boundary])
        cursor = boundary
    pieces.append(transformed[cursor:])
    return pieces


def _nearest_word_boundary(text: str, desired: int, minimum: int) -> int:
    desired = max(minimum, min(desired, len(text)))
    if desired in {minimum, len(text)} or text[desired - 1 : desired].isspace():
        return desired
    for distance in range(1, 25):
        after = desired + distance
        if after <= len(text) and text[after - 1 : after].isspace():
            return after
        before = desired - distance
        if before > minimum and text[before - 1 : before].isspace():
            return before
    return desired


def _rebuild_archive(
    source_archive: ZipFile,
    parts: list[_XmlPart],
    cancellation: CancellationToken | None,
) -> bytes:
    transformed_parts = {
        part.name: etree.tostring(
            part.root,
            encoding="UTF-8",
            xml_declaration=True,
            standalone=None,
        )
        for part in parts
    }
    destination = BytesIO()
    with ZipFile(destination, "w") as target_archive:
        target_archive.comment = source_archive.comment
        for info in source_archive.infolist():
            check_cancelled(cancellation)
            name = info.filename.replace("\\", "/")
            target_archive.writestr(info, transformed_parts.get(name, source_archive.read(info)))
    return destination.getvalue()
