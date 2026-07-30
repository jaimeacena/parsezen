"""DOCX to Markdown conversion with optional portable image resources."""

from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import cast
from zipfile import BadZipFile

import mammoth
from markitdown import MarkItDown, MarkItDownException
from markitdown.converter_utils.docx.pre_process import pre_process_docx

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


def convert_docx(source_path: Path) -> ConvertedDocument:
    """Convert a validated DOCX while retaining common embedded images in reading order."""
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
            prepared_stream = pre_process_docx(source_stream)
        html_result = mammoth.convert_to_html(
            prepared_stream,
            convert_image=collect_image,
        )
        markdown_result = MarkItDown(enable_plugins=False).convert_stream(
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
