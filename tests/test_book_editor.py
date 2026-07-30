from io import BytesIO
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

import pytest
from defusedxml import ElementTree

from parsezen.application.book_editor import (
    BookEditor,
    create_book_from_markdown,
    publish_book,
)
from parsezen.document_model import ConvertedResource
from parsezen.epub_builder import EPUB_CHAPTER_MARKER, EpubBookMetadata
from parsezen.infrastructure.artifact_store import ArtifactStore


def reversible(payload: bytes) -> bytes:
    return bytes(value ^ 0x33 for value in payload)


def make_book(store: ArtifactStore):
    markdown = f"# First\n\nFirst body.\n\n{EPUB_CHAPTER_MARKER}\n\n# Second\n\nSecond body.\n"
    return create_book_from_markdown(
        markdown,
        (),
        EpubBookMetadata("Book", "en", "Author"),
        store,
        job_id="job",
    )


def test_book_editor_preserves_content_when_divisions_change(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    book = make_book(store)
    editor = BookEditor(book, store, job_id="job")
    second_id = book.spine[1]

    renamed = editor.rename(second_id, "A better title")
    nested = BookEditor(renamed, store, job_id="job").indent(second_id)
    assert nested.sections[0].children[0].title == "A better title"
    flattened = BookEditor(nested, store, job_id="job").outdent(second_id)
    merged = BookEditor(flattened, store, job_id="job").merge_with_previous(second_id)

    assert len(merged.spine) == 1
    html = BookEditor(merged, store, job_id="job").editable_html(merged.spine[0])
    assert "First body." in html
    assert "Second body." in html


def test_book_editor_sanitizes_active_content_and_publishes_epub(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    book = make_book(store)
    editor = BookEditor(book, store, job_id="job")
    first_id = book.spine[0]
    updated = editor.update_content(
        first_id,
        '<h1 onclick="bad()">First</h1><script>bad()</script>'
        '<p><a href="javascript:bad()">Safe text</a></p>',
    )

    content = publish_book(updated, store, job_id="job")

    with ZipFile(BytesIO(content)) as archive:
        assert archive.read("mimetype") == b"application/epub+zip"
        chapter = archive.read("EPUB/text/chapter-0001.xhtml")
        assert b"script" not in chapter
        assert b"onclick" not in chapter
        assert b"javascript:" not in chapter
        ElementTree.fromstring(chapter)
        nav = ElementTree.fromstring(archive.read("EPUB/nav.xhtml"))
        assert nav is not None


def test_book_editor_rewrites_legacy_cross_chapter_links_after_reordering(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    book = create_book_from_markdown(
        (
            "# Origin\n\n[Jump to destination](<#destination>)\n\n"
            f"{EPUB_CHAPTER_MARKER}\n\n"
            '# Destination\n\n<a id="destination"></a>\n\nReached.'
        ),
        (),
        EpubBookMetadata("Linked book", "en"),
        store,
        job_id="job",
    )
    origin_id, destination_id = book.spine
    origin = book.section(origin_id)
    legacy_xhtml = store.read_text("job", origin.xhtml_artifact_id).replace(
        "chapter-0002.xhtml",
        "chapter-002.xhtml",
    )
    legacy_record = store.put_text(
        job_id="job",
        text=legacy_xhtml,
        media_type="application/xhtml+xml",
    )
    legacy_sections = (
        origin.__class__(origin.id, origin.title, legacy_record.id),
        *(
            section.__class__(section.id, section.title, section.xhtml_artifact_id)
            for section in book.sections[1:]
        ),
    )
    legacy_book = book.__class__(
        book.metadata,
        legacy_sections,
        book.spine,
        book.resources,
        book.stylesheet_artifact_ids,
        book.cover_resource_id,
    )
    reordered = BookEditor(legacy_book, store, job_id="job").move(destination_id, -1)

    content = publish_book(reordered, store, job_id="job")

    assert reordered.spine == (destination_id, origin_id)
    with ZipFile(BytesIO(content)) as archive:
        destination = archive.read("EPUB/text/chapter-0001.xhtml").decode("utf-8")
        origin = archive.read("EPUB/text/chapter-0002.xhtml").decode("utf-8")
    assert 'id="destination"' in destination
    assert 'href="chapter-0001.xhtml#destination"' in origin
    assert 'href="chapter-002.xhtml#destination"' not in origin


def test_book_editor_add_split_move_and_validation_paths(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    book = make_book(store)
    editor = BookEditor(book, store, job_id="job")
    first_id, second_id = book.spine

    added = editor.add_after(first_id, "  Added   chapter ")
    added_id = added.spine[1]
    assert added.section(added_id).title == "Added chapter"
    moved = BookEditor(added, store, job_id="job").move(added_id, 1)
    assert moved.spine[-1] == added_id
    unchanged = BookEditor(moved, store, job_id="job").move(first_id, -1)
    assert unchanged is moved
    split = BookEditor(moved, store, job_id="job").split(
        second_id,
        "<p>First half.</p>",
        "<p>Second half.</p>",
        "Second half",
    )
    assert len(split.spine) == len(moved.spine) + 1

    with pytest.raises(ValueError, match="conservar contenido"):
        editor.split(first_id, "", "<p>Only second.</p>", "Invalid")
    with pytest.raises(ValueError, match="primer capítulo"):
        editor.merge_with_previous(first_id)
    with pytest.raises(ValueError, match="vacío"):
        editor.rename(first_id, "\0   ")
    with pytest.raises(KeyError):
        editor.rename("missing", "Title")
    with pytest.raises(KeyError):
        editor.add_after("missing", "Title")


def test_book_editor_rejects_invalid_or_unsafe_editable_payloads(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    book = make_book(store)
    editor = BookEditor(book, store, job_id="job")
    first_id = book.spine[0]

    with pytest.raises(ValueError, match="no es válido"):
        editor.update_content(first_id, "\0")

    invalid = store.put_text(
        job_id="job",
        text="<html><body><p>broken",
        media_type="application/xhtml+xml",
    )
    invalid_section = book.sections[0].__class__(
        first_id,
        book.sections[0].title,
        invalid.id,
    )
    invalid_book = book.__class__(
        book.metadata,
        (invalid_section, *book.sections[1:]),
        book.spine,
        book.resources,
        book.stylesheet_artifact_ids,
        book.cover_resource_id,
    )
    with pytest.raises(Exception, match="XHTML no válido"):
        BookEditor(invalid_book, store, job_id="job").editable_html(first_id)


def test_book_editor_preserves_resources_cover_and_nested_operations(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    image = ConvertedResource(PurePosixPath("images/cover.png"), b"png", "image/png")
    book = create_book_from_markdown(
        f"# First\n\nOne.\n\n{EPUB_CHAPTER_MARKER}\n\n# Second\n\nTwo.",
        (image,),
        EpubBookMetadata("Book", "en", cover_resource=image.relative_path),
        store,
        job_id="job",
    )
    first_id, second_id = book.spine
    nested = BookEditor(book, store, job_id="job").indent(second_id)
    added = BookEditor(nested, store, job_id="job").add_after(second_id, "Nested sibling")
    added_id = next(section_id for section_id in added.spine if section_id not in book.spine)
    moved = BookEditor(added, store, job_id="job").move(added_id, -1)
    outdented = BookEditor(moved, store, job_id="job").outdent(added_id)
    content = publish_book(outdented, store, job_id="job")

    assert book.cover_resource_id == "resource-0001"
    assert outdented.spine[-1] == added_id
    with ZipFile(BytesIO(content)) as archive:
        cover_name = next(name for name in archive.namelist() if name.endswith("images/cover.png"))
        assert archive.read(cover_name) == b"png"


def test_book_editor_can_replace_and_remove_the_cover(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    book = make_book(store)

    replaced = BookEditor(book, store, job_id="job").replace_cover(
        "selected.webp",
        b"webp-cover",
    )

    assert replaced.cover_resource_id is not None
    cover = next(
        resource for resource in replaced.resources if resource.id == replaced.cover_resource_id
    )
    assert cover.href == "images/cover.webp"
    assert cover.media_type == "image/webp"
    assert store.read("job", cover.payload_artifact_id) == b"webp-cover"
    assert BookEditor(replaced, store, job_id="job").remove_cover().cover_resource_id is None

    with pytest.raises(ValueError, match="PNG"):
        BookEditor(book, store, job_id="job").replace_cover("selected.bmp", b"cover")


def test_book_editor_preserves_identity_metadata_and_safe_styles(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    book = make_book(store)
    stylesheet = store.put_text(
        job_id="job",
        text='@import "https://example.invalid/theme.css";\np { color: teal; }',
        media_type="text/css",
    )
    edited = BookEditor(book, store, job_id="job").update_metadata(
        title="Edited Book",
        author="New Author",
        language="es-ES",
    )
    edited = edited.__class__(
        edited.metadata,
        edited.sections,
        edited.spine,
        edited.resources,
        (stylesheet.id,),
        edited.cover_resource_id,
    )

    first = publish_book(edited, store, job_id="job")
    second = publish_book(edited, store, job_id="job")

    with ZipFile(BytesIO(first)) as archive:
        package = archive.read("EPUB/package.opf").decode("utf-8")
        css = archive.read("EPUB/styles/book.css").decode("utf-8")
    with ZipFile(BytesIO(second)) as archive:
        repeated_package = archive.read("EPUB/package.opf").decode("utf-8")
    assert edited.metadata.identifier in package
    assert edited.metadata.identifier in repeated_package
    assert "Edited Book" in package
    assert "New Author" in package
    assert "@import" not in css
    assert "color: teal" in css


def test_book_publication_rejects_active_or_remote_xhtml(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    book = make_book(store)
    unsafe = store.put_text(
        job_id="job",
        text=(
            '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
            "<head><title>Unsafe</title></head>"
            '<body><img src="//example.invalid/image.png" /></body></html>'
        ),
        media_type="application/xhtml+xml",
    )
    section = book.sections[0].__class__(
        book.sections[0].id,
        book.sections[0].title,
        unsafe.id,
    )
    unsafe_book = book.__class__(
        book.metadata,
        (section, *book.sections[1:]),
        book.spine,
        book.resources,
        book.stylesheet_artifact_ids,
        book.cover_resource_id,
    )

    with pytest.raises(Exception, match="imagen remota"):
        publish_book(unsafe_book, store, job_id="job")


def test_book_publication_rejects_missing_local_images(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    book = make_book(store)
    missing = store.put_text(
        job_id="job",
        text=(
            '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
            "<head><title>Missing</title></head>"
            '<body><img src="../images/missing.png" /></body></html>'
        ),
        media_type="application/xhtml+xml",
    )
    section = book.sections[0].__class__(
        book.sections[0].id,
        book.sections[0].title,
        missing.id,
    )
    missing_book = book.__class__(
        book.metadata,
        (section, *book.sections[1:]),
        book.spine,
        book.resources,
        book.stylesheet_artifact_ids,
        book.cover_resource_id,
    )

    with pytest.raises(Exception, match="no está disponible"):
        publish_book(missing_book, store, job_id="job")
