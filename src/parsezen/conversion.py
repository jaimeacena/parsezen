"""Local-only text reading and document conversion."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from urllib.parse import quote
from zipfile import BadZipFile, ZipFile, ZipInfo, is_zipfile

from parsezen.cancellation import CancellationToken, check_cancelled
from parsezen.document_model import (
    RESOURCE_REFERENCE_PREFIX,
    ConvertedDocument,
    ConvertedResource,
)
from parsezen.docx_conversion import convert_docx
from parsezen.errors import ConversionError
from parsezen.markdown_resources import collect_markdown_resources
from parsezen.pdf_conversion import (
    PdfPageRange,
    PdfProgressCallback,
    PdfQualityReport,
    PdfVisualArbiterFactory,
    convert_pdf,
    convert_pdf_document,
)

SUPPORTED_EXTENSIONS = frozenset({".txt", ".md", ".markdown", ".docx", ".pdf", ".epub"})
CONVERSION_REQUIRED_EXTENSIONS = frozenset({".docx", ".pdf", ".epub"})
EPUB_BUILD_INPUT_EXTENSIONS = frozenset({".txt", ".md", ".markdown", ".docx", ".pdf"})
MAX_DIRECT_TEXT_BYTES = 64 * 1024 * 1024
_DIRECT_TEXT_EXTENSIONS = frozenset({".txt", ".md", ".markdown"})
_REQUIRED_DOCX_MEMBERS = frozenset({"[Content_Types].xml", "word/document.xml"})
_MAX_DOCX_ARCHIVE_ENTRIES = 10_000
_MAX_DOCX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
_MAX_DOCX_MEMBER_BYTES = 128 * 1024 * 1024
_MAX_DOCX_COMPRESSION_RATIO = 1_000
_EPUB_ANCHOR_COMMENT_PATTERN = re.compile(r"<!--\s*PZDOC EPUB ANCHOR ([a-z0-9][a-z0-9-]*)\s*-->")
_RESOURCE_IMAGE_PATTERN = re.compile(
    r"!\[(?P<alt>(?:\\.|[^\]\\])*)\]"
    r"\(\s*<?" + re.escape(RESOURCE_REFERENCE_PREFIX) + r"[^)>\s]+>?"
    r"(?:\s+(?:\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|\((?:\\.|[^)])*\)))?\s*\)",
)


def convert_file(
    source_path: Path,
    on_ocr_start: Callable[[], None] | None = None,
    on_pdf_quality_report: Callable[[PdfQualityReport], None] | None = None,
    cancellation: CancellationToken | None = None,
    pdf_page_range: PdfPageRange | None = None,
    force_pdf_ocr: bool = False,
    on_pdf_progress: PdfProgressCallback | None = None,
    load_pdf_ocr_checkpoint: Callable[[int], str | None] | None = None,
    save_pdf_ocr_checkpoint: Callable[[int, str], bool] | None = None,
    load_pdf_page_checkpoint: Callable[[int], str | None] | None = None,
    save_pdf_page_checkpoint: Callable[[int, str], bool] | None = None,
    pdf_visual_arbiter_factory: PdfVisualArbiterFactory | None = None,
) -> str:
    """Return Markdown from a supported local file without writing output."""
    converted = convert_document(
        source_path,
        on_ocr_start=on_ocr_start,
        on_pdf_quality_report=on_pdf_quality_report,
        cancellation=cancellation,
        pdf_page_range=pdf_page_range,
        force_pdf_ocr=force_pdf_ocr,
        on_pdf_progress=on_pdf_progress,
        load_pdf_ocr_checkpoint=load_pdf_ocr_checkpoint,
        save_pdf_ocr_checkpoint=save_pdf_ocr_checkpoint,
        load_pdf_page_checkpoint=load_pdf_page_checkpoint,
        save_pdf_page_checkpoint=save_pdf_page_checkpoint,
        pdf_visual_arbiter_factory=pdf_visual_arbiter_factory,
        preserve_resources=False,
    )
    return materialize_converted_markdown(converted.markdown, None)


def convert_document(
    source_path: Path,
    on_ocr_start: Callable[[], None] | None = None,
    on_pdf_quality_report: Callable[[PdfQualityReport], None] | None = None,
    cancellation: CancellationToken | None = None,
    pdf_page_range: PdfPageRange | None = None,
    force_pdf_ocr: bool = False,
    on_pdf_progress: PdfProgressCallback | None = None,
    load_pdf_ocr_checkpoint: Callable[[int], str | None] | None = None,
    save_pdf_ocr_checkpoint: Callable[[int, str], bool] | None = None,
    load_pdf_page_checkpoint: Callable[[int], str | None] | None = None,
    save_pdf_page_checkpoint: Callable[[int, str], bool] | None = None,
    preserve_resources: bool = False,
    strict_resource_references: bool = False,
    pdf_visual_arbiter_factory: PdfVisualArbiterFactory | None = None,
) -> ConvertedDocument:
    """Return Markdown and portable resources without writing final output."""
    check_cancelled(cancellation)
    extension = source_path.suffix.lower()
    if extension in _DIRECT_TEXT_EXTENSIONS:
        markdown = _read_text(source_path)
        check_cancelled(cancellation)
        if preserve_resources and extension in {".md", ".markdown"}:
            return collect_markdown_resources(
                source_path,
                markdown,
                strict=strict_resource_references,
            )
        return ConvertedDocument(markdown)
    if extension == ".docx":
        validate_docx_container(source_path)
        converted = convert_docx(source_path)
        check_cancelled(cancellation)
        return converted if preserve_resources else without_internal_resource_images(converted)
    if extension == ".pdf":
        if preserve_resources:
            converted_pdf = convert_pdf_document(
                source_path,
                on_ocr_start=on_ocr_start,
                on_quality_report=on_pdf_quality_report,
                cancellation=cancellation,
                page_range=pdf_page_range,
                force_ocr=force_pdf_ocr,
                on_progress=on_pdf_progress,
                load_ocr_checkpoint=load_pdf_ocr_checkpoint,
                save_ocr_checkpoint=save_pdf_ocr_checkpoint,
                load_page_checkpoint=load_pdf_page_checkpoint,
                save_page_checkpoint=save_pdf_page_checkpoint,
                include_images=True,
                visual_arbiter_factory=pdf_visual_arbiter_factory,
            )
            temporary_directory = TemporaryDirectory(prefix="parsezen-pdf-resources-")
            try:
                return ConvertedDocument(
                    converted_pdf.markdown,
                    tuple(
                        ConvertedResource.from_path(
                            resource.relative_path,
                            _spill_pdf_resource(resource, temporary_directory),
                            resource.media_type,
                            owner=temporary_directory,
                        )
                        for resource in converted_pdf.resources
                    ),
                )
            except Exception:
                temporary_directory.cleanup()
                raise
        return ConvertedDocument(
            convert_pdf(
                source_path,
                on_ocr_start=on_ocr_start,
                on_quality_report=on_pdf_quality_report,
                cancellation=cancellation,
                page_range=pdf_page_range,
                force_ocr=force_pdf_ocr,
                on_progress=on_pdf_progress,
                load_ocr_checkpoint=load_pdf_ocr_checkpoint,
                save_ocr_checkpoint=save_pdf_ocr_checkpoint,
                load_page_checkpoint=load_pdf_page_checkpoint,
                save_page_checkpoint=save_pdf_page_checkpoint,
                visual_arbiter_factory=pdf_visual_arbiter_factory,
            )
        )
    if extension == ".epub":
        from parsezen.epub_conversion import convert_epub

        converted = convert_epub(source_path, cancellation=cancellation)
        return converted if preserve_resources else without_internal_resource_images(converted)
    raise ConversionError(f"El formato {extension or '(sin extensión)'} no está soportado.")


def _spill_pdf_resource(resource: object, temporary_directory: TemporaryDirectory) -> Path:
    """Move one PDF image to local temporary storage before it crosses the pipeline boundary."""
    relative_path = getattr(resource, "relative_path", None)
    content = getattr(resource, "content", None)
    if not isinstance(relative_path, PurePosixPath) or not isinstance(content, bytes):
        raise ConversionError("El recurso binario del PDF no es válido.")
    destination = Path(temporary_directory.name) / "resources" / relative_path.as_posix()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    return destination


def materialize_converted_markdown(
    markdown: str,
    resources_reference: str | None,
) -> str:
    """Resolve internal EPUB markers only when the final output name is known."""

    materialized = _EPUB_ANCHOR_COMMENT_PATTERN.sub(
        lambda match: f'<a id="{match.group(1)}"></a>',
        markdown,
    )
    if resources_reference is not None:
        encoded_directory = quote(resources_reference.replace("\\", "/"), safe="/._-~")
        materialized = materialized.replace(
            RESOURCE_REFERENCE_PREFIX,
            f"{encoded_directory}/",
        )
    return materialized


def without_internal_resource_images(converted: ConvertedDocument) -> ConvertedDocument:
    """Return a textual Markdown view without exposing internal resource placeholders."""

    def replacement(match: re.Match[str]) -> str:
        return match.group("alt").replace("\\]", "]").strip()

    markdown = _RESOURCE_IMAGE_PATTERN.sub(replacement, converted.markdown)
    return ConvertedDocument(markdown, ())


def _read_text(source_path: Path) -> str:
    try:
        if source_path.stat().st_size > MAX_DIRECT_TEXT_BYTES:
            raise ConversionError(
                f"{source_path.name} supera el límite de 64 MiB para texto o Markdown."
            )
        return source_path.read_text(encoding="utf-8-sig")
    except UnicodeError as exc:
        raise ConversionError(
            f"{source_path.name} no está codificado como UTF-8 o UTF-8 con BOM."
        ) from exc
    except OSError as exc:
        raise ConversionError(f"No se pudo leer {source_path.name}.") from exc


def validate_docx_container(source_path: Path) -> None:
    """Reject malformed or unsafe DOCX containers before any XML is read."""
    try:
        valid_container = is_zipfile(source_path)
    except OSError as exc:
        raise ConversionError(f"No se pudo abrir el documento DOCX {source_path.name}.") from exc
    if not valid_container:
        raise ConversionError(f"{source_path.name} no es un documento DOCX válido.")
    try:
        with ZipFile(source_path) as archive:
            infos = archive.infolist()
            normalized_names = _validated_docx_member_names(infos, source_path.name)
            missing_members = _REQUIRED_DOCX_MEMBERS.difference(normalized_names)
    except (BadZipFile, OSError) as exc:
        raise ConversionError(f"No se pudo abrir el contenedor DOCX {source_path.name}.") from exc
    if missing_members:
        raise ConversionError(f"{source_path.name} no contiene la estructura mínima de un DOCX.")


def _validated_docx_member_names(infos: list[ZipInfo], filename: str) -> set[str]:
    if len(infos) > _MAX_DOCX_ARCHIVE_ENTRIES:
        raise ConversionError(f"{filename} contiene demasiados archivos internos.")
    if sum(info.file_size for info in infos) > _MAX_DOCX_UNCOMPRESSED_BYTES:
        raise ConversionError(f"{filename} es demasiado grande una vez descomprimido.")

    normalized_names: set[str] = set()
    for info in infos:
        normalized = info.filename.replace("\\", "/")
        _validate_docx_member_path(normalized, normalized_names, filename)
        _validate_docx_member_size(info, filename)
        normalized_names.add(normalized)
    return normalized_names


def _validate_docx_member_path(
    normalized: str,
    existing_names: set[str],
    filename: str,
) -> None:
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or not normalized or normalized in existing_names:
        raise ConversionError(f"{filename} contiene rutas internas no válidas o repetidas.")


def _validate_docx_member_size(info: ZipInfo, filename: str) -> None:
    if info.flag_bits & 0x1:
        raise ConversionError(f"{filename} contiene archivos cifrados.")
    if info.file_size > _MAX_DOCX_MEMBER_BYTES:
        raise ConversionError(f"{filename} contiene un archivo interno demasiado grande.")
    unsafe_ratio = info.file_size and (
        not info.compress_size or info.file_size / info.compress_size > _MAX_DOCX_COMPRESSION_RATIO
    )
    if unsafe_ratio:
        raise ConversionError(f"{filename} contiene datos con una compresión no segura.")
