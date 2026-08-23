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
    classify_heading_role,
    iter_epub_text_documents,
    plan_epub,
    reconcile_generated_html_tables,
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
        stylesheet = archive.read("EPUB/styles/book.css").decode("utf-8")
        assert "break-inside: avoid" in stylesheet
        assert "overflow-wrap: anywhere" in stylesheet
        assert "padding-top: 0.5em" in stylesheet
        assert "page-break-inside: avoid" in stylesheet
        package = archive.read("EPUB/package.opf").decode("utf-8")
        assert "Parsezen" in package
        assert "2026-07-22T00:00:00Z" in package
    assert built.chapter_count == 1
    assert built.integrity_report is not None
    assert built.integrity_report.verified
    assert built.integrity_report.ledger.headings == 1


def test_epub_keeps_dense_numeric_index_references_literal() -> None:
    built = _build(
        "# Index\n\n"
        "259. 295, 304, 310, 317\n"
        "202. 204, 207, 208, 211, 213\n"
        "\n10. Read chapter 202.\n"
    )

    chapter = "\n".join(content for _name, content in iter_epub_text_documents(built.content))

    assert "259. 295, 304, 310, 317" in chapter
    assert "202. 204, 207, 208, 211, 213" in chapter
    assert '<ol start="259">' not in chapter
    assert '<ol start="10">' in chapter


def test_epub_replaces_xml_forbidden_controls_in_content_and_metadata() -> None:
    built = build_epub(
        "# Main\x1fTitle\n\nFirst\x01Second and third\x0bfourth.",
        (),
        EpubBookMetadata("Book\x01Title", "es", "Local\x1fAuthor"),
        identifier=UUID(int=1),
        modified_at=datetime(2026, 7, 22, tzinfo=UTC),
    )

    validate_epub_archive(built.content)
    with ZipFile(BytesIO(built.content)) as archive:
        chapter = archive.read("EPUB/text/chapter-0001.xhtml").decode("utf-8")
        package = archive.read("EPUB/package.opf").decode("utf-8")
    assert "Main Title" in chapter
    assert "First Second and third fourth." in chapter
    assert "Book Title" in package
    assert "Local Author" in package
    assert not any(ord(character) in {0x1, 0xB, 0x1F} for character in chapter + package)


def test_epub_preserves_extended_publication_metadata() -> None:
    built = build_epub(
        "# Chapter\n\nContent.",
        (),
        EpubBookMetadata(
            "Preserved book",
            "en",
            "Local Author",
            identifiers=("primary-id", "secondary-id"),
            publisher="Local Publisher",
            publication_date="2024-03-14",
        ),
        modified_at=datetime(2026, 7, 22, tzinfo=UTC),
    )

    with ZipFile(BytesIO(built.content)) as archive:
        package = archive.read("EPUB/package.opf").decode("utf-8")

    assert '<dc:identifier id="book-id">primary-id</dc:identifier>' in package
    assert "<dc:identifier>secondary-id</dc:identifier>" in package
    assert "<dc:publisher>Local Publisher</dc:publisher>" in package
    assert "<dc:date>2024-03-14</dc:date>" in package


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


def test_epub_renders_restricted_multiline_pdf_table_html() -> None:
    markdown = """# Datos

<table>
<thead><tr><th>Nombre</th><th>Notas</th></tr></thead>
<tbody><tr><td>Parsezen</td><td>Primera lÃ­nea<br>Segunda lÃ­nea</td></tr></tbody>
</table>
"""

    chapter = next(iter_epub_text_documents(_build(markdown).content))[1]

    assert "&lt;table&gt;" not in chapter
    assert "<table>" in chapter
    assert "<th>Nombre</th>" in chapter
    assert "Primera lÃ­nea<br />Segunda lÃ­nea" in chapter


def test_epub_renders_a_source_faithful_document_contents_table() -> None:
    markdown = """# Contenidos

<table class="document-toc">
<thead><tr><th class="toc-label">Entrada</th><th class="toc-folio">Página</th></tr></thead>
<tbody>
<tr><td class="toc-label toc-level-0"><strong>Parte uno</strong></td>
<td class="toc-folio">12</td></tr>
<tr><td class="toc-label toc-level-1"><em>Primera sección</em></td>
<td class="toc-folio">14</td></tr>
</tbody>
</table>
"""

    built = _build(markdown)
    chapter = next(iter_epub_text_documents(built.content))[1]

    assert '<table class="document-toc">' in chapter
    assert '<td class="toc-label toc-level-1"><em>Primera sección</em></td>' in chapter
    assert '<td class="toc-folio">14</td>' in chapter
    with ZipFile(BytesIO(built.content)) as archive:
        stylesheet = archive.read("EPUB/styles/book.css").decode("utf-8")
    assert ".document-toc .toc-level-1" in stylesheet
    assert "font-variant-numeric: tabular-nums" in stylesheet


def test_epub_escapes_a_bare_ampersand_inside_a_safe_generated_table() -> None:
    markdown = """# Contenidos

<table class="document-toc">
<thead><tr><th class="toc-label">Entrada</th><th class="toc-folio">Página</th></tr></thead>
<tbody><tr><td class="toc-label toc-level-0">Research & Practice</td>
<td class="toc-folio">12</td></tr></tbody>
</table>
"""

    chapter = next(iter_epub_text_documents(_build(markdown).content))[1]

    assert '<table class="document-toc">' in chapter
    assert "Research &amp; Practice" in chapter
    assert "&lt;table" not in chapter


def test_epub_rejects_an_invalid_internal_contents_table_instead_of_printing_its_html() -> None:
    markdown = """# Contenidos

<table class="document-toc">
<tbody><tr><td class="toc-label toc-level-0">Entrada</td></tr></tbody>
</table>
"""

    with pytest.raises(ConversionError, match="índice interno"):
        _build(markdown)


def test_generated_table_reconciliation_keeps_source_attributes_and_candidate_text() -> None:
    source = (
        '<table class="document-toc"><thead><tr><th class="toc-label">Entry</th>'
        '<th class="toc-folio">Page</th></tr></thead><tbody><tr>'
        '<td class="toc-label toc-level-0"><a href="#page-12">First lesson</a></td>'
        '<td class="toc-folio">12</td></tr></tbody></table>'
    )
    candidate = (
        '<table class="changed"><thead><tr><th class="changed">Entrada</th>'
        '<th class="toc-folio">Página</th></tr></thead><tbody><tr>'
        '<td class="toc-label toc-level-0"><a href="#wrong">Primera lección</a></td>'
        '<td class="toc-folio">12</td></tr></tbody></table>'
    )

    reconciled = reconcile_generated_html_tables(source, candidate)
    chapter = next(iter_epub_text_documents(_build(reconciled).content))[1]

    assert '<a href="#page-12">Primera lección</a>' in reconciled
    assert '<table class="document-toc">' in chapter
    assert "Primera lección" in chapter
    assert "Página" in chapter
    assert "#wrong" not in chapter
    assert 'class="changed"' not in chapter


def test_generated_table_reconciliation_preserves_source_when_nodes_do_not_align() -> None:
    source = (
        '<table class="document-toc"><thead><tr><th class="toc-label">Entry</th>'
        '<th class="toc-folio">Page</th></tr></thead><tbody><tr>'
        '<td class="toc-label toc-level-0">First lesson</td>'
        '<td class="toc-folio">12</td></tr></tbody></table>'
    )
    candidate = source.replace("First lesson", "<span>Primera lección</span>")

    assert reconcile_generated_html_tables(source, candidate) == source


def test_epub_keeps_untrusted_raw_table_html_disabled() -> None:
    markdown = """# Datos

<table>
<thead><tr><th onclick="alert('no')">Nombre</th></tr></thead>
<tbody><tr><td><script>alert('no')</script></td></tr></tbody>
</table>
"""

    chapter = next(iter_epub_text_documents(_build(markdown).content))[1]

    assert "<table>" not in chapter
    assert "<script>" not in chapter
    assert "&lt;table&gt;" in chapter
    assert "&lt;script&gt;" in chapter


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


def test_epub_keeps_the_label_when_an_internal_target_is_outside_the_selection() -> None:
    chapter = next(
        iter_epub_text_documents(
            _build("# Partial book\n\n[Chapter outside the selected pages](<#page-99>)").content
        )
    )[1]

    assert "Chapter outside the selected pages" in chapter
    assert 'href="#page-99"' not in chapter
    assert "<a" not in chapter


def test_epub_keeps_text_but_drops_relative_links_that_cannot_be_packaged() -> None:
    chapter = next(
        iter_epub_text_documents(
            _build(
                "# Imported PDF\n\n"
                "[Malformed website](<./zeland%C2%ADs.com>) and "
                "[external website](https://example.com)."
            ).content
        )
    )[1]

    assert "Malformed website" in chapter
    assert "zeland" not in chapter
    assert '<a href="https://example.com">external website</a>' in chapter


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


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Part II — The journey", "container"),
        ("Libro 3: La vuelta", "container"),
        ("Volume IV", "container"),
        ("Chapter 7 — Arrival", "chapter"),
        ("Capítulo 8: Regreso", "chapter"),
        ("Part", None),
        ("Chapter sevenfold", None),
    ],
)
def test_classifies_only_explicit_numbered_container_and_chapter_titles(
    title: str,
    expected: str | None,
) -> None:
    assert classify_heading_role(title) == expected


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
        assert '<reference type="cover" title="Portada" href="text/cover.xhtml"/>' in package
        assert archive.read("EPUB/images/cover/front.jpg") == b"cover-bytes"
        cover_page = archive.read("EPUB/text/cover.xhtml").decode("utf-8")
        assert 'src="../images/cover/front.jpg"' in cover_page
        assert 'xmlns:epub="http://www.idpf.org/2007/ops"' in cover_page
        assert '<body epub:type="cover">' in cover_page


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
