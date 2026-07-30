from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from uuid import UUID
from xml.etree import ElementTree
from zipfile import ZIP_STORED, ZipFile

import pytest

from parsezen.document_model import ConvertedResource
from parsezen.epub_builder import (
    EpubBookMetadata,
    build_epub,
    iter_epub_text_documents,
    plan_epub,
    validate_epub_archive,
    validate_epub_file,
)
from parsezen.errors import ConversionError


def _build(markdown: str, resources: tuple[ConvertedResource, ...] = ()):
    return build_epub(
        markdown,
        resources,
        EpubBookMetadata("Libro de prueba", "es", "Parsezen"),
        identifier=UUID(int=1),
        modified_at=datetime(2026, 7, 22, tzinfo=UTC),
    )


def _rewrite_archive(
    content: bytes,
    *,
    remove: frozenset[str] = frozenset(),
    replacements: dict[str, bytes] | None = None,
) -> bytes:
    output = BytesIO()
    replacements = replacements or {}
    with ZipFile(BytesIO(content)) as source, ZipFile(output, "w") as target:
        for info in source.infolist():
            if info.filename in remove:
                continue
            target.writestr(info, replacements.get(info.filename, source.read(info.filename)))
    return output.getvalue()


def test_epub_contains_required_package_navigation_and_reflowable_chapter() -> None:
    built = _build("# Título\n\nTexto con **énfasis**.")

    with ZipFile(BytesIO(built.content)) as archive:
        assert archive.namelist()[0] == "mimetype"
        assert archive.getinfo("mimetype").compress_type == ZIP_STORED
        assert archive.read("mimetype") == b"application/epub+zip"
        for name in (
            "META-INF/container.xml",
            "EPUB/package.opf",
            "EPUB/nav.xhtml",
            "EPUB/text/chapter-0001.xhtml",
        ):
            ElementTree.fromstring(archive.read(name))
        assert archive.read("EPUB/styles/book.css")
        package = archive.read("EPUB/package.opf").decode("utf-8")
        assert "Parsezen" in package
        assert "2026-07-22T00:00:00Z" in package
    assert built.chapter_count == 1
    assert built.integrity_report is not None
    assert built.integrity_report.verified
    assert built.integrity_report.ledger.headings == 1


def test_epub_file_validation_uses_the_staged_package(tmp_path: Path) -> None:
    path = tmp_path / "book.epub"
    path.write_bytes(_build("# Chapter\n\nContent.").content)

    validate_epub_file(path)

    path.write_bytes(b"not an epub")
    with pytest.raises(ConversionError, match="ZIP válido"):
        validate_epub_file(path)


def test_epub_archive_validation_rejects_missing_manifest_items() -> None:
    content = _build("# Chapter\n\nContent.").content
    damaged = _rewrite_archive(
        content,
        remove=frozenset({"EPUB/text/chapter-0001.xhtml"}),
    )

    with pytest.raises(ConversionError, match="no existe"):
        validate_epub_archive(damaged)


def test_epub_archive_validation_rejects_broken_internal_fragments() -> None:
    content = _build(
        '# Chapter\n\n[Working link](<#target>)\n\n<a id="target"></a>\n\nTarget.'
    ).content
    chapter_name = "EPUB/text/chapter-0001.xhtml"
    with ZipFile(BytesIO(content)) as archive:
        chapter = archive.read(chapter_name)
    damaged = _rewrite_archive(
        content,
        replacements={chapter_name: chapter.replace(b'href="#target"', b'href="#still-missing"')},
    )

    with pytest.raises(ConversionError, match="ancla inexistente"):
        validate_epub_archive(damaged)


def test_epub_archive_validation_rejects_navigation_without_a_toc() -> None:
    content = _build("# Chapter\n\nContent.").content
    navigation_name = "EPUB/nav.xhtml"
    with ZipFile(BytesIO(content)) as archive:
        navigation = archive.read(navigation_name)
    damaged = _rewrite_archive(
        content,
        replacements={
            navigation_name: navigation.replace(b'epub:type="toc"', b'epub:type="landmarks"')
        },
    )

    with pytest.raises(ConversionError, match="tabla de contenidos"):
        validate_epub_archive(damaged)


def test_epub_renders_github_table_and_preserves_local_image() -> None:
    resource = ConvertedResource(
        PurePosixPath("pdf/figura.jpg"),
        b"jpeg-data",
        "image/jpeg",
    )
    markdown = (
        "# Datos\n\n| Nombre | Valor |\n|---|---:|\n| Parsezen | 42 |\n\n"
        "![Figura](__parsezen_resources__/pdf/figura.jpg)"
    )

    built = _build(markdown, (resource,))
    chapter = next(iter_epub_text_documents(built.content))[1]

    assert "<table>" in chapter
    assert "<th>Nombre</th>" in chapter
    assert 'src="../images/pdf/figura.jpg"' in chapter
    with ZipFile(BytesIO(built.content)) as archive:
        assert archive.read("EPUB/images/pdf/figura.jpg") == b"jpeg-data"
    assert built.resource_count == 1


def test_epub_disables_raw_html_and_keeps_safe_internal_page_anchors() -> None:
    markdown = (
        '<script>alert("no")</script>\n\n'
        "[Ir a la página](<#page-2>)\n\n"
        '<a id="page-2"></a>\n\n## Destino'
    )

    chapter = next(iter_epub_text_documents(_build(markdown).content))[1]

    assert "<script>" not in chapter
    assert "&lt;script&gt;" in chapter
    assert 'href="#page-2"' in chapter
    assert 'id="page-2"' in chapter


def test_long_book_splits_at_headings_and_rewrites_cross_chapter_links() -> None:
    markdown = (
        "# First\n\n[Next](<#page-2>)\n\n"
        + "word " * 2_000
        + '\n\n# Second\n\n<a id="page-2"></a>\n\nDestination.'
    )

    built = _build(markdown)
    chapters = list(iter_epub_text_documents(built.content))

    assert built.chapter_count == 2
    assert 'href="chapter-0002.xhtml#page-2"' in chapters[0][1]
    assert 'id="page-2"' in chapters[1][1]


def test_epub_preview_plan_is_the_same_plan_used_by_the_builder() -> None:
    markdown = (
        "# First chapter\n\n"
        + "First section text. " * 120
        + "\n\n## Detail\n\nMore detail.\n\n"
        + "# Second chapter\n\nSecond section.\n"
    )

    plan = plan_epub(markdown, "Fallback")
    built = _build(markdown)

    assert [chapter.title for chapter in plan.chapters] == [
        "First chapter",
        "Second chapter",
    ]
    assert built.chapter_count == len(plan.chapters) == 2
    assert [(entry.level, entry.title) for entry in plan.outline] == [
        (1, "First chapter"),
        (2, "Detail"),
        (1, "Second chapter"),
    ]


def test_epub_clamps_heading_jumps_without_changing_visible_text() -> None:
    markdown = "## Book\n\n# Opening\n\n#### Author\n\n### Section\n\nBody."

    plan = plan_epub(markdown, "Fallback")
    chapter = next(iter_epub_text_documents(_build(markdown).content))[1]
    root = ElementTree.fromstring(chapter)
    levels = [
        int(element.tag.rsplit("}", 1)[-1][1])
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] in {"h1", "h2", "h3", "h4", "h5", "h6"}
    ]

    assert [(entry.level, entry.title) for entry in plan.outline] == [
        (2, "Book"),
        (1, "Opening"),
        (2, "Author"),
        (3, "Section"),
    ]
    assert levels == [2, 1, 2, 3]
    assert all(title in chapter for title in ("Book", "Opening", "Author", "Section"))


def test_epub_plan_uses_toc_titles_to_split_real_chapters_below_level_one() -> None:
    front_matter = (
        "Copyright 2026 Example Press. All rights reserved. "
        "This edition contains a deliberately substantial publication note. "
    ) * 4
    markdown = (
        "# Field Manual\n\n"
        f"{front_matter}\n\n"
        "# Contents\n\n"
        "Origins ........ 1\n\n"
        "Techniques ........ 12\n\n"
        "## Origins\n\n"
        + "Origins body. " * 180
        + "\n\n## Techniques\n\n"
        + "Techniques body. " * 180
        + "\n"
    )

    plan = plan_epub(markdown, "Fallback")

    assert plan.preferred_heading_level == 2
    assert [chapter.title for chapter in plan.chapters] == [
        "Field Manual",
        "Origins",
        "Techniques",
    ]


def test_pdf_provenance_markers_never_appear_in_epub_pages() -> None:
    markdown = "<!-- PZDOC PDF PAGE 1 -->\n\n# Chapter\n\nVisible text."

    chapter = next(iter_epub_text_documents(_build(markdown).content))[1]

    assert "PZDOC PDF PAGE" not in chapter
    assert "Visible text." in chapter


def test_epub_rejects_missing_or_unsafe_resources() -> None:
    with pytest.raises(ConversionError, match="Falta una imagen"):
        _build("![Figura](__parsezen_resources__/missing.png)")

    unsafe = ConvertedResource(PurePosixPath("../secret.png"), b"x", "image/png")
    with pytest.raises(ConversionError, match="ruta interna"):
        _build("Texto", (unsafe,))


def test_epub_marks_the_selected_resource_as_its_cover() -> None:
    cover_path = PurePosixPath("cover/front.jpg")
    cover = ConvertedResource(cover_path, b"cover-bytes", "image/jpeg")

    built = build_epub(
        "# Libro\n\nContenido.",
        (cover,),
        EpubBookMetadata(
            "Título personalizado",
            "es",
            "Autora",
            cover_path,
        ),
        identifier=UUID(int=2),
        modified_at=datetime(2026, 7, 22, tzinfo=UTC),
    )

    with ZipFile(BytesIO(built.content)) as archive:
        package = archive.read("EPUB/package.opf").decode("utf-8")
        assert "Título personalizado" in package
        assert "<dc:creator>Autora</dc:creator>" in package
        assert 'properties="cover-image"' in package
        assert '<itemref idref="cover-page"/>' in package
        assert archive.read("EPUB/images/cover/front.jpg") == b"cover-bytes"
        cover_page = archive.read("EPUB/text/cover.xhtml").decode("utf-8")
        assert 'src="../images/cover/front.jpg"' in cover_page


def test_epub_rejects_a_cover_that_is_not_in_its_resources() -> None:
    with pytest.raises(ConversionError, match="portada"):
        build_epub(
            "Contenido",
            (),
            EpubBookMetadata(
                "Libro",
                cover_resource=PurePosixPath("cover/missing.png"),
            ),
        )
