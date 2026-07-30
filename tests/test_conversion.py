from __future__ import annotations

from base64 import b64decode
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

import parsezen.conversion as conversion_module
from parsezen.conversion import RESOURCE_REFERENCE_PREFIX, convert_document, convert_file
from parsezen.epub_builder import EpubBookMetadata, build_epub
from parsezen.errors import ConversionError
from parsezen.markdown_resources import without_markdown_images


def test_reads_markdown_directly_and_strips_utf8_bom(tmp_path: Path) -> None:
    source = tmp_path / "notes.md"
    source.write_bytes(b"\xef\xbb\xbf# Parsezen\r\n\r\nContent")

    assert convert_file(source) == "# Parsezen\n\nContent"


def test_reads_txt_as_basic_markdown(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Plain text", encoding="utf-8")

    assert convert_file(source) == "Plain text"


def test_rejects_non_utf8_text(tmp_path: Path) -> None:
    source = tmp_path / "legacy.txt"
    source.write_bytes(b"\xff\xfe\x00")

    with pytest.raises(ConversionError, match="UTF-8"):
        convert_file(source)


def test_rejects_direct_text_above_the_memory_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "large.md"
    source.write_text("12345", encoding="utf-8")
    monkeypatch.setattr(conversion_module, "MAX_DIRECT_TEXT_BYTES", 4)

    with pytest.raises(ConversionError, match="límite de 64 MiB"):
        convert_file(source)


def test_converts_minimal_docx_with_markitdown(tmp_path: Path) -> None:
    source = tmp_path / "document.docx"
    _write_minimal_docx(source)

    markdown = convert_file(source)

    assert "Parsezen heading" in markdown
    assert "A faithful paragraph." in markdown


def test_docx_images_are_available_as_portable_resources(tmp_path: Path) -> None:
    source = tmp_path / "illustrated.docx"
    _write_illustrated_docx(source)

    converted = convert_document(source, preserve_resources=True)

    assert len(converted.resources) == 1
    assert converted.resources[0].relative_path.as_posix() == "docx/image-001.png"
    assert converted.resources[0].media_type == "image/png"
    assert converted.resources[0].content.startswith(b"\x89PNG")
    assert f"{RESOURCE_REFERENCE_PREFIX}docx/image-001.png" in converted.markdown

    built = build_epub(
        converted.markdown,
        converted.resources,
        EpubBookMetadata("Illustrated"),
    )
    with ZipFile(BytesIO(built.content)) as archive:
        assert archive.read("EPUB/images/docx/image-001.png") == converted.resources[0].content


def test_docx_legacy_image_does_not_discard_the_useful_document(tmp_path: Path) -> None:
    source = tmp_path / "legacy-image.docx"
    _write_illustrated_docx(
        source,
        image_extension="emf",
        image_media_type="image/x-emf",
        image_content=b"legacy-vector-preview",
    )

    converted = convert_document(source, preserve_resources=True)

    assert "Illustrated" in converted.markdown
    assert converted.resources == ()


def test_markdown_local_images_are_collected_only_for_portable_output(tmp_path: Path) -> None:
    images = tmp_path / "images"
    images.mkdir()
    image = images / "cover image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\ncontent")
    source = tmp_path / "notes.md"
    original = "# Notes\n\n![Cover](<images/cover%20image.png>)\n"
    source.write_text(original, encoding="utf-8")

    direct = convert_document(source)
    portable = convert_document(source, preserve_resources=True)

    assert direct.markdown == original
    assert direct.resources == ()
    assert len(portable.resources) == 1
    assert portable.resources[0].content == image.read_bytes()
    assert f"{RESOURCE_REFERENCE_PREFIX}markdown/image-001.png" in portable.markdown


def test_markdown_images_can_be_removed_without_losing_alternative_text() -> None:
    markdown = (
        "# Notes\n\n![Local](figure.png)\n\n![Remote][cover]\n\n"
        '[cover]: https://example.com/cover.jpg\n\n<img src="other.jpg" alt="HTML image">\n\n'
        "![[obsidian.png|Obsidian image]]\n"
    )

    stripped = without_markdown_images(markdown)

    assert "![" not in stripped
    assert "Local" in stripped
    assert "Remote" in stripped
    assert "HTML image" in stripped
    assert "Obsidian image" in stripped
    assert "<img" not in stripped
    assert "![[" not in stripped


def test_markdown_reference_images_are_collected_and_deduplicated(tmp_path: Path) -> None:
    image = tmp_path / "figure.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\ncontent")
    source = tmp_path / "notes.md"
    source.write_text(
        '![First][hero]\n\n![Second][]\n\n[hero]: figure.png "Cover"\n[second]: <figure.png>\n',
        encoding="utf-8",
    )

    converted = convert_document(source, preserve_resources=True)

    assert len(converted.resources) == 1
    assert converted.markdown.count(RESOURCE_REFERENCE_PREFIX) == 2
    assert '"Cover"' in converted.markdown


@pytest.mark.parametrize(
    "reference, message",
    [
        ("https://example.com/cover.png", "archivos locales relativos"),
        ("../cover.png", "dentro de la carpeta"),
        ("missing.png", "No se encontró"),
    ],
)
def test_portable_markdown_rejects_images_that_cannot_be_packaged_safely(
    tmp_path: Path,
    reference: str,
    message: str,
) -> None:
    source = tmp_path / "notes.md"
    source.write_text(f"![Cover]({reference})", encoding="utf-8")

    with pytest.raises(ConversionError, match=message):
        convert_document(
            source,
            preserve_resources=True,
            strict_resource_references=True,
        )


def test_markdown_output_keeps_nonportable_images_as_original_links(tmp_path: Path) -> None:
    source = tmp_path / "notes.md"
    original = "![Remote](https://example.com/cover.png)\n\n![Missing](missing.png)\n"
    source.write_text(original, encoding="utf-8")

    converted = convert_document(source, preserve_resources=True)

    assert converted.markdown == original
    assert converted.resources == ()


def test_rejects_invalid_docx_before_markitdown_fallback(tmp_path: Path) -> None:
    source = tmp_path / "broken.docx"
    source.write_text("This is not a DOCX container.", encoding="utf-8")

    with pytest.raises(ConversionError, match="DOCX válido"):
        convert_file(source)


def test_rejects_docx_whose_expanded_content_exceeds_the_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "oversized.docx"
    _write_minimal_docx(source)
    monkeypatch.setattr(conversion_module, "_MAX_DOCX_UNCOMPRESSED_BYTES", 1)

    with pytest.raises(ConversionError, match="demasiado grande una vez descomprimido"):
        convert_file(source)


def test_rejects_docx_with_too_many_archive_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "too-many-members.docx"
    _write_minimal_docx(source)
    monkeypatch.setattr(conversion_module, "_MAX_DOCX_ARCHIVE_ENTRIES", 1)

    with pytest.raises(ConversionError, match="demasiados archivos internos"):
        convert_file(source)


def _write_minimal_docx(destination: Path) -> None:
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override
    PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override
    PartName="/word/styles.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>
"""
    package_relationships = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship
    Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    Target="word/document.xml"/>
</Relationships>
"""
    document_relationships = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship
    Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"
    Target="styles.xml"/>
</Relationships>
"""
    styles = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
  </w:style>
</w:styles>
"""
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Parsezen heading</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>A faithful paragraph.</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""

    with ZipFile(destination, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", package_relationships)
        archive.writestr("word/_rels/document.xml.rels", document_relationships)
        archive.writestr("word/styles.xml", styles)
        archive.writestr("word/document.xml", document)


def _write_illustrated_docx(
    destination: Path,
    *,
    image_extension: str = "png",
    image_media_type: str = "image/png",
    image_content: bytes | None = None,
) -> None:
    content_types = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="{image_extension}" ContentType="{image_media_type}"/>
  <Override PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>
"""
    package_relationships = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    Target="word/document.xml"/>
</Relationships>
"""
    document_relationships = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"
    Target="styles.xml"/>
  <Relationship Id="rId2"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
    Target="media/image1.{image_extension}"/>
</Relationships>
"""
    styles = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>
</w:styles>
"""
    document = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Illustrated</w:t></w:r></w:p>
    <w:p><w:r><w:drawing><wp:inline>
      <wp:extent cx="952500" cy="952500"/><wp:docPr id="1" name="Figure" descr="Cover"/>
      <a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
        <pic:pic><pic:nvPicPr>
          <pic:cNvPr id="0" name="image1.{image_extension}"/><pic:cNvPicPr/>
        </pic:nvPicPr>
        <pic:blipFill><a:blip r:embed="rId2"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>
        <pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="952500" cy="952500"/></a:xfrm>
        <a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>
      </a:graphicData></a:graphic>
    </wp:inline></w:drawing></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""
    image = image_content or b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    with ZipFile(destination, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", package_relationships)
        archive.writestr("word/_rels/document.xml.rels", document_relationships)
        archive.writestr("word/styles.xml", styles)
        archive.writestr("word/document.xml", document)
        archive.writestr(f"word/media/image1.{image_extension}", image)
