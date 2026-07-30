from __future__ import annotations

from io import BytesIO
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

import pytest

from parsezen.document_model import ConvertedResource
from parsezen.epub_builder import EpubBookMetadata, build_epub, iter_epub_text_documents
from parsezen.errors import FinalIntegrityError
from parsezen.final_integrity import (
    IntegrityLedger,
    binary_integrity_capture,
    text_integrity_capture,
    verify_epub_payload,
)


def test_text_capture_verifies_the_staged_content_and_structure(tmp_path: Path) -> None:
    staged = tmp_path / "result.md"
    staged.write_text("# Capítulo\n\nTexto con [enlace](https://example.com).\n", encoding="utf-8")
    capture = text_integrity_capture(
        "# Capítulo\n\nTexto con [enlace](https://example.com).\n",
        markdown=True,
    )

    capture(staged)

    assert capture.report is not None
    assert capture.report.verified
    assert capture.report.ledger.headings == 1
    assert capture.report.ledger.links == 1


def test_text_capture_blocks_a_changed_staging_file(tmp_path: Path) -> None:
    staged = tmp_path / "result.txt"
    staged.write_text("Contenido incompleto", encoding="utf-8")
    capture = text_integrity_capture("Contenido completo", markdown=False)

    with pytest.raises(FinalIntegrityError, match="control final"):
        capture(staged)

    assert capture.report is None


def test_markdown_capture_allows_only_expected_publication_rewrites(
    tmp_path: Path,
) -> None:
    expected = (
        "<!-- PZDOC EPUB ANCHOR section-one -->\n\n"
        "![Figura](__parsezen_resources__/images/figure.png)\n\n"
        "![Remota](https://example.com/original.png)\n"
    )
    staged = tmp_path / "result.md"
    staged.write_text(
        '<a id="section-one"></a>\n\n'
        "![Figura](book.assets/images/figure.png)\n\n"
        "![Remota](https://example.com/original.png)\n",
        encoding="utf-8",
    )
    text_integrity_capture(expected, markdown=True)(staged)

    staged.write_text(
        '<a id="section-one"></a>\n\n'
        "![Figura](book.assets/images/figure.png)\n\n"
        "![Remota](https://example.com/changed.png)\n",
        encoding="utf-8",
    )
    with pytest.raises(FinalIntegrityError, match="control final"):
        text_integrity_capture(expected, markdown=True)(staged)


def test_binary_capture_checks_bytes_and_the_container_before_publication(
    tmp_path: Path,
) -> None:
    staged = tmp_path / "result.bin"
    staged.write_bytes(b"validated")
    validated_paths: list[Path] = []
    capture = binary_integrity_capture(
        b"validated",
        format_label="Prueba",
        validate_container=validated_paths.append,
        ledger=IntegrityLedger(resources=1),
    )

    capture(staged)

    assert validated_paths == [staged]
    assert capture.report is not None
    assert capture.report.verified
    assert capture.report.ledger.resources == 1

    staged.write_bytes(b"changed")
    with pytest.raises(FinalIntegrityError, match="control final"):
        capture(staged)


def test_epub_payload_report_detects_a_resource_changed_after_rendering() -> None:
    resource = ConvertedResource(PurePosixPath("figure.png"), b"image", "image/png")
    built = build_epub(
        "# Capítulo\n\n![Figura](__parsezen_resources__/figure.png)\n",
        (resource,),
        EpubBookMetadata("Libro", "es"),
    )
    chapters = {
        PurePosixPath(name).name: xhtml
        for name, xhtml in iter_epub_text_documents(built.content)
        if name != "EPUB/text/cover.xhtml"
    }
    rewritten = BytesIO()
    with ZipFile(BytesIO(built.content)) as source, ZipFile(rewritten, "w") as target:
        for info in source.infolist():
            payload = source.read(info.filename)
            if info.filename == "EPUB/images/figure.png":
                payload = b"changed"
            target.writestr(info, payload)

    with pytest.raises(FinalIntegrityError, match="control final"):
        verify_epub_payload(
            rewritten.getvalue(),
            chapters,
            {"figure.png": resource},
        )

    assert built.integrity_report is not None
    assert built.integrity_report.verified
