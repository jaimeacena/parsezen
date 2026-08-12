"""Shared, Qt-free policy for supported document workflows."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from parsezen.conversion import EPUB_BUILD_INPUT_EXTENSIONS

IMAGE_CAPABLE_EXTENSIONS = frozenset({".md", ".markdown", ".docx", ".pdf", ".epub"})


class OutputFormat(StrEnum):
    """User-visible format produced by a document workflow."""

    MARKDOWN = "markdown"
    EPUB = "epub"


class WorkflowMode(StrEnum):
    """The two user intentions exposed by the desktop application."""

    CONVERT = "convert"
    IMPROVE = "improve"


@dataclass(frozen=True, slots=True)
class WorkflowOptions:
    """Only the user choices that alter the shape of a workflow."""

    improvement_enabled: bool = False
    clean: bool = False
    translate: bool = False
    review_content: bool = False
    review_structure: bool = False
    mode: WorkflowMode | None = None

    @property
    def content_revision(self) -> bool:
        """Keep the legacy clean flag compatible with the review workflow."""
        return self.clean or self.review_content

    @property
    def any_improvement(self) -> bool:
        return self.content_revision or self.review_structure or self.translate

    @property
    def resolved_mode(self) -> WorkflowMode:
        if self.mode is not None:
            return self.mode
        return WorkflowMode.IMPROVE if self.improvement_enabled else WorkflowMode.CONVERT


@dataclass(frozen=True, slots=True)
class WorkflowPlan:
    """Resolved capabilities and copy for one homogeneous batch selection."""

    extensions: frozenset[str]
    output_format: OutputFormat
    options: WorkflowOptions
    has_sources: bool
    all_epub: bool
    mixed_with_epub: bool
    epub_buildable: bool
    epub_rebuildable: bool
    output_available: bool
    direct_epub_translation: bool
    rebuilds_container: bool
    image_inclusion_available: bool
    image_options_visible: bool
    guidance: str
    semantic_issue: str | None

    @property
    def epub_label(self) -> str:
        if self.all_epub and self.direct_epub_translation and self.options.translate:
            return "EPUB traducido (.epub)"
        if self.all_epub and self.rebuilds_container:
            return "EPUB revisado (.epub)"
        return "Libro EPUB (.epub)"

    @property
    def epub_tooltip(self) -> str:
        if self.direct_epub_translation and self.options.translate:
            return "Traduce el texto y conserva la estructura del libro."
        if self.all_epub and self.rebuilds_container:
            return "Reconstruye el libro con los cambios que apruebes en la revisión."
        if self.epub_buildable:
            return "Crea un libro EPUB reajustable."
        return "No está disponible para esta combinación de documentos."

    def convert_to_markdown_for(self, extension: str) -> bool:
        """Return false only when an EPUB container is preserved directly."""
        normalized = _normalize_extension(extension)
        return not (
            normalized == ".epub"
            and self.output_format is OutputFormat.EPUB
            and self.direct_epub_translation
        )

    def action_text(self, count: int) -> str:
        """Return the primary action without duplicating format rules in the UI."""
        if self.options.review_content or self.options.review_structure:
            return f"Preparar revisión de {count} documentos" if count > 1 else "Preparar revisión"
        if self.direct_epub_translation and self.options.translate:
            return f"Traducir {count} EPUB" if count > 1 else "Traducir EPUB"
        if self.direct_epub_translation:
            return f"Personalizar {count} EPUB" if count > 1 else "Personalizar EPUB"
        if self.output_format is OutputFormat.EPUB:
            if self.options.translate:
                return f"Traducir y crear {count} EPUB" if count > 1 else "Traducir y crear EPUB"
            return f"Crear {count} EPUB" if count > 1 else "Crear EPUB"
        return f"Crear {count} Markdown" if count > 1 else "Crear Markdown"

    def ready_message(self, count: int) -> str:
        """Return the success-state explanation for a valid plan."""
        if self.options.review_content or self.options.review_structure:
            return (
                f"Listos {count} documentos para preparar su revisión."
                if count > 1
                else "Listo para preparar la revisión."
            )
        if self.direct_epub_translation and self.options.translate:
            return (
                f"Listos {count} EPUB para traducir conservando su formato."
                if count > 1
                else "Listo para traducir el EPUB conservando su formato."
            )
        if self.direct_epub_translation:
            return (
                f"Listos {count} EPUB para personalizar."
                if count > 1
                else "Listo para personalizar el EPUB."
            )
        if self.output_format is OutputFormat.EPUB:
            return (
                f"Listos {count} documentos para crear sus EPUB."
                if count > 1
                else "Listo para crear el EPUB."
            )
        return (
            f"Listos {count} documentos para crear sus Markdown."
            if count > 1
            else "Listo para crear el Markdown."
        )


def plan_workflow(
    extensions: Iterable[str],
    output_format: OutputFormat,
    *,
    options: WorkflowOptions | None = None,
) -> WorkflowPlan:
    """Resolve one deterministic plan shared by the UI and processing boundary."""
    if not isinstance(output_format, OutputFormat):
        raise TypeError("output_format must be an OutputFormat")

    normalized = frozenset(
        extension
        for raw_extension in extensions
        if (extension := _normalize_extension(raw_extension))
    )
    resolved_options = options or WorkflowOptions()
    has_sources = bool(normalized)
    all_epub = has_sources and normalized == {".epub"}
    mixed_with_epub = ".epub" in normalized and not all_epub
    epub_buildable = has_sources and all(
        extension in EPUB_BUILD_INPUT_EXTENSIONS for extension in normalized
    )
    epub_rebuildable = has_sources and all(
        extension in EPUB_BUILD_INPUT_EXTENSIONS or extension == ".epub" for extension in normalized
    )
    all_markdown = has_sources and normalized.issubset({".md", ".markdown"})
    wants_rebuild = resolved_options.content_revision or resolved_options.review_structure
    output_available = output_format is OutputFormat.MARKDOWN or (
        output_format is OutputFormat.EPUB
        and (epub_buildable or all_epub or (wants_rebuild and epub_rebuildable))
    )
    direct_epub = (
        all_epub
        and output_format is OutputFormat.EPUB
        and (
            resolved_options.translate
            or (not resolved_options.review_content and not resolved_options.review_structure)
        )
    )
    has_image_capable_source = any(
        extension in IMAGE_CAPABLE_EXTENSIONS for extension in normalized
    )
    generated_epub = (
        output_format is OutputFormat.EPUB
        and not direct_epub
        and ((epub_buildable and not all_epub) or (wants_rebuild and epub_rebuildable))
    )

    semantic_issue: str | None = None
    if (
        resolved_options.mode is WorkflowMode.CONVERT
        and all_markdown
        and output_format is OutputFormat.MARKDOWN
    ):
        semantic_issue = "Este documento ya es Markdown. Usa Mejorar si quieres modificarlo."
    elif (
        all_markdown
        and output_format is OutputFormat.MARKDOWN
        and not resolved_options.any_improvement
    ):
        semantic_issue = "Activa una mejora o elige otro formato para crear un resultado nuevo."
    elif resolved_options.mode is WorkflowMode.IMPROVE and not resolved_options.any_improvement:
        semantic_issue = "Activa Traducir, Corregir errores y ruido u Organizar estructura."
    elif direct_epub and resolved_options.clean:
        semantic_issue = (
            "Esta configuración antigua no puede conservar el EPUB. "
            "Usa Corregir errores y ruido para revisarlo y reconstruirlo."
        )
    elif resolved_options.improvement_enabled and not resolved_options.any_improvement:
        semantic_issue = "Activa Traducir, Corregir errores y ruido u Organizar estructura."

    guidance = ""
    if mixed_with_epub and not (
        resolved_options.resolved_mode is WorkflowMode.IMPROVE and wants_rebuild
    ):
        guidance = (
            "Para conservar un EPUB como libro traducido, procésalo separado de los demás formatos."
        )
    elif direct_epub:
        guidance = semantic_issue or (
            "La traducción conservará capítulos, enlaces, estilos e imágenes."
            if resolved_options.translate
            else "Podrás personalizar metadatos, portada, capítulos y contenido."
        )

    return WorkflowPlan(
        extensions=normalized,
        output_format=output_format,
        options=resolved_options,
        has_sources=has_sources,
        all_epub=all_epub,
        mixed_with_epub=mixed_with_epub,
        epub_buildable=epub_buildable,
        epub_rebuildable=epub_rebuildable,
        output_available=output_available,
        direct_epub_translation=direct_epub,
        rebuilds_container=generated_epub
        and any(extension in {".epub", ".docx"} for extension in normalized),
        image_inclusion_available=(
            has_image_capable_source and (output_format is OutputFormat.MARKDOWN or generated_epub)
        ),
        image_options_visible=(output_format is OutputFormat.MARKDOWN and has_image_capable_source),
        guidance=guidance,
        semantic_issue=semantic_issue,
    )


def _normalize_extension(extension: str) -> str:
    normalized = extension.strip().lower()
    if not normalized:
        return ""
    return normalized if normalized.startswith(".") else f".{normalized}"
