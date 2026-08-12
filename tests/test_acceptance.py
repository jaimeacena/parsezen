"""Repeatable acceptance checks for Parsezen's essential MVP journeys."""

from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

import parsezen.pdf_conversion as pdf_conversion_module
import parsezen.processing as processing_module
from parsezen.domain.jobs import (
    DocumentFormat,
    JobConfiguration,
    JobStatus,
    OutputConfiguration,
)
from parsezen.improvement import ImprovementMode
from parsezen.pdf_conversion import PdfPageRange
from parsezen.presentation.main_window import ParsezenMainWindow
from parsezen.processing import OutputFormat, ProcessRequest, ProcessStage, process_document
from parsezen.settings import AppSettings

pytestmark = pytest.mark.acceptance

LOCAL_SETTINGS = AppSettings(model="acceptance-model", context_window=4_096)


def test_acceptance_converts_text_and_markdown_without_changing_sources(
    tmp_path: Path,
) -> None:
    sources = tmp_path / "sources"
    outputs = tmp_path / "outputs"
    sources.mkdir()
    outputs.mkdir()
    text_source = sources / "plain.txt"
    markdown_source = sources / "notes.markdown"
    text_source.write_text("Plain text with value 2026.", encoding="utf-8")
    markdown_source.write_text(
        "# Notes\n\n[Official site](https://example.com/docs)",
        encoding="utf-8",
    )

    text_result = process_document(ProcessRequest(text_source, True, outputs))
    markdown_result = process_document(ProcessRequest(markdown_source, True, outputs))

    assert text_result.final_path.read_text(encoding="utf-8") == "Plain text with value 2026."
    assert markdown_result.final_path.read_text(encoding="utf-8") == (
        "# Notes\n\n[Official site](https://example.com/docs)"
    )
    assert text_source.read_text(encoding="utf-8") == "Plain text with value 2026."
    assert markdown_source.read_text(encoding="utf-8").startswith("# Notes")


def test_acceptance_converts_a_real_docx_through_markitdown(tmp_path: Path) -> None:
    source = tmp_path / "document.docx"
    output_directory = tmp_path / "outputs"
    output_directory.mkdir()
    _write_minimal_docx(source)
    stages: list[ProcessStage] = []

    result = process_document(
        ProcessRequest(source, True, output_directory),
        on_stage=stages.append,
    )

    markdown = result.final_path.read_text(encoding="utf-8")
    assert result.final_path == output_directory / "document.md"
    assert "# Parsezen heading" in markdown
    assert "A faithful paragraph with value 2026." in markdown
    assert stages == [
        ProcessStage.VALIDATING,
        ProcessStage.CONVERTING,
        ProcessStage.WRITING,
        ProcessStage.COMPLETED,
    ]


def test_acceptance_converts_pdf_links_and_only_the_selected_pages(tmp_path: Path) -> None:
    source = tmp_path / "structured.pdf"
    output_directory = tmp_path / "outputs"
    output_directory.mkdir()
    _write_structured_pdf(source)

    complete = process_document(ProcessRequest(source, True, output_directory))
    selected = process_document(
        ProcessRequest(
            source,
            True,
            output_directory,
            pdf_page_range=PdfPageRange(2, 2),
        )
    )

    complete_markdown = complete.final_path.read_text(encoding="utf-8")
    selected_markdown = selected.final_path.read_text(encoding="utf-8")
    assert "# Parsezen PDF" in complete_markdown
    assert "Faithful paragraph." in complete_markdown
    assert "[Official site](<https://example.com/docs>)" in complete_markdown
    assert "Second page" in complete_markdown
    assert selected.final_path == output_directory / "structured.pages-2-2.md"
    assert "Second page" in selected_markdown
    assert "More faithful content." in selected_markdown
    assert "Parsezen PDF" not in selected_markdown


def test_acceptance_routes_a_scanned_pdf_through_selective_ocr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "scan.pdf"
    _write_image_pdf(source)
    requested_pages: list[set[int]] = []
    stages: list[ProcessStage] = []
    progress: list[tuple[int, int]] = []

    def recognize(_source: Path, pages: set[int], *, on_progress=None) -> dict[int, str]:
        requested_pages.append(pages)
        if on_progress is not None:
            on_progress(1, 1)
        return {1: "# Documento escaneado\n\nTexto 2026 reconocido con precisión."}

    monkeypatch.setattr(pdf_conversion_module, "convert_pdf_pages_with_ocr", recognize)

    result = process_document(
        ProcessRequest(source, True),
        on_stage=stages.append,
        on_progress=lambda current, total: progress.append((current, total)),
    )

    assert requested_pages == [{1}]
    assert ProcessStage.OCR in stages
    assert ProcessStage.PRESERVING_IMAGES in stages
    assert ProcessStage.STRUCTURING in stages
    assert progress == [(1, 1), (1, 1), (1, 1), (1, 1)]
    assert result.problematic_pdf_pages == (1,)
    assert "Texto 2026 reconocido con precisión." in result.final_path.read_text(encoding="utf-8")


def test_acceptance_builds_an_epub_from_an_illustrated_pdf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "illustrated.pdf"
    _write_image_pdf(source, native_text="Illustrated book text.", discrete_image=True)
    monkeypatch.setattr(
        pdf_conversion_module,
        "convert_pdf_pages_with_ocr",
        lambda *_args, **_kwargs: {1: ""},
    )

    result = process_document(
        ProcessRequest(source, True, output_format=OutputFormat.EPUB),
        work_checkpoint_root=tmp_path / "checkpoints",
    )

    assert result.final_path == tmp_path / "illustrated.epub"
    assert result.preserved_images == 1
    with ZipFile(result.final_path) as archive:
        assert archive.read("mimetype") == b"application/epub+zip"
        assert "EPUB/nav.xhtml" in archive.namelist()
        assert any(name.startswith("EPUB/images/") for name in archive.namelist())


def test_acceptance_keeps_paired_raw_and_mended_outputs_without_overwriting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "document.docx"
    _write_minimal_docx(source)
    original_bytes = source.read_bytes()
    calls: list[tuple[ImprovementMode, str | None]] = []

    def improve(
        markdown: str,
        mode: ImprovementMode,
        _settings: AppSettings,
        target_language: str | None,
        **_kwargs,
    ) -> str:
        calls.append((mode, target_language))
        assert "A faithful paragraph with value 2026." in markdown
        return markdown.replace("Parsezen heading", "Encabezado Parsezen").replace(
            "A faithful paragraph with value 2026.",
            "Un párrafo fiel y pulido con el valor 2026.",
        )

    monkeypatch.setattr(processing_module, "improve_markdown", improve)
    request = ProcessRequest(
        source,
        True,
        improvement_mode=ImprovementMode.CLEAN_AND_TRANSLATE,
        target_language="Español",
    )

    first = process_document(request, settings=LOCAL_SETTINGS)
    second = process_document(request, settings=LOCAL_SETTINGS)

    assert calls == [
        (ImprovementMode.CLEAN_AND_TRANSLATE, "Español"),
        (ImprovementMode.CLEAN_AND_TRANSLATE, "Español"),
    ]
    assert first.raw_markdown_path == tmp_path / "document.raw.md"
    assert first.final_path == tmp_path / "document.mended.md"
    assert second.raw_markdown_path == tmp_path / "document-2.raw.md"
    assert second.final_path == tmp_path / "document-2.mended.md"
    assert "value 2026" in first.raw_markdown_path.read_text(encoding="utf-8")
    assert "párrafo fiel y pulido" in first.final_path.read_text(encoding="utf-8")
    assert source.read_bytes() == original_bytes


def test_acceptance_translates_offline_without_an_ai_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "notes.md"
    source.write_text(
        "# Notes\n\nValue 2026 at [site](https://example.com/docs).",
        encoding="utf-8",
    )
    stages: list[ProcessStage] = []

    def translate(markdown: str, language: str, **_kwargs) -> str:
        assert language == "Español"
        assert "2026" in markdown
        assert "https://example.com/docs" in markdown
        return "# Notas\n\nValor 2026 en [sitio](https://example.com/docs)."

    monkeypatch.setattr(processing_module, "translate_markdown_offline", translate)

    result = process_document(
        ProcessRequest(
            source,
            False,
            offline_translation_language="Español",
        ),
        on_stage=stages.append,
    )

    assert result.final_path.read_text(encoding="utf-8") == (
        "# Notas\n\nValor 2026 en [sitio](https://example.com/docs)."
    )
    assert result.review_original_path == source
    assert ProcessStage.TRANSLATING in stages
    assert source.read_text(encoding="utf-8").startswith("# Notes")


def test_acceptance_window_processes_a_document_in_the_background(
    tmp_path: Path,
    qtbot,
) -> None:
    source_directory = tmp_path / "sources"
    output_directory = tmp_path / "outputs"
    source_directory.mkdir()
    output_directory.mkdir()
    source = source_directory / "window.txt"
    source.write_text("# Window acceptance", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(output_directory=output_directory),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    job = window._job_queue.for_source(source)
    assert job is not None
    window._job_queue.configure(
        job.id,
        JobConfiguration(
            output=OutputConfiguration(
                configured=True,
                format=DocumentFormat.MARKDOWN,
                directory=output_directory,
            )
        ),
    )
    window._sync_workspace()

    window.parsezen_workspace.primary_button.click()
    runtime = window._runtime_by_job[job.id]
    qtbot.waitUntil(
        lambda: (
            runtime.result is not None or window._job_queue.get(job.id).status is JobStatus.FAILED
        ),
        timeout=3_000,
    )

    assert runtime.result is not None
    assert runtime.result.final_path == output_directory / "window.md"
    assert runtime.result.final_path.read_text(encoding="utf-8") == "# Window acceptance"


def _write_minimal_docx(destination: Path) -> None:
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
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
    document_relationships = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
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
    <w:p><w:r><w:t>A faithful paragraph with value 2026.</w:t></w:r></w:p>
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


def _write_structured_pdf(destination: Path) -> None:
    page_one = b"""BT
/F2 24 Tf
72 700 Td
(Parsezen PDF) Tj
ET
BT
/F1 12 Tf
72 660 Td
(Faithful paragraph.) Tj
ET
BT
/F1 12 Tf
72 620 Td
(Official site) Tj
ET"""
    page_two = b"""BT
/F2 18 Tf
72 700 Td
(Second page) Tj
ET
BT
/F1 12 Tf
72 660 Td
(More faithful content.) Tj
ET"""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> "
            b"/Contents 7 0 R /Annots [9 0 R] >>"
        ),
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> /Contents 8 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
        _stream(page_one),
        _stream(page_two),
        (
            b"<< /Type /Annot /Subtype /Link /Rect [72 615 145 635] "
            b"/Border [0 0 0] /A << /S /URI /URI (https://example.com/docs) >> >>"
        ),
    ]
    _write_pdf(destination, objects)


def _write_image_pdf(
    destination: Path,
    *,
    native_text: str | None = None,
    discrete_image: bool = False,
) -> None:
    image_width = 180 if discrete_image else 612
    image_height = 180 if discrete_image else 792
    image_x = 360 if discrete_image else 0
    image_y = 430 if discrete_image else 0
    content = f"q\n{image_width} 0 0 {image_height} {image_x} {image_y} cm\n/Im1 Do\nQ"
    if native_text is not None:
        content += f"\nBT\n/F1 12 Tf\n72 700 Td\n({native_text}) Tj\nET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /XObject << /Im1 4 0 R >> /Font << /F1 5 0 R >> >> "
            b"/Contents 6 0 R >>"
        ),
        (
            b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 "
            b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Length 1 >>\nstream\n"
            b"\xff\nendstream"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        _stream(content.encode("latin-1")),
    ]
    _write_pdf(destination, objects)


def _stream(content: bytes) -> bytes:
    return b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"


def _write_pdf(destination: Path, objects: list[bytes]) -> None:
    document = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_number, body in enumerate(objects, start=1):
        offsets.append(len(document))
        document.extend(f"{object_number} 0 obj\n".encode())
        document.extend(body)
        document.extend(b"\nendobj\n")

    xref_offset = len(document)
    document.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    document.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        document.extend(f"{offset:010d} 00000 n \n".encode())
    document.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode())
    document.extend(f"startxref\n{xref_offset}\n%%EOF\n".encode())
    destination.write_bytes(document)
