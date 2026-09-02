from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from uuid import UUID
from xml.etree import ElementTree
from zipfile import ZIP_STORED, ZipFile

import pytest

from parsezen.document_model import ConvertedResource
from parsezen.epub_builder import (
    EPUB_CHAPTER_MARKER,
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


def test_epub_navigation_links_the_complete_heading_hierarchy() -> None:
    markdown = (
        "# First chapter\n\n"
        + "First section text. " * 120
        + "\n\n## Detail\n\nDetail body.\n\n### Deeper\n\nDeep body.\n\n"
        + "# Second chapter\n\nSecond body.\n"
    )

    built = _build(markdown)
    with ZipFile(BytesIO(built.content)) as archive:
        navigation = archive.read("EPUB/nav.xhtml").decode("utf-8")
        first_chapter = archive.read("EPUB/text/chapter-0001.xhtml").decode("utf-8")

    assert '<a href="text/chapter-0001.xhtml">First chapter</a><ol>' in navigation
    assert '<a href="text/chapter-0001.xhtml#section-0001-0002">Detail</a><ol>' in navigation
    assert '<a href="text/chapter-0001.xhtml#section-0001-0003">Deeper</a>' in navigation
    assert '<a href="text/chapter-0002.xhtml">Second chapter</a>' in navigation
    assert '<h2 id="section-0001-0002">Detail</h2>' in first_chapter
    assert '<h3 id="section-0001-0003">Deeper</h3>' in first_chapter


def test_epub_navigation_nests_printed_chapters_beneath_their_parts() -> None:
    toc = (
        '<table class="document-toc">'
        '<thead><tr><th class="toc-label">Entry</th>'
        '<th class="toc-folio">Page</th></tr></thead><tbody>'
        '<tr><td class="toc-label toc-level-0">Part I: Foundations</td>'
        '<td class="toc-folio">1</td></tr>'
        '<tr><td class="toc-label toc-level-1">Chapter 1: Origins</td>'
        '<td class="toc-folio">3</td></tr>'
        '<tr><td class="toc-label toc-level-1">Chapter 2: Practice</td>'
        '<td class="toc-folio">9</td></tr>'
        '<tr><td class="toc-label toc-level-0">Afterword</td>'
        '<td class="toc-folio">15</td></tr>'
        "</tbody></table>\n\n"
    )
    markdown = toc + f"\n\n{EPUB_CHAPTER_MARKER}\n\n".join(
        (
            "# Part I: Foundations\n\nOpening.",
            "# Chapter 1: Origins\n\nFirst body.",
            "# Chapter 2: Practice\n\nSecond body.",
            "# Afterword\n\nClosing.",
        )
    )

    built = _build(markdown)
    with ZipFile(BytesIO(built.content)) as archive:
        navigation = archive.read("EPUB/nav.xhtml").decode("utf-8")

    assert (
        '<a href="text/chapter-0001.xhtml">Part I: Foundations</a><ol>'
        '<li><a href="text/chapter-0002.xhtml">Chapter 1: Origins</a></li>'
        '<li><a href="text/chapter-0003.xhtml">Chapter 2: Practice</a></li>'
        "</ol>" in navigation
    )
    assert '<li><a href="text/chapter-0004.xhtml">Afterword</a></li>' in navigation


def test_epub_navigation_uses_explicit_part_roles_when_no_contents_depth_exists() -> None:
    markdown = f"\n\n{EPUB_CHAPTER_MARKER}\n\n".join(
        (
            "# Part I: Foundations\n\nOpening.",
            "# Chapter 1: Origins\n\nFirst body.",
            "# Chapter 2: Practice\n\nSecond body.",
        )
    )

    built = _build(markdown)
    with ZipFile(BytesIO(built.content)) as archive:
        navigation = archive.read("EPUB/nav.xhtml").decode("utf-8")

    assert (
        '<a href="text/chapter-0001.xhtml">Part I: Foundations</a><ol>'
        '<li><a href="text/chapter-0002.xhtml">Chapter 1: Origins</a></li>'
        '<li><a href="text/chapter-0003.xhtml">Chapter 2: Practice</a></li>'
        "</ol>" in navigation
    )


def test_epub_navigation_keeps_an_ambiguous_single_chapter_group_flat() -> None:
    markdown = f"\n\n{EPUB_CHAPTER_MARKER}\n\n".join(
        (
            "# Part I: Foundations\n\nOpening.",
            "# Chapter 1: Origins\n\nOnly chapter.",
            "# Afterword\n\nClosing.",
        )
    )

    built = _build(markdown)
    with ZipFile(BytesIO(built.content)) as archive:
        navigation = archive.read("EPUB/nav.xhtml").decode("utf-8")

    assert (
        '<li><a href="text/chapter-0001.xhtml">Part I: Foundations</a></li>'
        '<li><a href="text/chapter-0002.xhtml">Chapter 1: Origins</a></li>' in navigation
    )


def test_epub_outline_ignores_heading_examples_inside_fenced_code() -> None:
    markdown = "# Real chapter\n\n```markdown\n## Not a section\n```\n\n## Real section\n"

    plan = plan_epub(markdown, "Fallback")
    built = _build(markdown)
    with ZipFile(BytesIO(built.content)) as archive:
        navigation = archive.read("EPUB/nav.xhtml").decode("utf-8")

    assert [(entry.level, entry.title) for entry in plan.outline] == [
        (1, "Real chapter"),
        (2, "Real section"),
    ]
    assert "Not a section" not in navigation
    assert "# Not a section" in next(iter_epub_text_documents(built.content))[1]


def test_epub_navigation_prefers_headings_backed_by_the_printed_contents() -> None:
    markdown = (
        "# Manual\n\n"
        "# Contents\n\n"
        "First section ........ 10\n\n"
        "Second section ........ 20\n\n"
        "## First section\n\n"
        "Body.\n\n"
        "### Minor unlisted detail\n\n"
        "Detail.\n\n"
        "## Second section\n\n"
        "Final body.\n"
    )

    built = _build(markdown)
    with ZipFile(BytesIO(built.content)) as archive:
        navigation = archive.read("EPUB/nav.xhtml").decode("utf-8")
        chapter = archive.read("EPUB/text/chapter-0001.xhtml").decode("utf-8")

    assert ">First section</a>" in navigation
    assert ">Second section</a>" in navigation
    assert "Minor unlisted detail" not in navigation
    assert "Minor unlisted detail" in chapter


def test_epub_navigation_falls_back_to_shallow_headings_when_toc_matching_is_sparse() -> None:
    sections = "\n\n".join(f"## Section {index}\n\nBody {index}." for index in range(1, 21))
    markdown = f"# Manual\n\n# Contents\n\nSection 1 ........ 10\n\n{sections}\n"

    plan = plan_epub(markdown, "Fallback")

    assert len([entry for entry in plan.outline if entry.include_in_navigation]) == 20


def test_pdf_outline_backed_heading_survives_the_navigation_budget_but_not_the_epub_body() -> None:
    sections = []
    for index in range(1, 252):
        if index == 251:
            sections.append("<!-- PZDOC PDF OUTLINE 2 -->")
        sections.append(f"## Topic {index}\n\nBody {index}.")
    markdown = "# Manual\n\n" + "\n\n".join(sections)

    plan = plan_epub(markdown, "Fallback")
    built = _build(markdown)
    with ZipFile(BytesIO(built.content)) as archive:
        navigation = archive.read("EPUB/nav.xhtml").decode("utf-8")
        bodies = "".join(
            archive.read(name).decode("utf-8")
            for name in archive.namelist()
            if name.startswith("EPUB/text/chapter-")
        )

    included_titles = {entry.title for entry in plan.outline if entry.include_in_navigation}
    assert "Topic 251" in included_titles
    assert "Topic 250" not in included_titles
    assert ">Topic 251</a>" in navigation
    assert "PZDOC PDF OUTLINE" not in bodies


def test_reliable_pdf_outline_filters_unconfirmed_peer_headings_from_navigation() -> None:
    sections = []
    for index in range(1, 21):
        if index <= 8:
            sections.append("<!-- PZDOC PDF OUTLINE 2 -->")
        sections.append(f"## Topic {index}\n\nBody {index}.")
    markdown = "# Manual\n\n" + "\n\n".join(sections)

    plan = plan_epub(markdown, "Fallback")
    included_titles = {entry.title for entry in plan.outline if entry.include_in_navigation}

    assert "Topic 8" in included_titles
    assert "Topic 9" not in included_titles


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
        ("Chapter 7. Arrival", "chapter"),
        ("Chapter one, ‘The Trainings’, introduces the method", None),
        ("Part One contains several traditional lists", None),
        ("Part Three, so I will give it a short treatment here", None),
        ("Part One. An old dhamma friend explained the method to me", None),
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


def test_epub_plan_retains_unambiguous_printed_contents_depth() -> None:
    markdown = (
        '<table class="document-toc">\n'
        '<thead><tr><th class="toc-label">Entry</th>'
        '<th class="toc-folio">Page</th></tr></thead>\n<tbody>\n'
        '<tr><td class="toc-label toc-level-0">Foundations</td>'
        '<td class="toc-folio">1</td></tr>\n'
        '<tr><td class="toc-label toc-level-1"><em>First lesson</em></td>'
        '<td class="toc-folio">3</td></tr>\n'
        '<tr><td class="toc-label toc-level-0">Closing</td>'
        '<td class="toc-folio">9</td></tr>\n'
        "</tbody>\n</table>\n\n"
        "# Foundations\n\nBody.\n\n"
        f"{EPUB_CHAPTER_MARKER}\n\n# First lesson\n\nLesson.\n\n"
        f"{EPUB_CHAPTER_MARKER}\n\n# Closing\n\nEnd.\n"
    )

    plan = plan_epub(markdown, "Fallback")

    assert [(chapter.title, chapter.toc_level) for chapter in plan.chapters] == [
        ("Foundations", 0),
        ("First lesson", 1),
        ("Closing", 0),
    ]


def test_epub_plan_recovers_groups_from_a_visually_styled_flat_contents_table() -> None:
    toc = (
        '<table class="document-toc"><tbody>'
        '<tr><td class="toc-label toc-level-0"><strong>Introduction</strong></td></tr>'
        '<tr><td class="toc-label toc-level-0">I - Foundations</td></tr>'
        '<tr><td class="toc-label toc-level-0">First practice</td></tr>'
        '<tr><td class="toc-label toc-level-0">Second practice</td></tr>'
        '<tr><td class="toc-label toc-level-0">II - Application</td></tr>'
        '<tr><td class="toc-label toc-level-0">Third practice</td></tr>'
        '<tr><td class="toc-label toc-level-0">Fourth practice</td></tr>'
        '<tr><td class="toc-label toc-level-0"><strong>Afterword</strong></td></tr>'
        "</tbody></table>\n\n"
    )
    body = "Substantial reading text. " * 90
    markdown = (
        f"# Handbook\n\n{'Publication note. ' * 100}\n\n{toc}"
        f"## Introduction\n\nOpening.\n\n"
        f"### I - Foundations\n\nGroup opening.\n\n"
        f"## First practice\n\n{body}\n\n"
        f"## Second practice\n\n{body}\n\n"
        f"### II - Application\n\nGroup opening.\n\n"
        f"## Third practice\n\n{body}\n\n"
        f"## Fourth practice\n\n{body}\n\n"
        f"## Afterword\n\nClosing.\n"
    )

    plan = plan_epub(markdown, "Fallback")
    relevant = plan.chapters[-8:]

    assert [chapter.title for chapter in relevant] == [
        "Introduction",
        "I - Foundations",
        "First practice",
        "Second practice",
        "II - Application",
        "Third practice",
        "Fourth practice",
        "Afterword",
    ]
    assert [chapter.toc_level for chapter in relevant] == [0, 0, 1, 1, 0, 1, 1, 0]
    assert [chapter.role for chapter in relevant] == [
        "chapter",
        "container",
        "chapter",
        "chapter",
        "container",
        "chapter",
        "chapter",
        "chapter",
    ]


def test_epub_plan_repairs_mixed_indentation_inside_explicit_parts() -> None:
    toc = (
        '<table class="document-toc"><tbody>'
        '<tr><td class="toc-label toc-level-0">Part I: History</td></tr>'
        '<tr><td class="toc-label toc-level-1">Introduction</td></tr>'
        '<tr><td class="toc-label toc-level-0">1. Origins</td></tr>'
        '<tr><td class="toc-label toc-level-0">2. Development</td></tr>'
        '<tr><td class="toc-label toc-level-0">Part II: Profiles</td></tr>'
        '<tr><td class="toc-label toc-level-1"><strong>Profile I</strong></td></tr>'
        '<tr><td class="toc-label toc-level-1"><strong>Profile</strong> II</td></tr>'
        '<tr><td class="toc-label toc-level-0">Bibliography</td></tr>'
        "</tbody></table>\n\n"
    )
    headings = (
        "# Part I: History",
        "# Introduction",
        "# Chapter 1: Origins",
        "# Chapter 2: Development",
        "# Part II: Profiles",
        "# Profile I",
        "# Profile II",
        "# Bibliography",
    )
    markdown = toc + f"\n\n{EPUB_CHAPTER_MARKER}\n\n".join(
        f"{heading}\n\nBody." for heading in headings
    )

    plan = plan_epub(markdown, "Fallback")

    assert [chapter.toc_level for chapter in plan.chapters] == [0, 1, 1, 1, 0, 1, 1, 0]
    assert [chapter.role for chapter in plan.chapters] == [
        "container",
        "chapter",
        "chapter",
        "chapter",
        "container",
        "chapter",
        "chapter",
        "chapter",
    ]


def test_epub_plan_splits_an_indexed_part_heading_adjacent_to_a_label() -> None:
    toc = (
        '<table class="document-toc"><tbody>'
        '<tr><td class="toc-label toc-level-0">Part I: History</td></tr>'
        '<tr><td class="toc-label toc-level-1">Chapter 1: Origins</td></tr>'
        '<tr><td class="toc-label toc-level-0">Part II: Profiles</td></tr>'
        '<tr><td class="toc-label toc-level-1">Profile I</td></tr>'
        '<tr><td class="toc-label toc-level-1">Profile II</td></tr>'
        '<tr><td class="toc-label toc-level-0">Appendices</td></tr>'
        '<tr><td class="toc-label toc-level-1">Appendix note</td></tr>'
        "</tbody></table>\n\n"
    )
    body = "Substantial reading text. " * 100
    markdown = (
        f"{toc}## Part I: History\n\nOpening.\n\n"
        f"## Chapter 1: Origins\n\n{body}\n\nPart II:\n"
        f"### Part II: Profiles\n\nOpening.\n\n"
        f"## Profile I\n\n{body}\n\n## Profile II\n\n{body}\n\n"
        f"## Appendices\n\n## Appendix note\n\nClosing."
    )

    plan = plan_epub(markdown, "Fallback")
    relevant = plan.chapters[-7:]

    assert [chapter.title for chapter in relevant] == [
        "Part I: History",
        "Chapter 1: Origins",
        "Part II: Profiles",
        "Profile I",
        "Profile II",
        "Appendices",
        "Appendix note",
    ]
    assert [chapter.toc_level for chapter in relevant] == [0, 1, 0, 1, 1, 0, 1]


def test_epub_plan_matches_short_numbered_body_titles_to_longer_contents_labels() -> None:
    toc = (
        '<table class="document-toc"><tbody>'
        '<tr><td class="toc-label toc-level-0">Part I: History</td></tr>'
        '<tr><td class="toc-label toc-level-1">1. Ancient Egypt: The opening image</td></tr>'
        '<tr><td class="toc-label toc-level-0">Part II: Profiles</td></tr>'
        '<tr><td class="toc-label toc-level-1">Aries I</td></tr>'
        "</tbody></table>\n\n"
    )
    markdown = (
        f"{toc}# Part I: History\n\nOpening.\n\n"
        f"{EPUB_CHAPTER_MARKER}\n\n# Chapter 1: Ancient Egypt\n\nBody.\n\n"
        f"{EPUB_CHAPTER_MARKER}\n\n# Part II: Profiles\n\nOpening.\n\n"
        f"{EPUB_CHAPTER_MARKER}\n\n# Aries I: The Axe\n\nBody.\n"
    )

    plan = plan_epub(markdown, "Fallback")

    assert [(chapter.toc_level, chapter.role) for chapter in plan.chapters] == [
        (0, "container"),
        (1, "chapter"),
        (0, "container"),
        (1, "chapter"),
    ]


def test_epub_plan_uses_a_repeated_index_series_to_recover_one_noisy_ordinal() -> None:
    toc = (
        '<table class="document-toc"><tbody>'
        '<tr><td class="toc-label toc-level-0">Part I: Profiles</td></tr>'
        '<tr><td class="toc-label toc-level-1">Scorpio I</td></tr>'
        '<tr><td class="toc-label toc-level-1">Scorpio II</td></tr>'
        '<tr><td class="toc-label toc-level-1">Scorpio III</td></tr>'
        "</tbody></table>\n\n"
    )
    markdown = (
        f"{toc}# Part I: Profiles\n\nOpening.\n\n"
        f"{EPUB_CHAPTER_MARKER}\n\n# Scorpio IE An apparatus\n\nBody.\n"
    )

    plan = plan_epub(markdown, "Fallback")

    assert plan.chapters[-1].toc_level == 1
    assert plan.chapters[-1].role == "chapter"


def test_epub_plan_matches_a_unique_container_to_its_decorative_subtitle() -> None:
    toc = (
        '<table class="document-toc"><tbody>'
        '<tr><td class="toc-label toc-level-0">Part II: The Profiles</td></tr>'
        '<tr><td class="toc-label toc-level-1">Profile I</td></tr>'
        '<tr><td class="toc-label toc-level-1">Profile II</td></tr>'
        "</tbody></table>\n\n"
    )
    markdown = (
        f"{toc}# THE PROFILES\n\nOpening.\n\n"
        f"{EPUB_CHAPTER_MARKER}\n\n# Profile I: First image\n\nBody.\n"
    )

    plan = plan_epub(markdown, "Fallback")

    assert [(chapter.toc_level, chapter.role) for chapter in plan.chapters] == [
        (0, "container"),
        (1, "chapter"),
    ]


def test_reliable_contents_prevents_front_matter_and_unlisted_callouts_from_splitting() -> None:
    toc = (
        '<table class="document-toc"><tbody>'
        '<tr><td class="toc-label toc-level-0">First lesson</td></tr>'
        '<tr><td class="toc-label toc-level-0">Second lesson</td></tr>'
        "</tbody></table>\n\n"
    )
    body = "Substantial reading text. " * 100
    markdown = (
        f"# THE HANDBOOK\n\n## An author line\n\n{'Copyright note. ' * 100}\n\n{toc}"
        f"## First lesson\n\n{body}\n\n"
        f"## AN UNLISTED VISUAL CALLOUT\n\n{body}\n\n"
        f"## Second lesson\n\n{body}\n"
    )

    plan = plan_epub(markdown, "Fallback")
    included = [entry.title for entry in plan.outline if entry.include_in_navigation]

    assert [chapter.title for chapter in plan.chapters][-2:] == [
        "First lesson",
        "Second lesson",
    ]
    assert "## AN UNLISTED VISUAL CALLOUT" in plan.chapters[-2].markdown
    assert included == ["First lesson", "Second lesson"]


def test_epub_plan_uses_adjacent_subtitle_for_a_bare_numbered_chapter() -> None:
    markdown = "# CHAPTER 1\n\n## The Hierarchy of the Cosmos\n\nOpening body.\n"
    plan = plan_epub(markdown, "Fallback")
    built = _build(markdown)
    with ZipFile(BytesIO(built.content)) as archive:
        navigation = archive.read("EPUB/nav.xhtml").decode("utf-8")

    assert [chapter.title for chapter in plan.chapters] == [
        "CHAPTER 1 — The Hierarchy of the Cosmos"
    ]
    assert [entry.include_in_navigation for entry in plan.outline] == [True, False]
    assert ">CHAPTER 1 — The Hierarchy of the Cosmos</a>" in navigation
    assert ">CHAPTER 1</a>" not in navigation


def test_epub_plan_uses_adjacent_subtitle_across_internal_anchor_markers() -> None:
    plan = plan_epub(
        "# CHAPTER 1\n\n"
        '<a id="page-12"></a>\n\n'
        "<!-- PZDOC EPUB ANCHOR destination -->\n\n"
        "## The Hierarchy of the Cosmos\n\nOpening body.\n",
        "Fallback",
    )

    assert [chapter.title for chapter in plan.chapters] == [
        "CHAPTER 1 — The Hierarchy of the Cosmos"
    ]


def test_epub_plan_hides_a_repeated_bare_chapter_marker_inside_its_chapter() -> None:
    plan = plan_epub(
        "# CHAPTER 4 — Classification\n\n## CHAPTER 4\n\n## Benefic and Malefic\n",
        "Fallback",
    )

    assert [entry.title for entry in plan.outline if entry.include_in_navigation] == [
        "CHAPTER 4 — Classification",
        "Benefic and Malefic",
    ]


def test_epub_plan_keeps_explicit_step_sequence_at_one_navigation_level() -> None:
    toc = (
        '<table class="document-toc"><tbody>'
        '<tr><td class="toc-label toc-level-1">Step One: Start</td></tr>'
        '<tr><td class="toc-label toc-level-2">Step Two: Continue</td></tr>'
        '<tr><td class="toc-label toc-level-1">Step Three: Finish</td></tr>'
        "</tbody></table>\n\n"
    )
    plan = plan_epub(
        f"{toc}# CHAPTER 2\n\n## Step One: Start\n\n"
        "## Step Two: Continue\n\n## Step Three: Finish\n",
        "Fallback",
    )

    assert [
        entry.navigation_level for entry in plan.outline if entry.title.startswith("Step ")
    ] == [2, 2, 2]


def test_epub_plan_does_not_promote_toc_subsections_to_chapters() -> None:
    body = "Substantial body text. " * 100
    markdown = (
        "# Technical manual\n\n"
        "# Contents\n\n"
        "First detail ........ 10\n\n"
        "Second detail ........ 20\n\n"
        "Third detail ........ 30\n\n"
        "Fourth detail ........ 40\n\n"
        "## First module\n\n"
        f"{body}\n\n"
        "### First detail\n\n"
        f"{body}\n\n"
        "### Second detail\n\n"
        f"{body}\n\n"
        "## Second module\n\n"
        f"{body}\n\n"
        "### Third detail\n\n"
        f"{body}\n\n"
        "### Fourth detail\n\n"
        f"{body}\n"
    )

    plan = plan_epub(markdown, "Fallback")

    assert plan.preferred_heading_level == 2
    assert [chapter.title for chapter in plan.chapters] == [
        "Technical manual",
        "Second module",
    ]
    assert "## First module" in plan.chapters[0].markdown
    assert [entry.title for entry in plan.outline if entry.level == 3] == [
        "First detail",
        "Second detail",
        "Third detail",
        "Fourth detail",
    ]


def test_epub_plan_splits_numbered_toc_chapters_when_heading_density_has_no_preferred_level() -> (
    None
):
    toc = (
        '<table class="document-toc">\n'
        '<thead><tr><th class="toc-label">Entry</th>'
        '<th class="toc-folio">Page</th></tr></thead>\n<tbody>\n'
        '<tr><td class="toc-label toc-level-0">1. Introduction</td>'
        '<td class="toc-folio">1</td></tr>\n'
        '<tr><td class="toc-label toc-level-0">2. Practice</td>'
        '<td class="toc-folio">20</td></tr>\n'
        "</tbody>\n</table>\n\n"
    )
    middle = "\n\n".join(f"## Detail {index}\n\nShort body {index}." for index in range(1, 129))
    markdown = (
        f"# Manual\n\nFront.\n\n{toc}"
        f"## 1. Introduction\n\n{'Opening body. ' * 30}\n\n"
        f"{middle}\n\n## 2. Practice\n\n{'Practice body. ' * 30}\n"
    )

    plan = plan_epub(markdown, "Fallback")

    assert plan.preferred_heading_level is None
    assert [chapter.title for chapter in plan.chapters][-2:] == [
        "1. Introduction",
        "2. Practice",
    ]
    assert [chapter.role for chapter in plan.chapters][-2:] == ["chapter", "chapter"]


def test_epub_plan_forces_index_backed_chapter_after_a_short_part_page() -> None:
    toc = (
        '<table class="document-toc">\n'
        '<tbody><tr><td class="toc-label toc-level-0">Part II: Practice</td>'
        '<td class="toc-folio">20</td></tr>\n'
        '<tr><td class="toc-label toc-level-0">18. First exercise</td>'
        '<td class="toc-folio">21</td></tr></tbody>\n</table>\n\n'
    )
    markdown = (
        f"# Contents\n\n{toc}"
        "# Part II: Practice\n\nShort part introduction.\n\n"
        "## 18. First exercise\n\nExercise body.\n"
    )

    plan = plan_epub(markdown, "Fallback")

    assert [chapter.title for chapter in plan.chapters][-2:] == [
        "Part II: Practice",
        "18. First exercise",
    ]


def test_epub_plan_matches_a_numbered_body_heading_to_a_minor_index_ocr_error() -> None:
    toc = (
        '<table class="document-toc">\n'
        '<tbody><tr><td class="toc-label toc-level-0">44. Bodysurfifing</td>'
        '<td class="toc-folio">447</td></tr></tbody>\n</table>\n\n'
    )
    markdown = f"# Contents\n\n{toc}## 44. BODYSURFING\n\nBody.\n"

    plan = plan_epub(markdown, "Fallback")

    assert plan.chapters[-1].title == "44. BODYSURFING"
    assert plan.chapters[-1].role == "chapter"
    assert plan.chapters[-1].toc_level == 0


def test_epub_plan_splits_a_numbered_chapter_whose_title_starts_with_a_quote() -> None:
    toc = (
        '<table class="document-toc">\n'
        '<tbody><tr><td class="toc-label toc-level-0">31. The Three Doors</td>'
        '<td class="toc-folio">259</td></tr>\n'
        '<tr><td class="toc-label toc-level-0">32. “What Was That?”</td>'
        '<td class="toc-folio">265</td></tr></tbody>\n</table>\n\n'
    )
    markdown = (
        f"# Contents\n\n{toc}"
        "## 31. The Three Doors\n\nShort ending.\n\n"
        "## 32. “WHAT WAS THAT?”\n\nOpening body.\n"
    )

    plan = plan_epub(markdown, "Fallback")

    assert [chapter.title for chapter in plan.chapters][-2:] == [
        "31. The Three Doors",
        "32. “WHAT WAS THAT?”",
    ]
    assert plan.chapters[-1].role == "chapter"


def test_epub_keeps_one_large_semantic_chapter_instead_of_exposing_a_size_fragment() -> None:
    markdown = "# One chapter\n\n" + "Long body. " * 13_000 + "\n\n## A subsection\n\nEnd.\n"

    plan = plan_epub(markdown, "Fallback")

    assert [chapter.title for chapter in plan.chapters] == ["One chapter"]


def test_repeated_running_chapter_headings_do_not_fragment_the_epub() -> None:
    repeated = "\n\n".join(
        f"## Chapter 1 — Opening\n\nRepeated page body {index}. " + "word " * 45
        for index in range(260)
    )
    markdown = (
        "# Manual\n\nFront matter. "
        + "word " * 60
        + "\n\n"
        + repeated
        + "\n\n## Chapter 2 — Continuation\n\nFinal body."
    )

    plan = plan_epub(markdown, "Fallback")

    assert plan.preferred_heading_level is None
    assert [chapter.title for chapter in plan.chapters] == [
        "Manual",
        "Chapter 1 — Opening",
        "Chapter 2 — Continuation",
    ]


def test_dense_subsections_remain_navigation_inside_shallower_parts() -> None:
    subsection_count = 140
    first_half = "\n\n".join(
        f"## Topic {index}\n\nConcise section body {index}."
        for index in range(1, subsection_count // 2 + 1)
    )
    second_half = "\n\n".join(
        f"## Topic {index}\n\nConcise section body {index}."
        for index in range(subsection_count // 2 + 1, subsection_count + 1)
    )
    markdown = f"# Part 1 — Foundations\n\n{first_half}\n\n# Part 2 — Practice\n\n{second_half}\n"

    plan = plan_epub(markdown, "Fallback")

    assert plan.preferred_heading_level == 1
    assert [chapter.title for chapter in plan.chapters] == [
        "Part 1 — Foundations",
        "Part 2 — Practice",
    ]
    assert len([entry for entry in plan.outline if entry.level == 2]) == subsection_count
    assert len([entry for entry in plan.outline if entry.include_in_navigation]) == (
        subsection_count + 2
    )


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
