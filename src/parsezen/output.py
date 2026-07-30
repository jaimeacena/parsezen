"""Collision-free Markdown output policy."""

from __future__ import annotations

import os
from collections.abc import Callable
from itertools import count
from pathlib import Path, PurePosixPath
from tempfile import mkdtemp, mkstemp

from parsezen.conversion import materialize_converted_markdown
from parsezen.document_model import ConvertedResource
from parsezen.errors import OutputWriteError


def write_text_output(
    source_path: Path,
    text: str,
    output_directory: Path | None = None,
    *,
    validate_staged: Callable[[Path], None] | None = None,
) -> Path:
    """Write transformed UTF-8 text beside the source without overwriting anything."""
    directory = output_directory if output_directory is not None else source_path.parent
    for index in count(1):
        collision_suffix = "" if index == 1 else f"-{index}"
        destination = directory / f"{source_path.stem}{collision_suffix}.mended.txt"
        try:
            _write_exclusively(destination, text, validate_staged=validate_staged)
        except FileExistsError:
            continue
        return destination
    raise AssertionError("The collision search is intentionally unbounded.")


def write_docx_output(
    source_path: Path,
    content: bytes,
    output_directory: Path | None = None,
    *,
    validate_staged: Callable[[Path], None] | None = None,
) -> Path:
    """Write a transformed DOCX package without touching the original document."""
    directory = output_directory if output_directory is not None else source_path.parent
    for index in count(1):
        collision_suffix = "" if index == 1 else f"-{index}"
        destination = directory / f"{source_path.stem}{collision_suffix}.mended.docx"
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


def replace_text_output(
    destination: Path,
    text: str,
    *,
    validate_staged: Callable[[Path], None] | None = None,
) -> None:
    """Atomically replace one app-created text result after local review."""
    staged_path = _stage_markdown(destination.parent, text)
    temporary_path: Path | None = staged_path
    try:
        if validate_staged is not None:
            validate_staged(staged_path)
        os.replace(staged_path, destination)
        temporary_path = None
    except OSError as exc:
        raise OutputWriteError(f"No se pudo actualizar el resultado {destination.name}.") from exc
    finally:
        _remove_if_present(temporary_path)


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
        if destination.exists() or (
            resources_directory is not None and resources_directory.exists()
        ):
            continue
        resources_written = False
        try:
            if resources_directory is not None:
                _write_resource_directory(resources_directory, resources)
                resources_written = True
            resolved_markdown = materialize_converted_markdown(
                markdown,
                _resource_reference(destination.parent, resources_directory),
            )
            _write_exclusively(
                destination,
                resolved_markdown,
                validate_staged=validate_staged,
            )
        except FileExistsError:
            if resources_written:
                _remove_resource_directory(resources_directory)
            continue
        except Exception:
            if resources_written:
                _remove_resource_directory(resources_directory)
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
        if (
            final_path.exists()
            or (raw_path is not None and raw_path.exists())
            or (resources_directory is not None and resources_directory.exists())
        ):
            continue

        raw_written = False
        resources_written = False
        staged_resources: Path | None = None
        staged_raw: Path | None = None
        staged_final: Path | None = None
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
                improved_markdown,
                _resource_reference(final_path.parent, resources_directory),
            )
            if raw_path is not None:
                staged_raw = _stage_markdown(raw_path.parent, resolved_raw)
            staged_final = _stage_markdown(final_path.parent, resolved_improved)
            if validate_staged is not None:
                validate_staged(staged_final)

            if resources_directory is not None and staged_resources is not None:
                staged_resources.rename(resources_directory)
                staged_resources = None
                resources_written = True
            if raw_path is not None and staged_raw is not None:
                _publish_staged_file(staged_raw, raw_path)
                staged_raw = None
                raw_written = True
            _publish_staged_file(staged_final, final_path)
            staged_final = None
        except FileExistsError:
            if raw_written:
                _remove_if_present(raw_path)
            if resources_written:
                _remove_resource_directory(resources_directory)
            continue
        except Exception:
            if raw_written:
                _remove_if_present(raw_path)
            if resources_written:
                _remove_resource_directory(resources_directory)
            raise
        finally:
            _remove_resource_directory(staged_resources)
            _remove_if_present(staged_raw)
            _remove_if_present(staged_final)
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
