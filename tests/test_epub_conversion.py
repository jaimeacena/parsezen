from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest

import parsezen.epub_conversion as epub_conversion_module
import parsezen.processing as processing_module
from parsezen.conversion import RESOURCE_REFERENCE_PREFIX, convert_document, convert_file
from parsezen.epub_builder import EpubBookMetadata, build_epub
from parsezen.epub_conversion import (
    inspect_epub_package,
    replace_epub_metadata,
    translate_epub,
)
from parsezen.errors import ConversionError, RequestValidationError
from parsezen.improvement import ImprovementMode
from parsezen.output import write_improvement_outputs
from parsezen.processing import OutputFormat, ProcessRequest, ProcessStage, process_document
from parsezen.settings import AppSettings


def test_epub_literal_work_title_requires_full_emphasis() -> None:
    assert epub_conversion_module._epub_unit_is_fully_emphasized(
        '<p xmlns="urn:test"><i>THE QUIET PATH THROUGH INNER FREEDOM</i></p>'
    )
    assert not epub_conversion_module._epub_unit_is_fully_emphasized(
        '<p xmlns="urn:test">Context <i>THE QUIET PATH</i></p>'
    )


def test_epub_literal_work_title_requires_reference_section_context() -> None:
    title = '<p xmlns="urn:test"><i>THE QUIET PATH THROUGH INNER FREEDOM</i></p>'
    ordinary_units = [epub_conversion_module._EpubTranslationUnit("chapter.xhtml", (0,), title)]

    assert (
        epub_conversion_module._literal_epub_work_title_segments(
            ordinary_units,
            [title],
            source_language="en",
            target_language="es",
        )
        == frozenset()
    )

    bibliography_units = [
        epub_conversion_module._EpubTranslationUnit("references.xhtml", (0,), title),
        epub_conversion_module._EpubTranslationUnit(
            "references.xhtml",
            (1,),
            '<h1 xmlns="urn:test">BIBLIOGRAPHY</h1>',
        ),
        epub_conversion_module._EpubTranslationUnit("references.xhtml", (2,), title),
        epub_conversion_module._EpubTranslationUnit(
            "references.xhtml",
            (3,),
            '<h2 xmlns="urn:test">PRIMARY SOURCES</h2>',
        ),
        epub_conversion_module._EpubTranslationUnit("references.xhtml", (4,), title),
    ]

    assert epub_conversion_module._literal_epub_work_title_segments(
        bibliography_units,
        [unit.source for unit in bibliography_units],
        source_language="en",
        target_language="es",
    ) == frozenset({3, 5})


def test_epub_package_inspection_retains_all_publication_metadata(tmp_path: Path) -> None:
    source = tmp_path / "metadata.epub"
    _write_epub2(source)
    with ZipFile(source) as archive:
        package = archive.read("OEBPS/content.opf")
    package = package.replace(
        b'<dc:identifier id="book-id">small-book-id</dc:identifier>',
        (
            b'<dc:identifier id="book-id">small-book-id</dc:identifier>'
            b"<dc:identifier>secondary-id</dc:identifier>"
            b"<dc:publisher>Local Publisher</dc:publisher>"
            b"<dc:date>2024-03-14</dc:date>"
        ),
    )
    _rewrite_epub_members(source, {"OEBPS/content.opf": package})

    metadata = inspect_epub_package(source)

    assert metadata.identifier == "small-book-id"
    assert metadata.identifiers == ("small-book-id", "secondary-id")
    assert metadata.publisher == "Local Publisher"
    assert metadata.publication_date == "2024-03-14"


def test_epub_translation_prefers_detected_text_language_over_incorrect_metadata(
    tmp_path: Path,
) -> None:
    source = tmp_path / "incorrect-language.epub"
    source.write_bytes(
        build_epub(
            "# Chapter\n\n"
            "This complete English paragraph contains enough natural language to establish "
            "the actual source language even though the publication metadata is incorrect.",
            (),
            EpubBookMetadata("Book", "es", "Author"),
        ).content
    )

    translated = translate_epub(
        source,
        "es",
        lambda text, *_args: text.replace("Chapter", "Capítulo").replace(
            "This complete English paragraph contains enough natural language to establish "
            "the actual source language even though the publication metadata is incorrect.",
            "Este párrafo completo en inglés contiene suficiente lenguaje natural para "
            "establecer el idioma de origen real aunque los metadatos de publicación sean "
            "incorrectos.",
        ),
    )

    assert translated.quality_report is not None
    assert translated.quality_report.source_language == "en"


def test_epub2_conversion_restores_navigation_images_and_internal_links(
    tmp_path: Path,
) -> None:
    source = tmp_path / "small book.epub"
    _write_epub2(source)

    converted = convert_document(source, preserve_resources=True)

    assert converted.markdown.startswith("# Small Book")
    assert "## Chapter One" in converted.markdown
    assert "## Chapter Two" in converted.markdown
    assert "## Document Outline" in converted.markdown
    normalized_markdown = re.sub(r"\s+", " ", converted.markdown)
    assert (
        "A deliberately long promotional sentence that crosses a page boundary and asks readers "
        "to visit [www.example.com](https://example.com/split) for details." in normalized_markdown
    )
    assert "Visit [official site](https://example.com/book)" in converted.markdown
    assert re.findall(r"(?m)^# [^#].*$", converted.markdown) == ["# Small Book"]
    assert "[next chapter](#epub-document-2-anchor-1-start)" in converted.markdown
    assert "[official site](https://example.com/book)" in converted.markdown
    assert (
        f"![Cover]({RESOURCE_REFERENCE_PREFIX}OEBPS/images/cover%20photo.jpg)" in converted.markdown
    )
    assert "PZDOC EPUB ANCHOR" in converted.markdown
    assert len(converted.resources) == 1
    assert converted.resources[0].relative_path.as_posix() == "OEBPS/images/cover photo.jpg"
    assert converted.resources[0].content == b"fake-jpeg-content"

    plain_markdown = convert_file(source)
    assert "PZDOC EPUB ANCHOR" not in plain_markdown
    assert RESOURCE_REFERENCE_PREFIX not in plain_markdown
    assert "Cover" in plain_markdown


@pytest.mark.acceptance
def test_processes_epub_resources_collision_free_and_reports_normal_stages(
    tmp_path: Path,
) -> None:
    source = tmp_path / "small book.epub"
    _write_epub2(source)
    stages: list[ProcessStage] = []

    first = process_document(
        ProcessRequest(source, convert_to_markdown=True),
        on_stage=stages.append,
    )
    second = process_document(ProcessRequest(source, convert_to_markdown=True))

    assert first.final_path == tmp_path / "small book.md"
    assert second.final_path == tmp_path / "small book-2.md"
    first_markdown = first.final_path.read_text(encoding="utf-8")
    second_markdown = second.final_path.read_text(encoding="utf-8")
    assert "small%20book.assets/OEBPS/images/cover%20photo.jpg" in first_markdown
    assert "small%20book-2.assets/OEBPS/images/cover%20photo.jpg" in second_markdown
    assert "PZDOC EPUB ANCHOR" not in first_markdown
    assert '<a id="epub-document-1-anchor-1-start"></a>' in first_markdown
    assert (tmp_path / "small book.assets" / "OEBPS/images/cover photo.jpg").read_bytes() == (
        b"fake-jpeg-content"
    )
    assert (tmp_path / "small book-2.assets" / "OEBPS/images/cover photo.jpg").exists()
    assert stages == [
        ProcessStage.VALIDATING,
        ProcessStage.CONVERTING,
        ProcessStage.WRITING,
        ProcessStage.COMPLETED,
    ]


def test_raw_and_mended_epub_outputs_share_one_portable_resource_folder(
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.epub"
    _write_epub2(source)
    converted = convert_document(source, preserve_resources=True)

    final_path, raw_path = write_improvement_outputs(
        source,
        converted.markdown,
        converted.markdown.replace("A faithful first paragraph.", "A polished first paragraph."),
        keep_raw=True,
        resources=converted.resources,
    )

    assert raw_path == tmp_path / "book.raw.md"
    assert final_path == tmp_path / "book.mended.md"
    assert "book.assets/OEBPS/images/cover%20photo.jpg" in raw_path.read_text(encoding="utf-8")
    assert "book.assets/OEBPS/images/cover%20photo.jpg" in final_path.read_text(encoding="utf-8")
    assert (tmp_path / "book.assets" / "OEBPS/images/cover photo.jpg").exists()


def test_epub3_navigation_is_used_without_an_ncx_file(tmp_path: Path) -> None:
    source = tmp_path / "modern.epub"
    _write_epub3(source)

    converted = convert_document(source, preserve_resources=True)

    assert "# Modern Book" in converted.markdown
    assert "## Opening" in converted.markdown
    assert "Modern EPUB content." in converted.markdown
    assert f"![Portada]({RESOURCE_REFERENCE_PREFIX}EPUB/cover.jpg)" in converted.markdown
    assert len(converted.resources) == 1
    assert converted.resources[0].relative_path.as_posix() == "EPUB/cover.jpg"
    assert converted.resources[0].content == b"modern-cover"


def test_inspects_epub_metadata_for_the_normalized_editor(tmp_path: Path) -> None:
    source = tmp_path / "modern.epub"
    _write_epub3(source)

    metadata = inspect_epub_package(source)

    assert metadata.title == "Modern Book"
    assert metadata.language == "en"
    assert metadata.cover_path is not None
    assert metadata.cover_path.as_posix() == "EPUB/cover.jpg"


def test_epub_translation_preserves_package_images_links_and_reading_order(
    tmp_path: Path,
) -> None:
    source = tmp_path / "modern.epub"
    _write_epub3(source)
    source_bytes = source.read_bytes()
    progress: list[tuple[int, int]] = []

    def translate(text: str, on_progress, _cancellation) -> str:
        if on_progress is not None:
            on_progress(1, 1)
        return (
            text.replace("Modern Book", "Libro moderno")
            .replace("Modern EPUB content.", "Contenido EPUB moderno.")
            .replace("Cover image", "Imagen de portada")
            .replace("Contents", "Índice")
            .replace("Opening", "Apertura")
        )

    translated = translate_epub(
        source,
        "es",
        translate,
        on_progress=lambda current, total: progress.append((current, total)),
    )

    assert source.read_bytes() == source_bytes
    assert translated.language_code == "es"
    assert translated.translated_units >= 5
    assert translated.quality_report.checked_segments == translated.translated_units
    assert progress == [(1, 1)]
    with ZipFile(source) as original, ZipFile(BytesIO(translated.content)) as result:
        assert result.namelist() == original.namelist()
        assert result.namelist()[0] == "mimetype"
        assert result.getinfo("mimetype").compress_type == ZIP_STORED
        assert result.read("EPUB/cover.jpg") == original.read("EPUB/cover.jpg")
        package = result.read("EPUB/package.opf").decode("utf-8")
        navigation = result.read("EPUB/nav.xhtml").decode("utf-8")
        opening = result.read("EPUB/opening.xhtml").decode("utf-8")
        assert "<dc:title>Libro moderno</dc:title>" in package
        assert "<dc:language>es</dc:language>" in package
        assert 'href="opening.xhtml#start"' in navigation
        assert ">Apertura<" in navigation
        assert ">Contenido EPUB moderno.<" in opening
        assert 'id="start"' in opening
        assert 'xlink:href="cover.jpg"' in opening
        assert 'aria-label="Imagen de portada"' in opening
        assert 'lang="es"' in opening


def test_epub_metadata_can_change_without_normalizing_the_package(tmp_path: Path) -> None:
    source = tmp_path / "metadata.epub"
    _write_epub2(source)
    source_bytes = source.read_bytes()

    updated = replace_epub_metadata(
        source_bytes,
        title="Título elegido",
        author="Autora elegida",
    )

    with ZipFile(BytesIO(source_bytes)) as original, ZipFile(BytesIO(updated)) as result:
        assert result.namelist() == original.namelist()
        assert result.read("OEBPS/images/cover photo.jpg") == original.read(
            "OEBPS/images/cover photo.jpg"
        )
        package = result.read("OEBPS/content.opf").decode("utf-8")
        assert "<dc:title>Título elegido</dc:title>" in package
        assert "Autora elegida" in package
        assert 'opf:role="aut"' in package


def test_epub_translation_preserves_xml_prolog_comments_and_processing_instructions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "fidelity.epub"
    _write_epub3(source)
    with ZipFile(source) as archive:
        opening = archive.read("EPUB/opening.xhtml")
    opening = opening.replace(
        b'<html xmlns="http://www.w3.org/1999/xhtml">',
        (
            b"<!DOCTYPE html>\n<!-- preserved prolog -->\n<?parsezen keep?>\n"
            b'<html xmlns="http://www.w3.org/1999/xhtml">'
        ),
    ).replace(b"  <body>", b"  <body><!-- preserved body comment -->")
    _rewrite_epub_members(source, {"EPUB/opening.xhtml": opening})

    translated = translate_epub(
        source,
        "es",
        lambda text, _progress, _token: text.replace(
            "Modern EPUB content.",
            "Contenido EPUB moderno.",
        ),
    )

    with ZipFile(BytesIO(translated.content)) as result:
        generated = result.read("EPUB/opening.xhtml")
    assert b"<!DOCTYPE html>" in generated
    assert b"<!-- preserved prolog -->" in generated
    assert b"<?parsezen keep?>" in generated
    assert b"<!-- preserved body comment -->" in generated
    assert "Contenido EPUB moderno." in generated.decode("utf-8")


def test_epub_translation_allows_standard_font_obfuscation(tmp_path: Path) -> None:
    source = tmp_path / "obfuscated-font.epub"
    _write_epub3(source)
    with ZipFile(source) as archive:
        package = archive.read("EPUB/package.opf")
    package = package.replace(
        b'<item id="cover" href="cover.jpg" media-type="image/jpeg" properties="cover-image"/>',
        (
            b'<item id="cover" href="cover.jpg" media-type="image/jpeg" '
            b'properties="cover-image"/>\n'
            b'    <item id="font" href="fonts/book.otf" media-type="font/otf"/>'
        ),
    )
    encryption = b"""<?xml version="1.0" encoding="UTF-8"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
            xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
  <enc:EncryptedData>
    <enc:EncryptionMethod Algorithm="http://www.idpf.org/2008/embedding"/>
    <enc:CipherData><enc:CipherReference URI="EPUB/fonts/book.otf"/></enc:CipherData>
  </enc:EncryptedData>
</encryption>
"""
    font = b"obfuscated-font-bytes"
    _rewrite_epub_members(
        source,
        {"EPUB/package.opf": package},
        {
            "META-INF/encryption.xml": encryption,
            "EPUB/fonts/book.otf": font,
        },
    )

    translated = translate_epub(source, "es", lambda text, _progress, _token: text)

    with ZipFile(BytesIO(translated.content)) as result:
        assert result.read("META-INF/encryption.xml") == encryption
        assert result.read("EPUB/fonts/book.otf") == font


def test_epub_translation_still_rejects_content_encryption(tmp_path: Path) -> None:
    source = tmp_path / "encrypted-content.epub"
    _write_epub3(source)
    encryption = b"""<?xml version="1.0" encoding="UTF-8"?>
<encryption xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
  <enc:EncryptedData>
    <enc:EncryptionMethod Algorithm="urn:example:content-encryption"/>
    <enc:CipherData><enc:CipherReference URI="EPUB/opening.xhtml"/></enc:CipherData>
  </enc:EncryptedData>
</encryption>
"""
    _rewrite_epub_members(source, {}, {"META-INF/encryption.xml": encryption})

    with pytest.raises(ConversionError, match="recursos cifrados"):
        translate_epub(source, "es", lambda text, _progress, _token: text)


def test_epub_translation_does_not_expose_internal_references_to_translator(
    tmp_path: Path,
) -> None:
    source = tmp_path / "unsafe.epub"
    _write_epub3(source)

    def change_link(text: str, _on_progress, _cancellation) -> str:
        return text.replace("opening.xhtml#start", "missing.xhtml#start")

    translated = translate_epub(source, "es", change_link)

    with ZipFile(BytesIO(translated.content)) as archive:
        navigation = archive.read("EPUB/nav.xhtml").decode("utf-8")
    assert 'href="opening.xhtml#start"' in navigation
    assert "missing.xhtml#start" not in navigation


def test_epub2_translation_preserves_prefixed_opf_attributes(tmp_path: Path) -> None:
    source = tmp_path / "legacy.epub"
    _write_epub2(source)

    translated = translate_epub(
        source,
        "es",
        lambda text, _progress, _token: _translate_epub2_payload(text),
    )

    with ZipFile(BytesIO(translated.content)) as result:
        package = result.read("OEBPS/content.opf")
    assert b'xmlns:opf="http://www.idpf.org/2007/opf"' in package
    assert b'opf:role="aut"' in package


def test_epub_translation_uses_semantic_parts_without_chapter_metadata(
    tmp_path: Path,
) -> None:
    source = tmp_path / "one-file-book.epub"
    _write_chapterless_epub(source)
    progress: list[tuple[int, int]] = []
    calls = 0

    def translate(text: str, _progress, _token) -> str:
        nonlocal calls
        calls += 1
        return _translate_chapterless_payload(text)

    translated = translate_epub(
        source,
        "es",
        translate,
        on_progress=lambda current, total: progress.append((current, total)),
    )

    assert translated.translation_parts > 1
    assert calls == translated.translation_parts
    assert progress == [
        (current, translated.translation_parts)
        for current in range(1, translated.translation_parts + 1)
    ]
    with ZipFile(BytesIO(translated.content)) as result:
        content = result.read("EPUB/book.xhtml").decode("utf-8")
    assert content.count("Párrafo traducido") == 30
    assert "Original paragraph" not in content


def test_epub_translation_recovers_text_from_poorly_structured_xhtml(tmp_path: Path) -> None:
    source = tmp_path / "poor-markup.epub"
    _write_chapterless_epub(source, structured_paragraphs=False)

    translated = translate_epub(
        source,
        "es",
        lambda text, _progress, _token: (
            text.replace("Loose prose", "Prosa suelta")
            .replace("nested emphasis", "énfasis anidado")
            .replace("and a tail", "y una continuación")
        ),
    )

    assert translated.translated_units >= 1
    with ZipFile(BytesIO(translated.content)) as result:
        content = result.read("EPUB/book.xhtml").decode("utf-8")
    assert "Prosa suelta" in content
    assert "énfasis anidado" in content
    assert "y una continuación" in content
    assert "<strong>énfasis anidado</strong>" in content


def test_epub_translation_resumes_only_verified_completed_parts(tmp_path: Path) -> None:
    source = tmp_path / "resumable.epub"
    _write_chapterless_epub(source)
    checkpoints: dict[str, str] = {}
    first_calls = 0

    def interrupt_second_part(text: str, _progress, _token) -> str:
        nonlocal first_calls
        first_calls += 1
        if first_calls == 2:
            raise ConversionError("Interrupción simulada")
        return _translate_chapterless_payload(text)

    with pytest.raises(ConversionError, match="Interrupción simulada"):
        translate_epub(
            source,
            "es",
            interrupt_second_part,
            load_checkpoint=checkpoints.get,
            save_checkpoint=lambda key, value: not checkpoints.__setitem__(key, value),
        )

    assert len(checkpoints) == 1
    resumed_calls = 0

    def finish_translation(text: str, _progress, _token) -> str:
        nonlocal resumed_calls
        resumed_calls += 1
        return _translate_chapterless_payload(text)

    translated = translate_epub(
        source,
        "es",
        finish_translation,
        load_checkpoint=checkpoints.get,
        save_checkpoint=lambda key, value: not checkpoints.__setitem__(key, value),
    )

    assert translated.resumed_parts == 1
    assert resumed_calls == translated.translation_parts - 1
    with ZipFile(BytesIO(translated.content)) as result:
        content = result.read("EPUB/book.xhtml").decode("utf-8")
    assert content.count("Párrafo traducido") == 30


def test_epub_translation_retranslates_an_invalid_cached_part(tmp_path: Path) -> None:
    source = tmp_path / "invalid-cache.epub"
    _write_epub3(source)
    calls = 0

    def translate(text: str, _progress, _token) -> str:
        nonlocal calls
        calls += 1
        return text

    translated = translate_epub(
        source,
        "es",
        translate,
        load_checkpoint=lambda _key: "not valid XHTML",
    )

    assert calls == translated.translation_parts
    assert translated.resumed_parts == 0


def test_epub_translation_retranslates_a_cached_part_that_changed_a_number(
    tmp_path: Path,
) -> None:
    source = tmp_path / "number-cache.epub"
    source.write_bytes(
        build_epub(
            "# Chapter 10\n\n"
            "This substantial paragraph records exactly 10,000 observations and contains "
            "enough English text to establish its language reliably during translation.",
            (),
            EpubBookMetadata("Number Book", "en"),
        ).content
    )
    checkpoints: dict[str, str] = {}

    def translate(text: str, _progress, _token) -> str:
        return (
            text.replace("Chapter", "Capítulo")
            .replace("Number Book", "Libro de cifras")
            .replace(
                "This substantial paragraph records exactly",
                "Este párrafo sustancial registra exactamente",
            )
            .replace(
                "observations and contains enough English text to establish its language "
                "reliably during translation.",
                "observaciones y contiene suficiente texto para establecer su idioma con "
                "fiabilidad durante la traducción.",
            )
        )

    first = translate_epub(
        source,
        "es",
        translate,
        save_checkpoint=lambda key, value: not checkpoints.__setitem__(key, value),
    )
    damaged = {key: value.replace("10,000", "10") for key, value in checkpoints.items()}
    calls = 0

    def count_translation(text: str, progress, token) -> str:
        nonlocal calls
        calls += 1
        return translate(text, progress, token)

    second = translate_epub(
        source,
        "es",
        count_translation,
        load_checkpoint=damaged.get,
    )

    assert first.translation_parts == second.translation_parts
    assert second.resumed_parts == second.translation_parts - 1
    assert calls == 1
    with ZipFile(BytesIO(second.content)) as archive:
        content = archive.read("EPUB/text/chapter-0001.xhtml").decode("utf-8")
    assert "10,000" in content


def test_epub_translation_retries_a_unit_that_dropped_a_protected_number(
    tmp_path: Path,
) -> None:
    source = tmp_path / "changed-number.epub"
    source.write_bytes(
        build_epub(
            "# Chapter\n\n"
            "This substantial paragraph records exactly 10,000 observations and contains "
            "enough English text to establish its language reliably during translation.",
            (),
            EpubBookMetadata("Number Book", "en"),
        ).content
    )
    unit_retries = 0

    def translate(text: str, _progress, _token) -> str:
        nonlocal unit_retries
        is_unit_retry = "PZDOC_EPUB_XML_RETRY" in text
        unit_retries += is_unit_retry
        translated = (
            text.replace("Chapter", "Capítulo")
            .replace("Number Book", "Libro de cifras")
            .replace(
                "This substantial paragraph records exactly",
                "Este párrafo sustancial registra exactamente",
            )
            .replace(
                "observations and contains enough English text to establish its language "
                "reliably during translation.",
                "observaciones y contiene suficiente texto para establecer su idioma con "
                "fiabilidad durante la traducción.",
            )
        )
        if is_unit_retry:
            return translated
        return re.sub(
            r"(registra exactamente\s+)<!-- PZDOC_EPUB_XML_[A-Z_]+ -->",
            r"\g<1>10",
            translated,
        )

    translated = translate_epub(source, "es", translate)

    assert unit_retries == 1
    with ZipFile(BytesIO(translated.content)) as archive:
        content = archive.read("EPUB/text/chapter-0001.xhtml").decode("utf-8")
    assert "10,000" in content


@pytest.mark.parametrize("damage", ["empty", "duplicated"])
def test_epub_translation_retries_a_unit_with_unsafe_content_coverage(
    tmp_path: Path,
    damage: str,
) -> None:
    source = tmp_path / f"unsafe-coverage-{damage}.epub"
    original = (
        "This substantial paragraph contains enough English prose to verify that the complete "
        "meaning remains present after a safe local translation."
    )
    translated_paragraph = (
        "Este párrafo sustancial contiene suficiente prosa para verificar que todo el sentido "
        "permanece presente después de una traducción local segura."
    )
    source.write_bytes(
        build_epub(
            f"# Chapter\n\n{original}",
            (),
            EpubBookMetadata("Coverage Book", "en"),
        ).content
    )
    unit_retries = 0

    def translate(text: str, _progress, _token) -> str:
        nonlocal unit_retries
        is_unit_retry = "PZDOC_EPUB_XML_RETRY" in text
        unit_retries += is_unit_retry
        result = (
            text.replace("Chapter", "Capítulo")
            .replace("Coverage Book", "Libro de cobertura")
            .replace(original, translated_paragraph)
        )
        if is_unit_retry:
            return result
        replacement = "" if damage == "empty" else f"{translated_paragraph} {translated_paragraph}"
        return result.replace(translated_paragraph, replacement)

    result = translate_epub(source, "es", translate)

    assert unit_retries == 1
    with ZipFile(BytesIO(result.content)) as archive:
        content = archive.read("EPUB/text/chapter-0001.xhtml").decode("utf-8")
    assert content.count(translated_paragraph) == 1


def test_epub_translation_escapes_invalid_xml_text_without_retranslating(
    tmp_path: Path,
) -> None:
    source = tmp_path / "invalid-xhtml-unit.epub"
    _write_epub3(source)
    calls: list[bool] = []

    def translate(text: str, _progress, _token) -> str:
        is_unit_retry = "PZDOC_EPUB_XML_RETRY" in text
        calls.append(is_unit_retry)
        assert "xmlns:" not in text
        assert "<html:" not in text
        assert "<dc:" not in text
        translated = text.replace("Opening", "Apertura").replace(
            "Modern EPUB content.",
            "Contenido EPUB moderno.",
        )
        if not is_unit_retry and "Contenido EPUB moderno." in translated:
            return translated.replace(
                "Contenido EPUB moderno.",
                "Contenido & EPUB moderno.",
            )
        return translated

    result = translate_epub(source, "es", translate)

    assert calls.count(True) == 0
    assert len(calls) == result.translation_parts
    with ZipFile(BytesIO(result.content)) as archive:
        content = archive.read("EPUB/opening.xhtml").decode("utf-8")
    assert "Contenido &amp; EPUB moderno." in content


def test_epub_xml_protection_uses_independent_private_comments() -> None:
    source = '<p xmlns="urn:test"><strong>Opening</strong></p>'

    protected, markers = epub_conversion_module._protect_epub_xml_markup(source, 0)

    assert len(markers) == 2
    assert protected.count("<!-- PZDOC_EPUB_XML_") == len(markers)
    assert "`" not in protected
    assert epub_conversion_module._restore_epub_xml_markup(protected, markers) == source
    translated = protected.replace("Opening", "Apertura & cierre <seguro>")
    assert epub_conversion_module._restore_epub_xml_markup(translated, markers) == (
        '<p xmlns="urn:test"><strong>Apertura &amp; cierre &lt;seguro&gt;</strong></p>'
    )
    wrapped = f"Explicación ajena. {translated} Nota ajena."
    assert epub_conversion_module._restore_epub_xml_markup(wrapped, markers) == (
        '<p xmlns="urn:test"><strong>Apertura &amp; cierre &lt;seguro&gt;</strong></p>'
    )


def test_epub_xml_protection_locks_visible_numbers_and_urls() -> None:
    source = '<p xmlns="urn:test">Keep 10,000 and https://example.com/10 intact.</p>'

    protected, markers = epub_conversion_module._protect_epub_xml_markup(source, 0)

    assert "10,000" not in protected
    assert "https://example.com/10" not in protected
    assert len(markers) == 4
    assert epub_conversion_module._restore_epub_xml_markup(protected, markers) == source


def test_epub_text_coverage_does_not_treat_plain_xml_text_as_markdown() -> None:
    epub_conversion_module._validate_epub_conserved_text(
        "- Original label",
        "Etiqueta traducida",
    )


def test_epub_translation_retries_a_detectably_unchanged_part_by_unit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "unchanged-part.epub"
    _write_chapterless_epub(source)
    calls: list[bool] = []

    def translate(text: str, _progress, _token) -> str:
        is_unit_retry = "PZDOC_EPUB_XML_RETRY" in text
        calls.append(is_unit_retry)
        if not is_unit_retry:
            return text
        return text.replace("Original paragraph", "Párrafo traducido").replace(
            "This is deliberately substantial text",
            "Este es un texto deliberadamente sustancial",
        )

    result = translate_epub(source, "es", translate)

    assert calls.count(False) == result.translation_parts
    assert calls.count(True) > 0
    with ZipFile(BytesIO(result.content)) as archive:
        content = archive.read("EPUB/book.xhtml").decode("utf-8")
    assert content.count("Párrafo traducido") == 30
    assert "Original paragraph" not in content


def test_epub_translation_retries_a_part_still_dominated_by_source_language(
    tmp_path: Path,
) -> None:
    source = tmp_path / "mostly-unchanged-part.epub"
    _write_chapterless_epub(source)
    calls: list[bool] = []

    def translate(text: str, _progress, _token) -> str:
        is_unit_retry = "PZDOC_EPUB_XML_RETRY" in text
        calls.append(is_unit_retry)
        if not is_unit_retry:
            return text.replace("Original paragraph", "Párrafo traducido", 1)
        return text.replace("Original paragraph", "Párrafo traducido").replace(
            "This is deliberately substantial text",
            "Este es un texto deliberadamente sustancial",
        )

    result = translate_epub(source, "es", translate)

    assert calls.count(False) == result.translation_parts
    assert calls.count(True) > 0
    with ZipFile(BytesIO(result.content)) as archive:
        content = archive.read("EPUB/book.xhtml").decode("utf-8")
    assert content.count("Párrafo traducido") == 30
    assert "Original paragraph" not in content


def test_epub_translation_retries_one_substantial_source_language_unit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "one-residual-unit.epub"
    regular = (
        "A regular English paragraph contains enough source-language prose to be translated "
        "safely and completely. " * 4
    )
    residual = (
        "This unique English paragraph must not remain untranslated when all surrounding units "
        "are already in the requested language. " * 4
    )
    source.write_bytes(
        build_epub(
            "# Book\n\n" + "\n\n".join((*((regular,) * 12), residual)),
            (),
            EpubBookMetadata("Book", "en"),
        ).content
    )
    unit_retries = 0

    def translate(text: str, _progress, _token) -> str:
        nonlocal unit_retries
        is_unit_retry = "PZDOC_EPUB_XML_RETRY" in text
        unit_retries += is_unit_retry
        result = (
            text.replace("Book", "Libro")
            .replace(
                "A regular English paragraph contains enough source-language prose to be "
                "translated",
                "Un párrafo normal contiene suficiente prosa traducida",
            )
            .replace("safely and completely.", "de forma segura y completa.")
        )
        if is_unit_retry:
            result = result.replace(
                "This unique English paragraph must not remain untranslated when all "
                "surrounding units",
                "Este párrafo único no debe permanecer sin traducir cuando las demás unidades",
            ).replace(
                "are already in the requested language.",
                "ya están en el idioma solicitado.",
            )
        return result

    result = translate_epub(source, "es", translate)

    assert unit_retries == 1
    with ZipFile(BytesIO(result.content)) as archive:
        content = archive.read("EPUB/text/chapter-0001.xhtml").decode("utf-8")
    assert residual not in content


def test_epub_does_not_retry_a_changed_unit_only_because_a_citation_dominates_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_text = (
        "The complete English citation has a deliberately long subtitle about inner freedom, "
        "consciousness, and the meaning of life for every attentive reader."
    )
    translated_text = (
        "Referencia conservada con su título original: a deliberately long subtitle about "
        "inner freedom, consciousness, and the meaning of life for every attentive reader."
    )
    unit = epub_conversion_module._EpubTranslationUnit(
        "chapter.xhtml",
        (),
        f'<p xmlns="urn:test">{source_text}</p>',
    )
    monkeypatch.setattr(
        epub_conversion_module,
        "detect_language_code",
        lambda *_args, **_kwargs: "en",
    )

    assert not epub_conversion_module._translated_unit_remains_source_language(
        unit,
        f'<p xmlns="urn:test">{translated_text}</p>',
        "en",
    )


def test_epub_translation_repairs_invalid_and_untranslated_units_independently(
    tmp_path: Path,
) -> None:
    source = tmp_path / "invalid-and-untranslated.epub"
    _write_chapterless_epub(source)
    calls: list[bool] = []

    def translate(text: str, _progress, _token) -> str:
        is_unit_retry = "PZDOC_EPUB_XML_RETRY" in text
        calls.append(is_unit_retry)
        if not is_unit_retry:
            return text.replace("Original paragraph 1.", "Párrafo & inválido.", 1)
        return _translate_chapterless_payload(text)

    result = translate_epub(source, "es", translate)

    assert calls.count(False) == result.translation_parts
    assert calls.count(True) == result.translated_units
    with ZipFile(BytesIO(result.content)) as archive:
        content = archive.read("EPUB/book.xhtml").decode("utf-8")
    assert content.count("Párrafo traducido") == 30
    assert "Original paragraph" not in content


def test_epub_translation_starts_grouped_for_dense_short_markup(
    tmp_path: Path,
) -> None:
    source = tmp_path / "dense-short-units.epub"
    markdown = "# Short Book\n\n" + "\n\n".join(
        f"Short original sentence {index} contains clear English words." for index in range(25)
    )
    source.write_bytes(build_epub(markdown, (), EpubBookMetadata("Book", "en")).content)
    calls: list[bool] = []

    def translate(text: str, _progress, _token) -> str:
        is_unit_retry = "PZDOC_EPUB_XML_RETRY" in text
        calls.append(is_unit_retry)
        assert not is_unit_retry
        return text.replace("Short original sentence", "Frase original breve").replace(
            "contains clear English words",
            "contiene palabras claras en inglés",
        )

    result = translate_epub(source, "es", translate)

    assert calls and not any(calls)
    with ZipFile(BytesIO(result.content)) as archive:
        content = archive.read("EPUB/text/chapter-0001.xhtml").decode("utf-8")
    assert content.count("Frase original breve") == 25


def test_epub_translation_completes_but_reports_checkpoint_degradation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "checkpoint-failure.epub"
    _write_epub3(source)

    translated = translate_epub(
        source,
        "es",
        lambda text, _progress, _token: text,
        save_checkpoint=lambda _key, _value: False,
    )

    assert translated.content
    assert translated.checkpoint_degraded is True


@pytest.mark.acceptance
def test_processes_an_epub_translation_directly_and_avoids_collisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "modern.epub"
    _write_epub3(source)
    source_bytes = source.read_bytes()
    stages: list[ProcessStage] = []

    def translate(markdown: str, language: str, **_kwargs) -> str:
        assert language == "Español"
        on_engine_ready = _kwargs.get("on_engine_ready")
        if on_engine_ready is not None:
            on_engine_ready()
        return (
            markdown.replace("Modern Book", "Libro moderno")
            .replace("Modern EPUB content.", "Contenido EPUB moderno.")
            .replace("Contents", "Índice")
            .replace("Opening", "Apertura")
        )

    monkeypatch.setattr(processing_module, "translate_markdown_offline", translate)
    request = ProcessRequest(
        source,
        convert_to_markdown=False,
        offline_translation_language="Español",
        output_format=OutputFormat.EPUB,
    )
    first = process_document(request, on_stage=stages.append)
    second = process_document(request)

    assert first.final_path == tmp_path / "modern.es.epub"
    assert second.final_path == tmp_path / "modern.es-2.epub"
    assert first.raw_markdown_path is None
    assert first.review_original_path == source
    assert first.translation_quality_report is not None
    assert first.translation_quality_report.target_language == "es"
    assert source.read_bytes() == source_bytes
    assert stages == [
        ProcessStage.VALIDATING,
        ProcessStage.PREPARING_TRANSLATION,
        ProcessStage.TRANSLATING,
        ProcessStage.WRITING,
        ProcessStage.COMPLETED,
    ]
    with ZipFile(first.final_path) as translated:
        assert translated.read("EPUB/cover.jpg") == b"modern-cover"
        assert b"Contenido EPUB moderno." in translated.read("EPUB/opening.xhtml")


def test_epub_translation_applies_selected_title_author_and_custom_cover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "customized.epub"
    _write_epub3(source)
    cover = tmp_path / "new-cover.png"
    cover.write_bytes(b"new-local-cover")

    def translate(markdown: str, _language: str, **kwargs) -> str:
        on_engine_ready = kwargs.get("on_engine_ready")
        if on_engine_ready is not None:
            on_engine_ready()
        return (
            markdown.replace("Modern Book", "Libro moderno")
            .replace("Modern EPUB content.", "Contenido EPUB moderno.")
            .replace("Contents", "Índice")
            .replace("Opening", "Apertura")
            .replace("Cover image", "Imagen de portada")
        )

    monkeypatch.setattr(processing_module, "translate_markdown_offline", translate)
    result = process_document(
        ProcessRequest(
            source,
            convert_to_markdown=False,
            offline_translation_language="Español",
            output_format=OutputFormat.EPUB,
            epub_title="Edición personalizada",
            epub_author="Autora local",
            epub_cover_path=cover,
        ),
        epub_checkpoint_root=tmp_path / "checkpoints",
    )

    with ZipFile(result.final_path) as archive:
        package = archive.read("EPUB/package.opf").decode("utf-8")
        assert "<dc:title>Edición personalizada</dc:title>" in package
        assert "<dc:creator>Autora local</dc:creator>" in package
        assert 'properties="cover-image"' in package
        assert archive.read("EPUB/images/cover/cover.png") == b"new-local-cover"
        assert "EPUB/images/EPUB/cover.jpg" not in archive.namelist()


def test_epub_review_route_can_remove_the_original_cover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "without-cover.epub"
    _write_epub3(source)
    review_modes: list[ImprovementMode] = []

    def translate(markdown: str, _language: str, **kwargs) -> str:
        on_engine_ready = kwargs.get("on_engine_ready")
        if on_engine_ready is not None:
            on_engine_ready()
        return (
            markdown.replace("Modern Book", "Libro moderno")
            .replace("Modern EPUB content.", "Contenido EPUB moderno.")
            .replace("Contents", "Índice")
            .replace("Opening", "Apertura")
            .replace("Cover image", "Imagen de portada")
        )

    monkeypatch.setattr(processing_module, "translate_markdown_offline", translate)

    def improve(markdown: str, mode: ImprovementMode, *_args, **_kwargs) -> str:
        review_modes.append(mode)
        return markdown

    monkeypatch.setattr(processing_module, "improve_markdown", improve)

    def review_translation(
        _source_markdown: str,
        translated_markdown: str,
        *_args,
        **_kwargs,
    ) -> str:
        review_modes.append(ImprovementMode.REVIEW_CONTENT)
        return translated_markdown

    monkeypatch.setattr(
        processing_module,
        "review_translation_markdown",
        review_translation,
    )
    result = process_document(
        ProcessRequest(
            source,
            convert_to_markdown=True,
            offline_translation_language="Español",
            output_format=OutputFormat.EPUB,
            review_content=True,
            epub_remove_cover=True,
        ),
        settings=AppSettings(model="installed-model"),
        epub_checkpoint_root=tmp_path / "epub-checkpoints",
        work_checkpoint_root=tmp_path / "work-checkpoints",
    )

    with ZipFile(result.final_path) as archive:
        package = archive.read("EPUB/package.opf").decode("utf-8")
        assert 'properties="cover-image"' not in package
        assert not any("cover" in name.casefold() for name in archive.namelist())
    assert result.epub_translation_parts > 0
    assert review_modes == [ImprovementMode.REVIEW_CONTENT]


@pytest.mark.parametrize(
    ("review_content", "expected_mode"),
    [
        (False, ImprovementMode.TRANSLATE),
        (True, ImprovementMode.CLEAN_AND_TRANSLATE),
    ],
)
def test_processes_an_epub_translation_with_the_selected_ollama_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    review_content: bool,
    expected_mode: ImprovementMode,
) -> None:
    source = tmp_path / "ai-book.epub"
    _write_epub3(source)
    calls: list[tuple[ImprovementMode, str | None]] = []
    checkpoint_callbacks: list[tuple[object, object]] = []
    source_languages: list[str | None] = []

    def improve(markdown: str, mode: ImprovementMode, _settings, language, **_kwargs) -> str:
        calls.append((mode, language))
        checkpoint_callbacks.append(
            (_kwargs.get("load_checkpoint"), _kwargs.get("save_checkpoint"))
        )
        source_languages.append(_kwargs.get("source_language_code"))
        return (
            markdown.replace("Modern Book", "Libro moderno")
            .replace("Contents", "Índice")
            .replace("Opening", "Apertura")
            .replace("Cover image", "Imagen de portada")
            .replace("Modern EPUB content.", "Contenido EPUB moderno.")
        )

    monkeypatch.setattr(processing_module, "improve_markdown", improve)
    result = process_document(
        ProcessRequest(
            source,
            convert_to_markdown=review_content,
            improvement_mode=ImprovementMode.TRANSLATE,
            target_language="Español",
            output_format=OutputFormat.EPUB,
            review_content=review_content,
        ),
        settings=AppSettings(model="installed-model"),
        epub_checkpoint_root=tmp_path / f"epub-checkpoints-{review_content}",
        work_checkpoint_root=tmp_path / f"work-checkpoints-{review_content}",
    )

    assert result.final_path == tmp_path / "ai-book.es.epub"
    assert calls == [(expected_mode, "Español")]
    assert source_languages == ["en"]
    assert all(callable(callback) for callbacks in checkpoint_callbacks for callback in callbacks)
    with ZipFile(result.final_path) as translated:
        assert b"Contenido EPUB moderno." in translated.read("EPUB/opening.xhtml")


def test_epub_translation_repairs_residual_text_before_saving_the_part(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "repair-book.epub"
    _write_epub3(source)
    calls: list[str] = []

    def translate(markdown: str, _language: str, **kwargs) -> str:
        calls.append(markdown)
        if len(calls) == 1:
            return (
                markdown.replace("Modern Book", "Libro moderno")
                .replace("Contents", "Índice")
                .replace("Opening", "Apertura")
                .replace("Cover image", "Imagen de portada")
            )
        assert kwargs["source_language_code"] == "en"
        return markdown.replace("Modern EPUB content.", "Contenido EPUB moderno.")

    monkeypatch.setattr(processing_module, "translate_markdown_offline", translate)
    result = process_document(
        ProcessRequest(
            source,
            convert_to_markdown=False,
            offline_translation_language="Español",
            output_format=OutputFormat.EPUB,
        ),
        epub_checkpoint_root=tmp_path / "checkpoints",
    )

    assert len(calls) == 2
    assert all("xmlns:" not in payload for payload in calls)
    assert all("<html:" not in payload for payload in calls)
    assert result.translation_quality_report is not None
    assert result.translation_quality_report.total_issues == 0
    with ZipFile(result.final_path) as translated:
        content = translated.read("EPUB/opening.xhtml").decode("utf-8")
    assert "Contenido EPUB moderno." in content
    assert "Modern EPUB content." not in content


@pytest.mark.acceptance
def test_direct_epub_translation_resumes_after_an_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "long-book.epub"
    _write_chapterless_epub(source)
    cache_root = tmp_path / "translation-cache"
    request = ProcessRequest(
        source,
        convert_to_markdown=False,
        offline_translation_language="Español",
        output_format=OutputFormat.EPUB,
    )
    interrupted_main_calls = 0

    def interrupt_translation(markdown: str, _language: str, **_kwargs) -> str:
        nonlocal interrupted_main_calls
        if markdown.count("PZDOC EPUB TRANSLATION UNIT") > 1:
            interrupted_main_calls += 1
        if interrupted_main_calls == 2:
            raise ConversionError("Interrupción simulada")
        return _translate_chapterless_payload(markdown)

    monkeypatch.setattr(processing_module, "translate_markdown_offline", interrupt_translation)
    with pytest.raises(ConversionError, match="Interrupción simulada"):
        process_document(
            request,
            settings=AppSettings(checkpoint_retention_days=0),
            epub_checkpoint_root=cache_root,
        )
    assert not (tmp_path / "long-book.es.epub").exists()

    resumed_main_calls = 0

    def finish_translation(markdown: str, _language: str, **_kwargs) -> str:
        nonlocal resumed_main_calls
        if markdown.count("PZDOC EPUB TRANSLATION UNIT") > 1:
            resumed_main_calls += 1
        return _translate_chapterless_payload(markdown)

    monkeypatch.setattr(processing_module, "translate_markdown_offline", finish_translation)
    result = process_document(
        request,
        settings=AppSettings(checkpoint_retention_days=0),
        epub_checkpoint_root=cache_root,
    )

    assert result.epub_resumed_parts == 1
    assert resumed_main_calls == result.epub_translation_parts - 1
    assert list(cache_root.iterdir()) == []
    with ZipFile(result.final_path) as translated:
        content = translated.read("EPUB/book.xhtml").decode("utf-8")
    assert content.count("Párrafo traducido") == 30


def test_rejects_a_zip_that_is_not_an_epub(tmp_path: Path) -> None:
    source = tmp_path / "broken.epub"
    with ZipFile(source, "w") as archive:
        archive.writestr("notes.txt", "not an epub")

    with pytest.raises(ConversionError, match="identificación mínima"):
        convert_document(source)


def test_rejects_an_epub_member_above_the_individual_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "oversized-member.epub"
    _write_epub2(source)
    _rewrite_epub_members(source, {}, {"OEBPS/unreferenced.bin": b"x" * 2_048})
    monkeypatch.setattr(epub_conversion_module, "_MAX_ARCHIVE_MEMBER_BYTES", 1_024)

    with pytest.raises(ConversionError, match="archivo interno demasiado grande"):
        inspect_epub_package(source)


def test_epub_rebuild_streams_unchanged_unreferenced_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "streamed.epub"
    _write_epub2(source)
    payload = b"binary-resource" * 1_000
    _rewrite_epub_members(source, {}, {"OEBPS/unreferenced.bin": payload})
    original_read = ZipFile.read

    def guarded_read(archive: ZipFile, name, *args, **kwargs):
        filename = name.filename if hasattr(name, "filename") else name
        if filename == "OEBPS/unreferenced.bin":
            raise AssertionError("unchanged resources must be streamed")
        return original_read(archive, name, *args, **kwargs)

    monkeypatch.setattr(ZipFile, "read", guarded_read)

    translated = translate_epub(
        source,
        "es",
        lambda text, *_arguments: _translate_epub2_payload(text),
    )

    with ZipFile(BytesIO(translated.content)) as archive:
        with archive.open("OEBPS/unreferenced.bin") as resource:
            assert resource.read() == payload


def test_every_materialized_local_link_in_fixture_has_a_target(tmp_path: Path) -> None:
    source = tmp_path / "linked.epub"
    _write_epub2(source)
    result = process_document(ProcessRequest(source, convert_to_markdown=True))
    markdown = result.final_path.read_text(encoding="utf-8")

    anchor_ids = set(re.findall(r'<a id="([^"]+)"></a>', markdown))
    local_targets = set(re.findall(r"\]\(#([^)]+)\)", markdown))

    assert local_targets
    assert local_targets <= anchor_ids


def test_epub_cleaning_keeps_raw_markdown_images_and_anchors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "clean.epub"
    _write_epub2(source)

    def clean(markdown: str, *_args, **_kwargs) -> str:
        assert RESOURCE_REFERENCE_PREFIX in markdown
        assert "PZDOC EPUB ANCHOR" in markdown
        return markdown.replace("A faithful first paragraph.", "A polished first paragraph.")

    monkeypatch.setattr(processing_module, "improve_markdown", clean)
    result = process_document(
        ProcessRequest(
            source,
            convert_to_markdown=True,
            improvement_mode=ImprovementMode.CLEAN,
        ),
        settings=AppSettings(model="installed-model"),
    )

    assert result.raw_markdown_path == tmp_path / "clean.raw.md"
    assert result.final_path == tmp_path / "clean.mended.md"
    assert "A polished first paragraph." in result.final_path.read_text(encoding="utf-8")
    assert '<a id="epub-document-1-anchor-1-start"></a>' in result.final_path.read_text(
        encoding="utf-8"
    )
    assert (tmp_path / "clean.assets" / "OEBPS/images/cover photo.jpg").exists()


def test_epub_offline_translation_with_conversion_produces_translated_markdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "translate.epub"
    _write_epub2(source)

    def translate(markdown: str, language: str, **_kwargs) -> str:
        assert language == "Español"
        return markdown.replace("A faithful first paragraph.", "Un primer párrafo fiel.")

    monkeypatch.setattr(processing_module, "translate_markdown_offline", translate)
    result = process_document(
        ProcessRequest(
            source,
            convert_to_markdown=True,
            offline_translation_language="Español",
        )
    )

    assert result.final_path == tmp_path / "translate.mended.md"
    assert result.raw_markdown_path == tmp_path / "translate.raw.md"
    assert "Un primer párrafo fiel." in result.final_path.read_text(encoding="utf-8")
    assert (tmp_path / "translate.assets" / "OEBPS/images/cover photo.jpg").read_bytes() == (
        b"fake-jpeg-content"
    )


def test_epub_requires_conversion_before_cleanup_without_translation(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    _write_epub2(source)

    with pytest.raises(RequestValidationError, match="EPUB necesita convertirse"):
        process_document(
            ProcessRequest(
                source,
                convert_to_markdown=False,
                improvement_mode=ImprovementMode.CLEAN,
            ),
            settings=AppSettings(model="installed-model"),
        )


def _translate_chapterless_payload(text: str) -> str:
    return text.replace("Original paragraph", "Párrafo traducido").replace(
        "This is deliberately substantial text",
        "Este es un texto deliberadamente sustancial",
    )


def _translate_epub2_payload(text: str) -> str:
    replacements = (
        ("Small Book", "Libro pequeño"),
        ("Chapter One", "Capítulo uno"),
        ("Chapter Two", "Capítulo dos"),
        ("A faithful first paragraph.", "Un primer párrafo fiel."),
        ("Read the", "Lee el"),
        ("next chapter", "capítulo siguiente"),
        ("Visit", "Visita"),
        ("official site", "sitio oficial"),
        (
            "A deliberately long promotional sentence that crosses a page boundary and asks "
            "readers to",
            "Una oración promocional deliberadamente larga que cruza un límite de página y "
            "pide a los lectores",
        ),
        ("visit", "visitar"),
        ("for details", "para obtener detalles"),
        ("Cover", "Portada"),
        ("A faithful second paragraph.", "Un segundo párrafo fiel."),
        ("Return to", "Vuelve al"),
        ("chapter one", "capítulo uno"),
        ("Document Outline", "Esquema del documento"),
    )
    translated = text
    for source, target in replacements:
        translated = translated.replace(source, target)
    return translated


def _write_epub2(destination: Path) -> None:
    container = """<?xml version="1.0" encoding="UTF-8"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""
    package = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" xmlns:opf="http://www.idpf.org/2007/opf"
         version="2.0" unique-identifier="book-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Small Book</dc:title>
    <dc:creator opf:role="aut">Parsezen Author</dc:creator>
    <dc:language>en</dc:language>
    <dc:identifier id="book-id">small-book-id</dc:identifier>
    <meta name="cover" content="cover-image"/>
  </metadata>
  <manifest>
    <item id="chapter-1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
    <item id="chapter-2" href="chapter2.xhtml" media-type="application/xhtml+xml"/>
    <item id="toc" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="cover-image" href="images/cover%20photo.jpg" media-type="image/jpeg"/>
  </manifest>
  <spine toc="toc">
    <itemref idref="chapter-1"/>
    <itemref idref="chapter-2"/>
  </spine>
</package>
"""
    toc = """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <navMap>
    <navPoint id="one" playOrder="1">
      <navLabel><text>Chapter One</text></navLabel>
      <content src="chapter1.xhtml#start"/>
    </navPoint>
    <navPoint id="two" playOrder="2">
      <navLabel><text>Chapter Two</text></navLabel>
      <content src="chapter2.xhtml#start"/>
    </navPoint>
  </navMap>
</ncx>
"""
    chapter_one = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head><title>Chapter One</title></head>
  <body>
    <p><a id="start"></a></p><p>Chapter One</p>
    <p>A faithful first paragraph. Read the <a href="chapter2.xhtml#start">next chapter</a>.</p>
    <p>Visit</p><p><a href="https://example.com/book">official site</a></p>
    <p>A deliberately long promotional sentence that crosses a page boundary and asks readers to
    visit <a href="https://example.com/split">www.exam</a></p>
    <p><a href="https://example.com/split">ple.com</a> for details.</p>
    <p><img src="images/cover%20photo.jpg" alt="Cover"/></p>
  </body>
</html>
"""
    chapter_two = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head><title>Chapter Two</title></head>
  <body>
    <p><a id="start"></a></p><p>Chapter Two</p>
    <p>A faithful second paragraph. Return to <a href="chapter1.xhtml#start">chapter one</a>.</p>
    <h1>Document Outline</h1>
  </body>
</html>
"""
    with ZipFile(destination, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
        archive.writestr("META-INF/container.xml", container, compress_type=ZIP_DEFLATED)
        archive.writestr("OEBPS/content.opf", package, compress_type=ZIP_DEFLATED)
        archive.writestr("OEBPS/toc.ncx", toc, compress_type=ZIP_DEFLATED)
        archive.writestr("OEBPS/chapter1.xhtml", chapter_one, compress_type=ZIP_DEFLATED)
        archive.writestr("OEBPS/chapter2.xhtml", chapter_two, compress_type=ZIP_DEFLATED)
        archive.writestr(
            "OEBPS/images/cover photo.jpg",
            b"fake-jpeg-content",
            compress_type=ZIP_DEFLATED,
        )


def _rewrite_epub_members(
    destination: Path,
    replacements: dict[str, bytes],
    additions: dict[str, bytes] | None = None,
) -> None:
    with ZipFile(destination) as archive:
        entries = [
            (info.filename, archive.read(info), info.compress_type) for info in archive.infolist()
        ]
        comment = archive.comment
    with ZipFile(destination, "w") as archive:
        archive.comment = comment
        for name, content, compression in entries:
            archive.writestr(name, replacements.get(name, content), compress_type=compression)
        for name, content in (additions or {}).items():
            archive.writestr(name, content, compress_type=ZIP_DEFLATED)


def _write_epub3(destination: Path) -> None:
    container = """<?xml version="1.0" encoding="UTF-8"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles><rootfile full-path="EPUB/package.opf"/></rootfiles>
</container>
"""
    package = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Modern Book</dc:title><dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="opening" href="opening.xhtml" media-type="application/xhtml+xml"/>
    <item id="cover" href="cover.jpg" media-type="image/jpeg" properties="cover-image"/>
  </manifest>
  <spine><itemref idref="opening"/></spine>
</package>
"""
    navigation = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
  <head><title>Contents</title></head>
  <body>
    <nav epub:type="toc"><ol><li><a href="opening.xhtml#start">Opening</a></li></ol></nav>
  </body>
</html>
"""
    opening = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head><title>Opening</title></head>
  <body>
    <svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">
      <image xlink:href="cover.jpg" aria-label="Cover image"/>
    </svg>
    <p id="start">Opening</p><p>Modern EPUB content.</p>
  </body>
</html>
"""
    with ZipFile(destination, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
        archive.writestr("META-INF/container.xml", container, compress_type=ZIP_DEFLATED)
        archive.writestr("EPUB/package.opf", package, compress_type=ZIP_DEFLATED)
        archive.writestr("EPUB/nav.xhtml", navigation, compress_type=ZIP_DEFLATED)
        archive.writestr("EPUB/opening.xhtml", opening, compress_type=ZIP_DEFLATED)
        archive.writestr("EPUB/cover.jpg", b"modern-cover", compress_type=ZIP_DEFLATED)


def _write_chapterless_epub(
    destination: Path,
    *,
    structured_paragraphs: bool = True,
) -> None:
    container = """<?xml version="1.0" encoding="UTF-8"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles><rootfile full-path="EPUB/package.opf"/></rootfiles>
</container>
"""
    package = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"/>
  <manifest>
    <item id="book" href="book.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="book"/></spine>
</package>
"""
    if structured_paragraphs:
        body = "\n".join(
            (f"<p>Original paragraph {index}. {'This is deliberately substantial text. ' * 12}</p>")
            for index in range(1, 31)
        )
    else:
        body = "<div>Loose prose <strong>nested emphasis</strong> and a tail.</div>"
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head><title>Untitled</title></head>
  <body>{body}</body>
</html>
"""
    with ZipFile(destination, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
        archive.writestr("META-INF/container.xml", container, compress_type=ZIP_DEFLATED)
        archive.writestr("EPUB/package.opf", package, compress_type=ZIP_DEFLATED)
        archive.writestr("EPUB/book.xhtml", content, compress_type=ZIP_DEFLATED)
