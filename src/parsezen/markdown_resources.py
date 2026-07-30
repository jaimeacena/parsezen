"""Collect local Markdown images for portable Markdown and EPUB results."""

from __future__ import annotations

import re
from hashlib import sha256
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit

from parsezen.document_model import (
    RESOURCE_REFERENCE_PREFIX,
    ConvertedDocument,
    ConvertedResource,
)
from parsezen.errors import ConversionError

_MAX_MARKDOWN_IMAGES = 500
_MAX_IMAGE_BYTES = 50 * 1024 * 1024
_MAX_TOTAL_IMAGE_BYTES = 250 * 1024 * 1024
_IMAGE_MEDIA_TYPES = {
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}
_INLINE_IMAGE_PATTERN = re.compile(
    r"!\[(?P<alt>(?:\\.|[^\]\\])*)\]"
    r"\(\s*(?P<destination><[^>\n]+>|(?:\\.|[^\s()\\])+?)"
    r"(?P<title>\s+(?:\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|\((?:\\.|[^)])*\)))?\s*\)",
)
_REFERENCE_IMAGE_PATTERN = re.compile(
    r"!\[(?P<alt>(?:\\.|[^\]\\])*)\]"
    r"(?:\[(?P<label>(?:\\.|[^\]\\])*)\]|(?P<shortcut>(?!\s*[([])))",
)
_REFERENCE_DEFINITION_PATTERN = re.compile(
    r"^(?P<prefix> {0,3}\[(?P<label>(?:\\.|[^\]\\])+)\]:[ \t]*)"
    r"(?P<destination><[^>\n]+>|[^\s]+)(?P<suffix>[^\n]*)$",
    re.MULTILINE,
)
_HTML_PICTURE_PATTERN = re.compile(r"<picture\b[^>]*>.*?</picture\s*>", re.IGNORECASE | re.DOTALL)
_HTML_IMAGE_PATTERN = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_HTML_ALT_PATTERN = re.compile(
    r"\balt\s*=\s*(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)')",
    re.IGNORECASE,
)
_OBSIDIAN_IMAGE_PATTERN = re.compile(
    r"!\[\[(?P<destination>[^\]|\n]+)(?:\|(?P<label>[^\]\n]*))?\]\]"
)


def collect_markdown_resources(
    source_path: Path,
    markdown: str,
    *,
    strict: bool = False,
) -> ConvertedDocument:
    """Collect safe local images, optionally requiring every image to be portable."""
    resources: list[ConvertedResource] = []
    source_to_reference: dict[str, str] = {}
    content_to_reference: dict[str, str] = {}
    total_bytes = 0

    def portable_reference(raw_destination: str) -> str | None:
        nonlocal total_bytes
        destination = _unwrapped_destination(raw_destination)
        existing = source_to_reference.get(destination)
        if existing is not None:
            return existing
        try:
            image_path, media_type, suffix = _resolve_local_image(source_path, destination)
        except ConversionError:
            if strict:
                raise
            return None
        try:
            size = image_path.stat().st_size
        except OSError as exc:
            raise ConversionError(f"No se pudo leer la imagen {image_path.name}.") from exc
        if size > _MAX_IMAGE_BYTES:
            raise ConversionError(f"La imagen {image_path.name} supera el límite de 50 MiB.")
        if len(resources) >= _MAX_MARKDOWN_IMAGES:
            raise ConversionError("El Markdown contiene más de 500 imágenes locales.")
        try:
            content = image_path.read_bytes()
        except OSError as exc:
            raise ConversionError(f"No se pudo leer la imagen {image_path.name}.") from exc
        digest = sha256(content).hexdigest()
        deduplicated = content_to_reference.get(digest)
        if deduplicated is not None:
            source_to_reference[destination] = deduplicated
            return deduplicated
        total_bytes += len(content)
        if total_bytes > _MAX_TOTAL_IMAGE_BYTES:
            raise ConversionError(
                "Las imágenes del Markdown superan el límite conjunto de 250 MiB."
            )
        relative_path = PurePosixPath(f"markdown/image-{len(resources) + 1:03d}{suffix}")
        resource = ConvertedResource(relative_path, content, media_type)
        resources.append(resource)
        reference = f"{RESOURCE_REFERENCE_PREFIX}{relative_path.as_posix()}"
        source_to_reference[destination] = reference
        content_to_reference[digest] = reference
        return reference

    def replace_inline(match: re.Match[str]) -> str:
        reference = portable_reference(match.group("destination"))
        if reference is None:
            return match.group(0)
        return f"![{match.group('alt')}](<{reference}>{match.group('title') or ''})"

    rewritten = _INLINE_IMAGE_PATTERN.sub(replace_inline, markdown)
    image_labels = _referenced_image_labels(markdown)

    def replace_definition(match: re.Match[str]) -> str:
        if _normalized_reference_label(match.group("label")) not in image_labels:
            return match.group(0)
        reference = portable_reference(match.group("destination"))
        if reference is None:
            return match.group(0)
        return f"{match.group('prefix')}<{reference}>{match.group('suffix')}"

    rewritten = _REFERENCE_DEFINITION_PATTERN.sub(replace_definition, rewritten)
    return ConvertedDocument(rewritten, tuple(resources))


def without_markdown_images(markdown: str) -> str:
    """Remove Markdown image embeds while retaining their useful alternative text."""

    def alternative_text(match: re.Match[str]) -> str:
        return match.group("alt").replace("\\]", "]").strip()

    def html_alternative_text(match: re.Match[str]) -> str:
        alt_match = _HTML_ALT_PATTERN.search(match.group(0))
        if alt_match is None:
            return ""
        return (alt_match.group("double") or alt_match.group("single") or "").strip()

    def obsidian_alternative_text(match: re.Match[str]) -> str:
        label = (match.group("label") or "").strip()
        return label if label and not label.isdecimal() else ""

    without_pictures = _HTML_PICTURE_PATTERN.sub(html_alternative_text, markdown)
    without_html = _HTML_IMAGE_PATTERN.sub(html_alternative_text, without_pictures)
    without_inline = _INLINE_IMAGE_PATTERN.sub(alternative_text, without_html)
    without_references = _REFERENCE_IMAGE_PATTERN.sub(alternative_text, without_inline)
    return _OBSIDIAN_IMAGE_PATTERN.sub(obsidian_alternative_text, without_references)


def without_markdown_resource(markdown: str, relative_path: PurePosixPath) -> str:
    """Remove embeds of one packaged image while leaving every other image intact."""

    expected = f"{RESOURCE_REFERENCE_PREFIX}{quote(relative_path.as_posix(), safe='/._-~')}"

    def remove_matching_image(match: re.Match[str]) -> str:
        if _unwrapped_destination(match.group("destination")) != expected:
            return match.group(0)
        return match.group("alt").replace("\\]", "]").strip()

    return _INLINE_IMAGE_PATTERN.sub(remove_matching_image, markdown)


def _referenced_image_labels(markdown: str) -> frozenset[str]:
    labels: set[str] = set()
    for match in _REFERENCE_IMAGE_PATTERN.finditer(markdown):
        label = match.group("label")
        if label is None or not label:
            label = match.group("alt")
        normalized = _normalized_reference_label(label)
        if normalized:
            labels.add(normalized)
    return frozenset(labels)


def _normalized_reference_label(label: str) -> str:
    return " ".join(label.replace("\\", "").split()).casefold()


def _unwrapped_destination(raw_destination: str) -> str:
    destination = raw_destination.strip()
    if destination.startswith("<") and destination.endswith(">"):
        destination = destination[1:-1]
    return destination.replace("\\ ", " ")


def _resolve_local_image(
    source_path: Path,
    destination: str,
) -> tuple[Path, str, str]:
    try:
        parsed = urlsplit(destination)
    except ValueError as exc:
        raise ConversionError("El Markdown contiene una referencia de imagen no válida.") from exc
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ConversionError(
            "Para crear un EPUB, las imágenes de Markdown deben ser archivos locales relativos."
        )
    decoded = unquote(parsed.path)
    if not decoded or "\0" in decoded or "\\" in decoded:
        raise ConversionError("El Markdown contiene una ruta de imagen no válida.")
    relative = PurePosixPath(decoded)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ConversionError(
            "Las imágenes de Markdown deben estar dentro de la carpeta del documento."
        )
    root = source_path.parent.resolve()
    candidate = root.joinpath(*relative.parts).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ConversionError(
            "Las imágenes de Markdown deben estar dentro de la carpeta del documento."
        ) from exc
    if not candidate.is_file():
        raise ConversionError(f"No se encontró la imagen local {relative.name}.")
    suffix = candidate.suffix.casefold()
    media_type = _IMAGE_MEDIA_TYPES.get(suffix)
    if media_type is None:
        raise ConversionError(f"La imagen {candidate.name} no usa PNG, JPEG, GIF, SVG o WebP.")
    return candidate, media_type, suffix
