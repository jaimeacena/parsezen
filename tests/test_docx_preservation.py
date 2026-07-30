from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from lxml import etree

from parsezen.cancellation import CancellationToken
from parsezen.docx_preservation import transform_docx
from parsezen.errors import ConversionError, ProcessingCancelledError, TranslationError

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _write_docx(path: Path) -> None:
    document = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{WORD_NS}"><w:body>
  <w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Hello </w:t></w:r><w:r><w:t>world</w:t></w:r></w:p>
  <w:p><w:r><w:t>Second paragraph</w:t></w:r></w:p>
</w:body></w:document>""".encode()
    content_types = b"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="xml" ContentType="application/xml"/>
</Types>"""
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("word/document.xml", document)
        archive.writestr("word/media/image1.png", b"private-image-bytes")
        archive.writestr("docProps/custom.xml", b"<properties>untouched</properties>")


def test_docx_transformation_preserves_package_resources_and_run_structure(
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.docx"
    _write_docx(source)

    result = transform_docx(
        source,
        lambda text: text.replace("Hello world", "Hola mundo").replace(
            "Second paragraph", "Segundo párrafo"
        ),
    )
    output = tmp_path / "result.docx"
    output.write_bytes(result.content)

    with ZipFile(output) as archive:
        assert archive.read("word/media/image1.png") == b"private-image-bytes"
        assert archive.read("docProps/custom.xml") == b"<properties>untouched</properties>"
        root = etree.fromstring(archive.read("word/document.xml"))
    text_nodes = root.findall(f".//{{{WORD_NS}}}t")
    assert "".join(node.text or "" for node in text_nodes) == "Hola mundoSegundo párrafo"
    assert root.find(f".//{{{WORD_NS}}}b") is not None
    assert result.source_text == "Hello world\n\nSecond paragraph"
    assert result.transformed_text == "Hola mundo\n\nSegundo párrafo"
    assert result.paragraph_count == 2
    assert result.preserved_resources == 1


def test_docx_transformation_rejects_a_lost_paragraph_marker(tmp_path: Path) -> None:
    source = tmp_path / "book.docx"
    _write_docx(source)

    with pytest.raises(TranslationError, match="estructura interna"):
        transform_docx(source, lambda _text: "sin marcadores")


def test_docx_transformation_rejects_empty_or_malformed_word_xml(tmp_path: Path) -> None:
    empty = tmp_path / "empty.docx"
    with ZipFile(empty, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "word/document.xml",
            f'<w:document xmlns:w="{WORD_NS}"><w:body/></w:document>',
        )
    with pytest.raises(ConversionError, match="no contiene texto"):
        transform_docx(empty, lambda text: text)

    malformed = tmp_path / "malformed.docx"
    with ZipFile(malformed, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<broken")
    with pytest.raises(ConversionError, match="no es válida"):
        transform_docx(malformed, lambda text: text)


def test_docx_transformation_honours_cancellation_before_reading(tmp_path: Path) -> None:
    source = tmp_path / "book.docx"
    _write_docx(source)
    cancellation = CancellationToken()
    cancellation.cancel()

    with pytest.raises(ProcessingCancelledError):
        transform_docx(source, lambda text: text, cancellation=cancellation)
