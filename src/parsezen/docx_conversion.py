"""DOCX to Markdown conversion with optional portable image resources."""

from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import cast
from zipfile import BadZipFile

import mammoth

from parsezen.document_model import (
    RESOURCE_REFERENCE_PREFIX,
    ConvertedDocument,
    ConvertedResource,
)
from parsezen.errors import ConversionError

_MAX_DOCX_IMAGES = 500
_MAX_IMAGE_BYTES = 50 * 1024 * 1024
_MAX_TOTAL_IMAGE_BYTES = 250 * 1024 * 1024
_IMAGE_MEDIA_TYPES = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
}

# Kept as patchable compatibility seams while their heavy implementations stay lazy.
MarkItDown: object | None = None
pre_process_docx: object | None = None


class _DeferredMarkItDownException(Exception):
    pass


MarkItDownException: type[Exception] = _DeferredMarkItDownException


def _load_markitdown() -> tuple[object, object]:
    global MarkItDown, MarkItDownException, pre_process_docx
    if MarkItDown is None:
        from markitdown import MarkItDown as converter_type
        from markitdown import MarkItDownException as converter_error

        MarkItDown = converter_type
        MarkItDownException = converter_error
    if pre_process_docx is None:
        from markitdown.converter_utils.docx.pre_process import (
            pre_process_docx as preprocess,
        )

        pre_process_docx = preprocess
    return MarkItDown, pre_process_docx


def convert_docx(source_path: Path) -> ConvertedDocument:
    """Convert a validated DOCX while retaining common embedded images in reading order."""
    converter_type, preprocess = _load_markitdown()

    resources: list[ConvertedResource] = []
    content_to_reference: dict[str, str] = {}
    total_bytes = 0

    @mammoth.images.img_element
    def collect_supported_image(image: object) -> dict[str, str]:
        nonlocal total_bytes
        content_type = str(getattr(image, "content_type", "")).casefold()
        suffix = _IMAGE_MEDIA_TYPES.get(content_type)
        if suffix is None:
            raise AssertionError("Only supported images reach this converter.")
        if len(resources) >= _MAX_DOCX_IMAGES:
            raise ConversionError("El DOCX contiene más de 500 imágenes.")
        try:
            with image.open() as image_stream:  # type: ignore[attr-defined]
                content = image_stream.read(_MAX_IMAGE_BYTES + 1)
        except OSError as exc:
            raise ConversionError("No se pudo leer una imagen integrada en el DOCX.") from exc
        if len(content) > _MAX_IMAGE_BYTES:
            raise ConversionError("Una imagen del DOCX supera el límite de 50 MiB.")
        digest = sha256(content).hexdigest()
        existing = content_to_reference.get(digest)
        if existing is not None:
            return {"src": existing}
        total_bytes += len(content)
        if total_bytes > _MAX_TOTAL_IMAGE_BYTES:
            raise ConversionError("Las imágenes del DOCX superan el límite conjunto de 250 MiB.")
        relative_path = PurePosixPath(f"docx/image-{len(resources) + 1:03d}{suffix}")
        reference = f"{RESOURCE_REFERENCE_PREFIX}{relative_path.as_posix()}"
        resources.append(
            ConvertedResource(
                relative_path,
                content,
                "image/jpeg" if content_type == "image/jpg" else content_type,
            )
        )
        content_to_reference[digest] = reference
        return {"src": reference}

    def collect_image(image: object) -> list[object]:
        content_type = str(getattr(image, "content_type", "")).casefold()
        if content_type not in _IMAGE_MEDIA_TYPES:
            # Word documents often contain legacy previews (EMF/WMF). They should not
            # prevent the useful text and supported images from being converted.
            return []
        return cast(list[object], collect_supported_image(image))

    try:
        with source_path.open("rb") as source_stream:
            prepared_stream = preprocess(source_stream)  # type: ignore[operator]
        html_result = mammoth.convert_to_html(
            prepared_stream,
            convert_image=collect_image,
        )
        markdown_result = converter_type(enable_plugins=False).convert_stream(  # type: ignore[operator]
            BytesIO(html_result.value.encode("utf-8")),
            file_extension=".html",
            strict=True,
        )
    except ConversionError:
        raise
    except (BadZipFile, KeyError, MarkItDownException, OSError, ValueError) as exc:
        raise ConversionError(f"No se pudo convertir {source_path.name} con MarkItDown.") from exc
    if not isinstance(markdown_result.markdown, str):
        raise ConversionError("MarkItDown devolvió un resultado no válido.")
    return ConvertedDocument(markdown_result.markdown, tuple(resources))
