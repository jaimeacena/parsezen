"""Collision-free Markdown output policy."""

from __future__ import annotations

import os
from collections.abc import Callable
from itertools import count
from pathlib import Path, PurePosixPath
from tempfile import mkdtemp, mkstemp

from parsezen.conversion import materialize_converted_markdown
from parsezen.document_model import ConvertedResource
from parsezen.domain.jobs import MarkdownOrganization
from parsezen.errors import OutputWriteError
from parsezen.markdown_export import (
    MarkdownChapter,
    prepare_markdown_export,
    rebase_relative_markdown_links,
)


def write_epub_translation_output(
    source_path: Path,
    content: bytes,
    language_code: str,
    output_directory: Path | None = None,
    *,
    validate_staged: Callable[[Path], None] | None = None,
) -> Path:
    """Write one translated EPUB without touching the original or an existing result."""
    return write_epub_output(
        source_path,
        content,
        output_directory,
        language_code=language_code,
        validate_staged=validate_staged,
    )


def write_epub_output(
    source_path: Path,
    content: bytes,
    output_directory: Path | None = None,
    *,
    output_stem: str | None = None,
    language_code: str | None = None,
    validate_staged: Callable[[Path], None] | None = None,
) -> Path:
    """Write one generated EPUB without overwriting the source or an existing result."""
    directory = output_directory if output_directory is not None else source_path.parent
    base_stem = output_stem if output_stem is not None else source_path.stem
    stem = f"{base_stem}.{language_code}" if language_code is not None else base_stem
    for index in count(1):
        suffix = "" if index == 1 else f"-{index}"
        destination = directory / f"{stem}{suffix}.epub"
        try:
            _write_bytes_exclusively(
                destination,
                content,
                validate_staged=validate_staged,
            )
        except FileExistsError:
            continue
        return destination
    raise AssertionError("The collision search is intentionally unbounded.")


def replace_markdown_output(
    destination: Path,
    markdown: str,
    *,
    organization: MarkdownOrganization,
    source_name: str,
    include_metadata: bool,
    include_page_references: bool,
    validate_staged: Callable[[Path], None] | None = None,
) -> None:
    """Replace a reviewed Markdown publication and keep its chapter companion in sync."""

    chapters_directory = destination.with_suffix(".chapters")
    export = prepare_markdown_export(
        markdown,
        organization=organization,
        source_name=source_name,
        include_metadata=include_metadata,
        include_page_references=include_page_references,
        chapter_directory=chapters_directory.name,
    )
    staged_chapters: Path | None = None
    staged_primary: Path | None = None
    backup_chapters: Path | None = None
    chapters_published = False
    manage_chapters = organization is MarkdownOrganization.BY_CHAPTER
    try:
        if export.chapters:
            staged_chapters = _stage_chapter_directory(
                chapters_directory.parent,
                export.chapters,
                None,
            )
        if validate_staged is not None:
            _validate_canonical_markdown(destination.parent, markdown, validate_staged)
        staged_primary = _stage_markdown(destination.parent, export.primary_markdown)

        if manage_chapters and chapters_directory.exists():
            backup_chapters = Path(
                mkdtemp(dir=chapters_directory.parent, prefix=".parsezen-previous-chapters-")
            )
            backup_chapters.rmdir()
            chapters_directory.rename(backup_chapters)
        if staged_chapters is not None:
            staged_chapters.rename(chapters_directory)
            staged_chapters = None
            chapters_published = True
        os.replace(staged_primary, destination)
        staged_primary = None
        _remove_resource_directory(backup_chapters)
    except (OSError, UnicodeError) as exc:
        if chapters_published:
            _remove_resource_directory(chapters_directory)
        if backup_chapters is not None and backup_chapters.exists():
            try:
                backup_chapters.rename(chapters_directory)
            except OSError:
                pass
        raise OutputWriteError(f"No se pudo actualizar el resultado {destination.name}.") from exc
    finally:
        _remove_resource_directory(staged_chapters)
        _remove_if_present(staged_primary)


def replace_binary_output(
    destination: Path,
    content: bytes,
    *,
    validate_staged: Callable[[Path], None] | None = None,
) -> None:
    """Atomically replace one app-created binary result after local review."""
    temporary_path: Path | None = None
    try:
        file_descriptor, temporary_name = mkstemp(
            dir=destination.parent,
            prefix=".parsezen-review-",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(file_descriptor, "wb") as output_file:
            output_file.write(content)
            output_file.flush()
            os.fsync(output_file.fileno())
        if validate_staged is not None:
            validate_staged(temporary_path)
        os.replace(temporary_path, destination)
        temporary_path = None
    except OSError as exc:
        raise OutputWriteError(f"No se pudo actualizar el resultado {destination.name}.") from exc
    finally:
        _remove_if_present(temporary_path)


def write_conversion_output(
    source_path: Path,
    markdown: str,
    output_directory: Path | None = None,
    *,
    output_stem: str | None = None,
    resources: tuple[ConvertedResource, ...] = (),
    image_output_directory: Path | None = None,
    validate_staged: Callable[[Path], None] | None = None,
    markdown_organization: MarkdownOrganization = MarkdownOrganization.SINGLE_FILE,
    markdown_include_metadata: bool = False,
    markdown_include_page_references: bool = False,
) -> Path:
    """Write a conversion result without overwriting any existing file."""
    directory = output_directory if output_directory is not None else source_path.parent
    stem = output_stem if output_stem is not None else source_path.stem

    for index in count(1):
        suffix = "" if index == 1 else f"-{index}"
        destination = directory / f"{stem}{suffix}.md"
        resources_directory = (
            (image_output_directory or directory) / f"{stem}{suffix}.assets" if resources else None
        )
        chapters_directory = directory / f"{stem}{suffix}.chapters"
        export = prepare_markdown_export(
            markdown,
            organization=markdown_organization,
            source_name=source_path.name,
            include_metadata=markdown_include_metadata,
            include_page_references=markdown_include_page_references,
            chapter_directory=chapters_directory.name,
        )
        has_chapters = bool(export.chapters)
        manage_chapters = markdown_organization is MarkdownOrganization.BY_CHAPTER
        derived_export = (
            has_chapters or markdown_include_metadata or markdown_include_page_references
        )
        if (
            destination.exists()
            or (resources_directory is not None and resources_directory.exists())
            or (manage_chapters and chapters_directory.exists())
        ):
            continue
        resources_written = False
        chapters_written = False
        try:
            if resources_directory is not None:
                _write_resource_directory(resources_directory, resources)
                resources_written = True
            if has_chapters:
                _write_chapter_directory(chapters_directory, export.chapters, resources_directory)
                chapters_written = True
            resolved_markdown = materialize_converted_markdown(
                export.primary_markdown,
                _resource_reference(destination.parent, resources_directory),
            )
            if derived_export and validate_staged is not None:
                _validate_canonical_markdown(destination.parent, markdown, validate_staged)
            _write_exclusively(
                destination,
                resolved_markdown,
                validate_staged=None if derived_export else validate_staged,
            )
        except FileExistsError:
            if resources_written:
                _remove_resource_directory(resources_directory)
            if chapters_written:
                _remove_resource_directory(chapters_directory)
            continue
        except Exception:
            if resources_written:
                _remove_resource_directory(resources_directory)
            if chapters_written:
                _remove_resource_directory(chapters_directory)
            raise
        return destination

    raise AssertionError("The collision search is intentionally unbounded.")


def write_improvement_outputs(
    source_path: Path,
    raw_markdown: str,
    improved_markdown: str,
    *,
    keep_raw: bool,
    output_directory: Path | None = None,
    output_stem: str | None = None,
    resources: tuple[ConvertedResource, ...] = (),
    image_output_directory: Path | None = None,
    validate_staged: Callable[[Path], None] | None = None,
    markdown_organization: MarkdownOrganization = MarkdownOrganization.SINGLE_FILE,
    markdown_include_metadata: bool = False,
    markdown_include_page_references: bool = False,
) -> tuple[Path, Path | None]:
    """Write a collision-free `.mended.md` and its optional paired `.raw.md`."""
    directory = output_directory if output_directory is not None else source_path.parent
    stem = output_stem if output_stem is not None else source_path.stem

    for index in count(1):
        suffix = "" if index == 1 else f"-{index}"
        final_path = directory / f"{stem}{suffix}.mended.md"
        raw_path = directory / f"{stem}{suffix}.raw.md" if keep_raw else None
        resources_directory = (
            (image_output_directory or directory) / f"{stem}{suffix}.assets" if resources else None
        )
        chapters_directory = directory / f"{stem}{suffix}.mended.chapters"
        export = prepare_markdown_export(
            improved_markdown,
            organization=markdown_organization,
            source_name=source_path.name,
            include_metadata=markdown_include_metadata,
            include_page_references=markdown_include_page_references,
            chapter_directory=chapters_directory.name,
        )
        has_chapters = bool(export.chapters)
        manage_chapters = markdown_organization is MarkdownOrganization.BY_CHAPTER
        derived_export = (
            has_chapters or markdown_include_metadata or markdown_include_page_references
        )
        if (
            final_path.exists()
            or (raw_path is not None and raw_path.exists())
            or (resources_directory is not None and resources_directory.exists())
            or (manage_chapters and chapters_directory.exists())
        ):
            continue

        raw_written = False
        resources_written = False
        staged_resources: Path | None = None
        staged_raw: Path | None = None
        staged_final: Path | None = None
        staged_chapters: Path | None = None
        chapters_written = False
        try:
            if resources_directory is not None:
                staged_resources = _stage_resource_directory(
                    resources_directory.parent,
                    resources,
                )
            resolved_raw = materialize_converted_markdown(
                raw_markdown,
                _resource_reference(final_path.parent, resources_directory),
            )
            resolved_improved = materialize_converted_markdown(
                export.primary_markdown,
                _resource_reference(final_path.parent, resources_directory),
            )
            if has_chapters:
                staged_chapters = _stage_chapter_directory(
                    chapters_directory.parent,
                    export.chapters,
                    resources_directory,
                )
            if raw_path is not None:
                staged_raw = _stage_markdown(raw_path.parent, resolved_raw)
            staged_final = _stage_markdown(final_path.parent, resolved_improved)
            if derived_export and validate_staged is not None:
                _validate_canonical_markdown(final_path.parent, improved_markdown, validate_staged)
            elif validate_staged is not None:
                validate_staged(staged_final)

            if resources_directory is not None and staged_resources is not None:
                staged_resources.rename(resources_directory)
                staged_resources = None
                resources_written = True
            if raw_path is not None and staged_raw is not None:
                _publish_staged_file(staged_raw, raw_path)
                staged_raw = None
                raw_written = True
            if has_chapters and staged_chapters is not None:
                staged_chapters.rename(chapters_directory)
                staged_chapters = None
                chapters_written = True
            _publish_staged_file(staged_final, final_path)
            staged_final = None
        except FileExistsError:
            if raw_written:
                _remove_if_present(raw_path)
            if resources_written:
                _remove_resource_directory(resources_directory)
            if chapters_written:
                _remove_resource_directory(chapters_directory)
            continue
        except Exception:
            if raw_written:
                _remove_if_present(raw_path)
            if resources_written:
                _remove_resource_directory(resources_directory)
            if chapters_written:
                _remove_resource_directory(chapters_directory)
            raise
        finally:
            _remove_resource_directory(staged_resources)
            _remove_if_present(staged_raw)
            _remove_if_present(staged_final)
            _remove_resource_directory(staged_chapters)
        return final_path, raw_path

    raise AssertionError("The collision search is intentionally unbounded.")


def _write_exclusively(
    destination: Path,
    markdown: str,
    *,
    validate_staged: Callable[[Path], None] | None = None,
) -> None:
    temporary_path: Path | None = None
    try:
        file_descriptor, temporary_name = mkstemp(
            dir=destination.parent,
            prefix=".parsezen-",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as output_file:
            output_file.write(markdown)
            output_file.flush()
            os.fsync(output_file.fileno())

        if validate_staged is not None:
            validate_staged(temporary_path)
        _publish_without_overwrite(temporary_path, destination)
        temporary_path = None
    except FileExistsError:
        raise
    except (OSError, UnicodeError) as exc:
        raise OutputWriteError(f"No se pudo escribir el resultado {destination.name}.") from exc
    finally:
        _remove_if_present(temporary_path)


def _resource_reference(markdown_parent: Path, resources_directory: Path | None) -> str | None:
    if resources_directory is None:
        return None
    try:
        reference = os.path.relpath(resources_directory, start=markdown_parent)
    except ValueError as exc:
        raise OutputWriteError(
            "La carpeta de imágenes debe estar en la misma unidad que el Markdown."
        ) from exc
    return Path(reference).as_posix()


def _stage_markdown(directory: Path, markdown: str) -> Path:
    temporary_path: Path | None = None
    try:
        file_descriptor, temporary_name = mkstemp(
            dir=directory,
            prefix=".parsezen-",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as output_file:
            output_file.write(markdown)
            output_file.flush()
            os.fsync(output_file.fileno())
        return temporary_path
    except (OSError, UnicodeError) as exc:
        _remove_if_present(temporary_path)
        raise OutputWriteError("No se pudo preparar el resultado para guardarlo.") from exc


def _publish_staged_file(temporary_path: Path, destination: Path) -> None:
    try:
        _publish_without_overwrite(temporary_path, destination)
    except FileExistsError:
        raise
    except OSError as exc:
        raise OutputWriteError(f"No se pudo escribir el resultado {destination.name}.") from exc


def _write_bytes_exclusively(
    destination: Path,
    content: bytes,
    *,
    validate_staged: Callable[[Path], None] | None = None,
) -> None:
    temporary_path: Path | None = None
    try:
        file_descriptor, temporary_name = mkstemp(
            dir=destination.parent,
            prefix=".parsezen-epub-",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(file_descriptor, "wb") as output_file:
            output_file.write(content)
            output_file.flush()
            os.fsync(output_file.fileno())

        if validate_staged is not None:
            validate_staged(temporary_path)
        _publish_without_overwrite(temporary_path, destination)
        temporary_path = None
    except FileExistsError:
        raise
    except OSError as exc:
        raise OutputWriteError(f"No se pudo escribir el resultado {destination.name}.") from exc
    finally:
        _remove_if_present(temporary_path)


def _publish_without_overwrite(temporary_path: Path, destination: Path) -> None:
    """Atomically expose complete content without creating an empty reservation."""

    try:
        os.link(temporary_path, destination)
    except FileExistsError:
        raise
    except OSError:
        if os.name != "nt":
            raise
        os.rename(temporary_path, destination)
        return
    temporary_path.unlink()


def _write_resource_directory(
    destination: Path,
    resources: tuple[ConvertedResource, ...],
) -> None:
    staging_directory: Path | None = None
    publishing_directory = False
    try:
        staging_directory = Path(mkdtemp(dir=destination.parent, prefix=".parsezen-assets-"))
        for resource in resources:
            relative_path = _validate_resource_path(resource.relative_path)
            resource_destination = staging_directory.joinpath(*relative_path.parts)
            resource_destination.parent.mkdir(parents=True, exist_ok=True)
            with resource_destination.open("xb") as resource_file:
                resource_file.write(resource.content)
                resource_file.flush()
                os.fsync(resource_file.fileno())
        publishing_directory = True
        staging_directory.rename(destination)
        staging_directory = None
    except FileExistsError as exc:
        if publishing_directory:
            raise
        raise OutputWriteError(
            f"El documento contiene rutas de imagen repetidas en {destination.stem}."
        ) from exc
    except OSError as exc:
        raise OutputWriteError(
            f"No se pudieron guardar las imágenes de {destination.stem}."
        ) from exc
    finally:
        _remove_resource_directory(staging_directory)


def _stage_resource_directory(
    parent: Path,
    resources: tuple[ConvertedResource, ...],
) -> Path:
    staging_directory: Path | None = None
    try:
        staging_directory = Path(mkdtemp(dir=parent, prefix=".parsezen-assets-"))
        for resource in resources:
            relative_path = _validate_resource_path(resource.relative_path)
            resource_destination = staging_directory.joinpath(*relative_path.parts)
            resource_destination.parent.mkdir(parents=True, exist_ok=True)
            with resource_destination.open("xb") as resource_file:
                resource_file.write(resource.content)
                resource_file.flush()
                os.fsync(resource_file.fileno())
        return staging_directory
    except FileExistsError as exc:
        _remove_resource_directory(staging_directory)
        raise OutputWriteError("El documento contiene rutas de imagen repetidas.") from exc
    except OSError as exc:
        _remove_resource_directory(staging_directory)
        raise OutputWriteError("No se pudieron preparar las imágenes del documento.") from exc


def _write_chapter_directory(
    destination: Path,
    chapters: tuple[MarkdownChapter, ...],
    resources_directory: Path | None,
) -> None:
    staging = _stage_chapter_directory(destination.parent, chapters, resources_directory)
    try:
        staging.rename(destination)
    except OSError as exc:
        _remove_resource_directory(staging)
        raise OutputWriteError("No se pudieron guardar los capítulos Markdown.") from exc


def _stage_chapter_directory(
    parent: Path,
    chapters: tuple[MarkdownChapter, ...],
    resources_directory: Path | None,
) -> Path:
    staging: Path | None = None
    try:
        staging = Path(mkdtemp(dir=parent, prefix=".parsezen-chapters-"))
        for chapter in chapters:
            resource_reference = _resource_reference(staging, resources_directory)
            content = materialize_converted_markdown(chapter.markdown, resource_reference)
            if resources_directory is None:
                content = rebase_relative_markdown_links(content)
            destination = staging / chapter.filename
            with destination.open("x", encoding="utf-8", newline="\n") as output_file:
                output_file.write(content)
                output_file.flush()
                os.fsync(output_file.fileno())
        return staging
    except (OSError, UnicodeError) as exc:
        _remove_resource_directory(staging)
        raise OutputWriteError("No se pudieron preparar los capítulos Markdown.") from exc


def _validate_canonical_markdown(
    directory: Path,
    markdown: str,
    validate_staged: Callable[[Path], None],
) -> None:
    staged = _stage_markdown(directory, markdown)
    try:
        validate_staged(staged)
    finally:
        _remove_if_present(staged)


def _validate_resource_path(relative_path: PurePosixPath) -> PurePosixPath:
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or any(part in {"", ".", ".."} for part in relative_path.parts)
    ):
        raise OutputWriteError("El documento contiene una ruta de imagen no válida.")
    return relative_path


def _remove_resource_directory(path: Path | None) -> None:
    if path is None or not path.exists():
        return
    try:
        descendants = sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True)
        for descendant in descendants:
            if descendant.is_dir():
                descendant.rmdir()
            else:
                descendant.unlink(missing_ok=True)
        path.rmdir()
    except OSError:
        pass


def _remove_if_present(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
