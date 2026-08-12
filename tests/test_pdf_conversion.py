from __future__ import annotations

import json
import zlib
from dataclasses import replace
from pathlib import Path
from statistics import median
from types import SimpleNamespace

import pytest

import parsezen.pdf_conversion as pdf_conversion_module
from parsezen.cancellation import CancellationToken
from parsezen.conversion import convert_file
from parsezen.errors import ConversionError, ProcessingCancelledError
from parsezen.pdf_conversion import (
    PdfPageRange,
    PdfProgressPhase,
    PdfQualityReport,
    convert_pdf_document,
    extract_pdf_warning_pages,
    render_pdf_page_cover,
    resolve_pdf_page_range,
)


def test_preserves_a_meaningful_pdf_image_as_a_portable_resource(tmp_path: Path) -> None:
    source = tmp_path / "illustrated.pdf"
    _write_image_pdf(
        source,
        native_text="A sufficiently useful paragraph remains available as native PDF text.",
        discrete_image=True,
    )
    progress: list[tuple[PdfProgressPhase, int, int]] = []

    converted = convert_pdf_document(
        source,
        include_images=True,
        load_ocr_checkpoint=lambda _page: "",
        on_progress=lambda phase, current, total: progress.append((phase, current, total)),
    )

    assert len(converted.resources) == 1
    resource = converted.resources[0]
    assert resource.media_type == "image/jpeg"
    assert resource.relative_path.as_posix() == "pdf/page-0001-image-01.jpg"
    assert resource.content.startswith(b"\xff\xd8")
    assert "__parsezen_resources__/pdf/page-0001-image-01.jpg" in converted.markdown
    assert (PdfProgressPhase.IMAGES, 1, 1) in progress


def test_preserves_a_curve_based_pdf_illustration_as_a_portable_resource(
    tmp_path: Path,
) -> None:
    source = tmp_path / "vector-illustration.pdf"
    _write_vector_pdf(source)

    converted = convert_pdf_document(source, include_images=True)

    assert len(converted.resources) == 1
    assert converted.resources[0].content.startswith(b"\xff\xd8")
    assert "__parsezen_resources__/pdf/page-0001-image-01.jpg" in converted.markdown


def test_extracts_pdf_headings_text_and_links(tmp_path: Path) -> None:
    source = tmp_path / "structured.pdf"
    _write_structured_pdf(source)

    reports: list[PdfQualityReport] = []
    markdown = convert_pdf_document(source, on_quality_report=reports.append).markdown

    assert "# Parsezen PDF" in markdown
    assert "Faithful paragraph." in markdown
    assert "All letters remain." in markdown
    assert "history continues." in markdown
    assert "Use printer!**" in markdown
    assert "History 11" not in markdown
    assert "[Official site](<https://example.com/docs>)" in markdown
    assert "[Next page](<#page-2>)" in markdown
    assert '<a id="page-1"></a>' not in markdown
    assert '<a id="page-2"></a>' in markdown
    assert reports[0].low_confidence_pages == ()


def test_extracts_only_the_selected_pdf_pages_and_keeps_original_page_numbers(
    tmp_path: Path,
) -> None:
    source = tmp_path / "structured.pdf"
    _write_structured_pdf(source)

    markdown = convert_file(source, pdf_page_range=PdfPageRange(2, 2))

    assert "Second page" in markdown
    assert "More faithful content." in markdown
    assert "Parsezen PDF" not in markdown
    assert "Faithful paragraph." not in markdown


def test_renders_a_real_pdf_page_as_an_epub_cover(tmp_path: Path) -> None:
    source = tmp_path / "structured.pdf"
    _write_structured_pdf(source)

    cover = render_pdf_page_cover(source, 2)

    assert cover.startswith(b"\xff\xd8")
    assert len(cover) > 1_000


def test_epub_cover_renderer_rejects_invalid_or_missing_pages(tmp_path: Path) -> None:
    source = tmp_path / "structured.pdf"
    _write_structured_pdf(source)

    with pytest.raises(ConversionError, match="no es válida"):
        render_pdf_page_cover(source, True)  # type: ignore[arg-type]
    with pytest.raises(ConversionError, match="no contiene la página 3"):
        render_pdf_page_cover(source, 3)


def test_reports_real_pdf_page_progress_for_extraction_and_structuring(tmp_path: Path) -> None:
    source = tmp_path / "structured.pdf"
    _write_structured_pdf(source)
    progress: list[tuple[PdfProgressPhase, int, int]] = []

    convert_file(
        source,
        on_pdf_progress=lambda phase, current, total: progress.append((phase, current, total)),
    )

    assert progress == [
        (PdfProgressPhase.EXTRACTING, 1, 2),
        (PdfProgressPhase.EXTRACTING, 2, 2),
        (PdfProgressPhase.STRUCTURING, 1, 2),
        (PdfProgressPhase.STRUCTURING, 2, 2),
    ]


def test_pdf_native_extraction_resumes_from_encrypted_page_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "structured.pdf"
    _write_structured_pdf(source)
    checkpoints: dict[int, str] = {}
    cancellation = CancellationToken()

    def save_checkpoint(page_number: int, payload: str) -> bool:
        checkpoints[page_number] = payload
        return True

    def pause_after_first_page(phase: PdfProgressPhase, current: int, _total: int) -> None:
        if phase is PdfProgressPhase.EXTRACTING and current == 1:
            cancellation.cancel()

    with pytest.raises(ProcessingCancelledError):
        convert_file(
            source,
            cancellation=cancellation,
            on_pdf_progress=pause_after_first_page,
            save_pdf_page_checkpoint=save_checkpoint,
        )

    assert set(checkpoints) == {1}
    original_deduplicate = pdf_conversion_module._deduplicated_page
    extracted_pages = 0

    def count_extraction(page):
        nonlocal extracted_pages
        extracted_pages += 1
        return original_deduplicate(page)

    monkeypatch.setattr(pdf_conversion_module, "_deduplicated_page", count_extraction)

    markdown = convert_file(
        source,
        load_pdf_page_checkpoint=checkpoints.get,
        save_pdf_page_checkpoint=save_checkpoint,
    )

    assert extracted_pages == 1
    assert set(checkpoints) == {1, 2}
    assert "# Parsezen PDF" in markdown
    assert "Second page" in markdown
    assert "Official site" in markdown


def test_pdf_native_page_checkpoint_rejects_damage_or_a_different_page(
    tmp_path: Path,
) -> None:
    source = tmp_path / "structured.pdf"
    _write_structured_pdf(source)
    checkpoints: dict[int, str] = {}
    convert_file(
        source,
        save_pdf_page_checkpoint=lambda page, payload: not checkpoints.__setitem__(page, payload),
    )
    wrong_page = json.loads(checkpoints[1])
    wrong_page["page"] = 2
    obsolete_version = json.loads(checkpoints[1])
    obsolete_version["version"] -= 1

    assert pdf_conversion_module._deserialize_page_checkpoint(None, 1) is None
    assert pdf_conversion_module._deserialize_page_checkpoint("not-json", 1) is None
    assert pdf_conversion_module._deserialize_page_checkpoint("\ud800", 1) is None
    assert (
        pdf_conversion_module._deserialize_page_checkpoint(
            json.dumps(wrong_page),
            1,
        )
        is None
    )
    assert (
        pdf_conversion_module._deserialize_page_checkpoint(
            json.dumps(obsolete_version),
            1,
        )
        is None
    )


def test_resolves_page_ranges_against_the_real_pdf_length(tmp_path: Path) -> None:
    source = tmp_path / "structured.pdf"
    _write_structured_pdf(source)

    assert resolve_pdf_page_range(source, PdfPageRange(1, 10)) == PdfPageRange(1, 2)

    with pytest.raises(ConversionError, match="tiene 2 páginas.*página 3"):
        resolve_pdf_page_range(source, PdfPageRange(3, 10))


@pytest.mark.parametrize(
    "page_range",
    [
        PdfPageRange(0, 1),
        PdfPageRange(2, 1),
        PdfPageRange(True, 2),
    ],
)
def test_rejects_invalid_pdf_page_ranges(tmp_path: Path, page_range: PdfPageRange) -> None:
    source = tmp_path / "structured.pdf"
    _write_structured_pdf(source)

    with pytest.raises(ConversionError, match="rango de páginas"):
        resolve_pdf_page_range(source, page_range)


def test_rejects_a_file_with_a_pdf_extension_but_no_pdf_header(tmp_path: Path) -> None:
    source = tmp_path / "broken.pdf"
    source.write_text("This is not a PDF.", encoding="utf-8")

    with pytest.raises(ConversionError, match="PDF válido"):
        convert_file(source)


def test_extracts_sorted_unique_pages_from_pdf_review_warnings() -> None:
    markdown = """# Result

> **Aviso OCR (página 9):** revisa esta página.

**Contenido reconocido en imágenes (página 4)**

> **Aviso de conversión (página 2):** revisa esta página.

> **Aviso OCR (página 9):** aviso repetido.
"""

    assert extract_pdf_warning_pages(markdown) == (2, 9)


def test_table_renderer_uses_markdown_html_and_reviewable_text_fallbacks() -> None:
    simple = (("Name", "Value"), ("A", "1"), ("B", "2"))
    assert pdf_conversion_module._table_rendering(simple, 2).value == "markdown"
    assert "| Name | Value |" in pdf_conversion_module._markdown_table(simple)

    multiline = (("Name", "Notes"), ("A", "first\nsecond"))
    assert pdf_conversion_module._table_rendering(multiline, 2).value == "html"
    assert "<br>" in pdf_conversion_module._html_table(multiline)

    complex_rows = tuple((f"row-{index}", "value") for index in range(81))
    assert pdf_conversion_module._table_rendering(complex_rows, 2).value == "structured_text"
    blocks = []
    table = pdf_conversion_module._PdfTable(
        (10.0, 10.0, 200.0, 300.0),
        complex_rows,
        pdf_conversion_module._TableRendering.STRUCTURED_TEXT,
    )
    pdf_conversion_module._append_pdf_table(blocks, 7, table)
    assert any(block.kind == "warning" and "página 7" in block.text for block in blocks)
    assert any("Tabla recuperada" in block.text for block in blocks)


def test_explains_that_an_empty_pdf_has_nothing_to_recognize(tmp_path: Path) -> None:
    source = tmp_path / "empty.pdf"
    _write_empty_pdf(source)

    with pytest.raises(ConversionError, match="no contiene texto ni imágenes"):
        convert_file(source)


def test_uses_selective_ocr_for_a_scanned_pdf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "scan.pdf"
    _write_image_pdf(source)
    ocr_started: list[bool] = []
    requested_pages: list[set[int]] = []
    reports: list[PdfQualityReport] = []

    def recognize(_source: Path, pages: set[int]) -> dict[int, str]:
        requested_pages.append(pages)
        return {1: "# Documento escaneado\n\nTexto reconocido con precisión."}

    monkeypatch.setattr(pdf_conversion_module, "convert_pdf_pages_with_ocr", recognize)

    markdown = convert_file(
        source,
        on_ocr_start=lambda: ocr_started.append(True),
        on_pdf_quality_report=reports.append,
    )

    assert requested_pages == [{1}]
    assert ocr_started == [True]
    assert "# Documento escaneado" in markdown
    assert "Texto reconocido con precisión." in markdown
    assert reports[0].processed_pages == (1,)
    assert reports[0].ocr_pages == (1,)
    assert reports[0].ocr_replaced_pages == (1,)
    assert reports[0].low_confidence_pages == (1,)
    assert reports[0].issues[0].page_number == 1
    assert "Documento escaneado" in reports[0].issues[0].markdown


def test_selective_ocr_reuses_a_valid_page_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "scan.pdf"
    _write_image_pdf(source)

    def unexpected_ocr(*_args, **_kwargs):
        raise AssertionError("A cached OCR page must not be recognized again.")

    monkeypatch.setattr(pdf_conversion_module, "convert_pdf_pages_with_ocr", unexpected_ocr)
    progress: list[tuple[PdfProgressPhase, int, int]] = []

    markdown = convert_file(
        source,
        on_pdf_progress=lambda phase, current, total: progress.append((phase, current, total)),
        load_pdf_ocr_checkpoint=lambda page: (
            "# Documento recuperado\n\nTexto reconocido previamente." if page == 1 else None
        ),
    )

    assert "Documento recuperado" in markdown
    assert (PdfProgressPhase.OCR, 1, 1) in progress


def test_selective_ocr_reuses_an_empty_page_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "mixed.pdf"
    _write_image_pdf(
        source,
        native_text="Native content remains available.",
        discrete_image=True,
    )

    def unexpected_ocr(*_args, **_kwargs):
        raise AssertionError("An analyzed empty OCR page must not run again.")

    monkeypatch.setattr(pdf_conversion_module, "convert_pdf_pages_with_ocr", unexpected_ocr)

    markdown = convert_file(
        source,
        load_pdf_ocr_checkpoint=lambda page: "" if page == 1 else None,
    )

    assert "Native content remains available." in markdown


def test_forced_ocr_analyzes_all_selected_pages_as_full_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "structured.pdf"
    _write_structured_pdf(source)
    requested: list[tuple[set[int], set[int]]] = []

    def recognize(
        _source: Path,
        pages: set[int],
        *,
        force_full_page_numbers: set[int],
    ) -> dict[int, str]:
        requested.append((pages, force_full_page_numbers))
        return {}

    monkeypatch.setattr(pdf_conversion_module, "convert_pdf_pages_with_ocr", recognize)

    markdown = convert_file(
        source,
        pdf_page_range=PdfPageRange(2, 2),
        force_pdf_ocr=True,
    )

    assert requested == [({2}, {2})]
    assert "Second page" in markdown
    assert "Parsezen PDF" not in markdown


def test_forced_ocr_returns_a_reviewable_warning_for_a_decorative_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "decoration.pdf"
    _write_image_pdf(source)
    monkeypatch.setattr(
        pdf_conversion_module,
        "convert_pdf_pages_with_ocr",
        lambda *_args, **_kwargs: {},
    )

    reports: list[PdfQualityReport] = []
    with pytest.raises(ConversionError, match="contenido textual útil"):
        convert_pdf_document(
            source,
            force_ocr=True,
            on_quality_report=reports.append,
        )

    assert reports[-1].issues[0].page_number == 1
    assert reports[-1].issues[0].blocking
    assert "no se encontró texto legible" in reports[-1].issues[0].message


def test_keeps_better_native_text_when_optional_ocr_adds_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "mixed.pdf"
    _write_image_pdf(source, native_text="Native content remains.", discrete_image=True)
    monkeypatch.setattr(
        pdf_conversion_module,
        "convert_pdf_pages_with_ocr",
        lambda _source, _pages: {1: "OCR"},
    )

    markdown = convert_file(source)

    assert "Native content remains." in markdown
    assert "\nOCR\n" not in f"\n{markdown}\n"


def test_keeps_native_text_and_warns_when_optional_ocr_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "mixed-ocr-failure.pdf"
    _write_image_pdf(source, native_text="Native content here.", discrete_image=True)

    def fail_ocr(_source: Path, _pages: set[int]) -> dict[int, str]:
        raise ConversionError("OCR unavailable")

    monkeypatch.setattr(pdf_conversion_module, "convert_pdf_pages_with_ocr", fail_ocr)

    reports: list[PdfQualityReport] = []
    markdown = convert_pdf_document(source, on_quality_report=reports.append).markdown

    assert "Native content here." in markdown
    assert "Aviso OCR" not in markdown
    assert reports[-1].issues[0].page_number == 1
    assert "no se pudieron analizar" in reports[-1].issues[0].message


def test_fails_when_a_scanned_pdf_cannot_run_required_ocr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "scan-ocr-failure.pdf"
    _write_image_pdf(source)

    def fail_ocr(_source: Path, _pages: set[int]) -> dict[int, str]:
        raise ConversionError("OCR unavailable")

    monkeypatch.setattr(pdf_conversion_module, "convert_pdf_pages_with_ocr", fail_ocr)

    with pytest.raises(ConversionError, match="OCR unavailable"):
        convert_file(source)


def test_appends_only_new_text_found_inside_a_mixed_page_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "mixed-with-caption.pdf"
    native_text = "Native content remains complete and reliable on this mixed PDF page."
    _write_image_pdf(source, native_text=native_text, discrete_image=True)
    monkeypatch.setattr(
        pdf_conversion_module,
        "convert_pdf_pages_with_ocr",
        lambda _source, _pages: {
            1: (
                f"{native_text}\n\nCaption found only inside the image.\n\n"
                "Caption found only inside the image."
            )
        },
    )

    markdown = convert_file(source)

    assert markdown.count(native_text) == 1
    assert "Contenido reconocido en imágenes" not in markdown
    assert markdown.count("Caption found only inside the image.") == 1


def test_does_not_repeat_native_text_when_ocr_joins_a_wrapped_word(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "mixed-wrapped.pdf"
    native_text = (
        "This paragraph keeps the complete original sentence with one line wrapped correctly."
    )
    _write_image_pdf(source, native_text=native_text, discrete_image=True)
    monkeypatch.setattr(
        pdf_conversion_module,
        "convert_pdf_pages_with_ocr",
        lambda _source, _pages: {
            1: "This paragraph keeps the complete original sentence with one linewrapped correctly."
        },
    )

    reports: list[PdfQualityReport] = []
    markdown = convert_pdf_document(source, on_quality_report=reports.append).markdown

    assert "Contenido reconocido en imágenes" not in markdown


def test_prefers_an_ocr_table_found_inside_an_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "table-image.pdf"
    _write_image_pdf(source, native_text="Quarterly figures.", discrete_image=True)
    table_markdown = """# Quarterly figures

| Quarter | Total |
| --- | ---: |
| Q1 | 120 |
| Q2 | 135 |"""
    monkeypatch.setattr(
        pdf_conversion_module,
        "convert_pdf_pages_with_ocr",
        lambda _source, _pages: {1: table_markdown},
    )

    markdown = convert_file(source)

    assert "| Quarter | Total |" in markdown
    assert "| Q2 | 135 |" in markdown


def test_repairs_known_glyph_encoding_without_replacing_native_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "glyph-map.pdf"
    _write_image_pdf(
        source,
        native_text="La atencio$n esta$ detra$s de una pra$ctica sistema$tica.",
        discrete_image=True,
    )
    monkeypatch.setattr(
        pdf_conversion_module,
        "convert_pdf_pages_with_ocr",
        lambda _source, _pages: {1: "OCR less reliable than the native layout."},
    )

    markdown = convert_file(source)

    assert "La atención está detrás de una práctica sistemática." in markdown
    assert "atencio$n" not in markdown


def test_repairs_uppercase_glyph_accent_markers() -> None:
    assert (
        pdf_conversion_module._normalize_text("PRAKCTICA, ATENCIOKN, MUKSCULOS")
        == "PRÁCTICA, ATENCIÓN, MÚSCULOS"
    )
    assert pdf_conversion_module._normalize_text("mayoría$ esta$") == "mayoría está"


def test_adaptive_ocr_quality_score_distinguishes_readable_and_garbled_text() -> None:
    readable = (
        "This paragraph contains complete words, ordinary punctuation, and a coherent "
        "reading order suitable for direct extraction."
    )
    garbled = "T $ h $ i $ s $ \ufffd A B C D E F G"

    assert pdf_conversion_module._text_quality_score(readable) > 0.8
    assert pdf_conversion_module._text_quality_score(garbled) < 0.5


def test_repairs_artificial_letter_spacing_from_character_geometry() -> None:
    raw_chars: list[dict[str, object]] = []
    x0 = 72.0
    for character in "P L A N E T S  A N D  S E C T":
        width = 3.0 if character == " " else 6.0
        raw_chars.append(
            {
                "text": character,
                "x0": x0,
                "x1": x0 + width,
                "top": 100.0,
                "bottom": 112.0,
                "size": 12.0,
                "fontname": "Body",
                "upright": True,
            }
        )
        x0 += width
    raw_line = {
        "text": "P L A N E T S  A N D  S E C T",
        "chars": raw_chars,
        "x0": 72.0,
        "x1": x0,
        "top": 100.0,
        "bottom": 112.0,
    }

    line = pdf_conversion_module._build_line(1, 600, 800, raw_line, ())

    assert line is not None
    assert line.text == "PLANETS AND SECT"


def test_repairs_a_long_spaced_word_without_joining_surrounding_prose() -> None:
    assert (
        pdf_conversion_module._repair_artificial_spacing_runs(
            "The classification by g e n d e r remains useful."
        )
        == "The classification by gender remains useful."
    )


def test_splits_widely_separated_columns_and_orders_each_column_contiguously() -> None:
    def line(text: str, x0: float, x1: float, top: float, *, bold: bool = False) -> object:
        return pdf_conversion_module._PdfLine(
            page_number=1,
            page_width=600,
            page_height=800,
            text=text,
            chars=(),
            x0=x0,
            x1=x1,
            top=top,
            bottom=top + 12,
            font_size=12,
            bold=bold,
            links=(),
            soft_hyphen_end=False,
            hard_hyphen_end=False,
            rotated=False,
        )

    heading = line("Chapter", 170, 430, 50, bold=True)
    source_order = [
        heading,
        line("Left one", 50, 270, 100),
        line("Right one", 330, 550, 100),
        line("Left two", 50, 270, 120),
        line("Right two", 330, 550, 120),
        line("Left three", 50, 270, 140),
        line("Right three", 330, 550, 140),
    ]

    ordered = pdf_conversion_module._reading_order_lines(source_order)

    assert [item.text for item in ordered] == [
        "Chapter",
        "Left one",
        "Left two",
        "Left three",
        "Right one",
        "Right two",
        "Right three",
    ]


def test_detects_a_scanned_table_from_repeated_side_by_side_text() -> None:
    def line(text: str, x0: float, x1: float, top: float, *, bold: bool = False) -> object:
        return pdf_conversion_module._PdfLine(
            page_number=1,
            page_width=600,
            page_height=800,
            text=text,
            chars=(),
            x0=x0,
            x1=x1,
            top=top,
            bottom=top + 12,
            font_size=12,
            bold=bold,
            links=(),
            soft_hyphen_end=False,
            hard_hyphen_end=False,
            rotated=False,
        )

    grid = (
        line("Key", 50, 90, 100, bold=True),
        line("Kind", 180, 220, 100, bold=True),
        line("Description", 300, 390, 100, bold=True),
        *(
            item
            for row, top in enumerate((130.0, 160.0, 190.0), start=1)
            for item in (
                line(f"Item {row}", 50, 100, top),
                line(f"Type {row}", 180, 230, top),
                line(f"Description {row}", 300, 500, top),
            )
        ),
    )
    prose = tuple(
        line(f"Complete prose line {row}", 50, 540, top)
        for row, top in enumerate(range(100, 300, 20))
    )

    assert pdf_conversion_module._has_spatial_table_candidate(grid)
    assert not pdf_conversion_module._has_spatial_table_candidate(prose)


def test_spatial_table_boundaries_use_real_gutters_without_cutting_wide_cells() -> None:
    def line(text: str, x0: float, x1: float, top: float) -> object:
        return pdf_conversion_module._PdfLine(
            page_number=1,
            page_width=432,
            page_height=648,
            text=text,
            chars=(),
            x0=x0,
            x1=x1,
            top=top,
            bottom=top + 10,
            font_size=10,
            bold=False,
            links=(),
            soft_hyphen_end=False,
            hard_hyphen_end=False,
            rotated=False,
        )

    lines = tuple(
        item
        for top in (70.0, 100.0, 130.0)
        for item in (
            line("Aries", 30, 65, top),
            line("Fire", 129, 165, top),
            line("Cardinal/Coagula", 228, 304, top),
            line("Fixed/Conjunctio", 329, 396, top),
        )
    )
    rules = tuple(
        pdf_conversion_module._RasterHorizontalRule(20, top, 410)
        for top in (60.0, 90.0, 120.0, 150.0, 180.0)
    )
    boundaries = pdf_conversion_module._spatial_table_boundaries(
        lines,
        pdf_conversion_module._side_by_side_line_indexes(lines, 432),
        432,
        648,
        rules,
    )

    assert boundaries is not None
    vertical, _horizontal = boundaries
    assert 304 < vertical[-2] < 329


def test_spatial_table_rules_split_at_an_explicit_caption() -> None:
    caption = pdf_conversion_module._PdfLine(
        page_number=1,
        page_width=432,
        page_height=648,
        text="Table 2. Secondary correspondences",
        chars=(),
        x0=100,
        x1=330,
        top=160,
        bottom=172,
        font_size=12,
        bold=True,
        links=(),
        soft_hyphen_end=False,
        hard_hyphen_end=False,
        rotated=False,
    )
    rules = tuple(
        pdf_conversion_module._RasterHorizontalRule(20, top, 410)
        for top in (60.0, 90.0, 120.0, 200.0, 230.0, 260.0)
    )

    groups = pdf_conversion_module._spatial_table_rule_groups(rules, (caption,))

    assert tuple(len(group) for group in groups) == (3, 3)


def test_short_captioned_table_keeps_columns_with_blank_cells_in_the_first_row() -> None:
    def line(text: str, x0: float, x1: float, top: float) -> object:
        return pdf_conversion_module._PdfLine(
            page_number=1,
            page_width=432,
            page_height=648,
            text=text,
            chars=(),
            x0=x0,
            x1=x1,
            top=top,
            bottom=top + 10,
            font_size=10,
            bold=False,
            links=(),
            soft_hyphen_end=False,
            hard_hyphen_end=False,
            rotated=False,
        )

    lines = (
        line("Element", 130, 175, 70),
        line("Mode of sign", 230, 300, 70),
        line("Mode of decan", 330, 405, 70),
        line("Aries 1", 30, 70, 100),
        line("Fire", 130, 155, 100),
        line("Cardinal", 330, 375, 100),
    )
    rules = tuple(
        pdf_conversion_module._RasterHorizontalRule(20, top, 410) for top in (60.0, 90.0, 120.0)
    )

    boundaries = pdf_conversion_module._spatial_table_boundaries(
        lines,
        pdf_conversion_module._side_by_side_line_indexes(lines, 432),
        432,
        648,
        rules,
        allow_singleton_columns=True,
    )

    assert boundaries is not None
    vertical, horizontal = boundaries
    assert len(vertical) == 5
    assert 70 < vertical[1] < 130
    assert 175 < vertical[2] < 230
    assert 300 < vertical[3] < 330
    assert len(horizontal) == 3


def test_recovers_a_raster_ruled_table_without_absorbing_following_prose(
    tmp_path: Path,
) -> None:
    source = tmp_path / "raster-ruled-table.pdf"
    _write_raster_ruled_table_pdf(source)

    page = pdf_conversion_module._extract_pages(source, None)[0]

    assert len(page.tables) == 1
    assert page.has_table
    assert page.tables[0].rows == (
        ("Key", "Count", "Description"),
        ("Alpha", "17", "First description\ncontinues here"),
        ("Beta", "23", "Second description"),
        ("Gamma", "41", "Third description"),
    )
    assert page.tables[0].bbox[3] < next(
        line.top for line in page.lines if line.text.startswith("Following prose")
    )
    assert (
        pdf_conversion_module._deserialize_page_checkpoint(
            pdf_conversion_module._serialize_page_checkpoint(page),
            1,
        )
        == page
    )


def test_table_cells_join_discretionary_and_typesetting_hyphens() -> None:
    assert pdf_conversion_module._normalize_table_cell("pre\u00ad\ntending") == "pretending"
    assert pdf_conversion_module._render_table_cell("wak-\nling") == "wakling"
    assert pdf_conversion_module._render_table_cell("ISO-\n9001") == "ISO-\n9001"


def test_table_rendering_preserves_an_empty_header_without_inventing_a_label() -> None:
    rows = (("", "Image"), ("Aries I", "A figure"))

    assert pdf_conversion_module._markdown_table(rows).startswith("|  | Image |")
    assert "<th></th><th>Image</th>" in pdf_conversion_module._html_table(rows)
    assert "Columna" not in pdf_conversion_module._html_table(rows)


def test_recovers_a_short_captioned_table_with_only_outer_raster_rules(tmp_path: Path) -> None:
    source = tmp_path / "short-raster-ruled-table.pdf"
    _write_short_raster_ruled_table_pdf(source)

    page = pdf_conversion_module._extract_pages(source, None)[0]

    assert len(page.tables) == 1
    assert page.tables[0].rows == (
        ("Decan", "Name", "Source", "Image"),
        ("Aries 1", "Chontare", "Aulathamas", "First description continues here"),
        ("Aries 2", "Chontachre", "Sabaoth", "Second description continues here"),
    )


def test_raster_ruled_table_with_a_link_stays_in_the_normal_text_flow(tmp_path: Path) -> None:
    source = tmp_path / "linked-raster-table.pdf"
    _write_raster_ruled_table_pdf(source, linked=True)

    page = pdf_conversion_module._extract_pages(source, None)[0]

    assert page.tables == ()
    assert not page.has_table
    assert any(line.links for line in page.lines)


def test_table_checkpoint_accepts_complex_output_but_rejects_unsafe_geometry(
    tmp_path: Path,
) -> None:
    source = tmp_path / "raster-ruled-table.pdf"
    _write_raster_ruled_table_pdf(source)
    page = pdf_conversion_module._extract_pages(source, None)[0]
    rows = (("First", "Second"),) + tuple(
        (f"Item {index}", f"Value {index}") for index in range(80)
    )
    complex_page = replace(
        page,
        tables=(
            pdf_conversion_module._PdfTable(
                page.tables[0].bbox,
                rows,
                pdf_conversion_module._TableRendering.STRUCTURED_TEXT,
            ),
        ),
    )
    payload = pdf_conversion_module._serialize_page_checkpoint(complex_page)

    assert pdf_conversion_module._deserialize_page_checkpoint(payload, 1) == complex_page

    unsafe_bbox = json.loads(payload)
    unsafe_bbox["tables"][0]["bbox"] = [0, 0, 100_000, 100_000]
    ragged = json.loads(payload)
    ragged["tables"][0]["rows"][1].append("unexpected")

    assert pdf_conversion_module._deserialize_page_checkpoint(json.dumps(unsafe_bbox), 1) is None
    assert pdf_conversion_module._deserialize_page_checkpoint(json.dumps(ragged), 1) is None


def test_orders_three_text_columns_contiguously() -> None:
    def line(text: str, x0: float, x1: float, top: float, *, bold: bool = False) -> object:
        return pdf_conversion_module._PdfLine(
            page_number=1,
            page_width=600,
            page_height=800,
            text=text,
            chars=(),
            x0=x0,
            x1=x1,
            top=top,
            bottom=top + 12,
            font_size=10,
            bold=bold,
            links=(),
            soft_hyphen_end=False,
            hard_hyphen_end=False,
            rotated=False,
        )

    heading = line("Index", 170, 430, 50, bold=True)
    source_order = [heading]
    for row, top in enumerate((100.0, 120.0, 140.0), start=1):
        source_order.extend(
            (
                line(f"Left {row}", 40, 170, top),
                line(f"Middle {row}", 240, 370, top),
                line(f"Right {row}", 440, 570, top),
            )
        )

    ordered = pdf_conversion_module._reading_order_lines(source_order)

    assert [item.text for item in ordered] == [
        "Index",
        "Left 1",
        "Left 2",
        "Left 3",
        "Middle 1",
        "Middle 2",
        "Middle 3",
        "Right 1",
        "Right 2",
        "Right 3",
    ]


def test_pairs_a_detached_toc_page_number_column_by_visual_row() -> None:
    number_link = pdf_conversion_module._PdfLink("#page-9", 500, 520, 100, 112)

    def line(
        text: str,
        x0: float,
        x1: float,
        top: float,
        *,
        links: tuple[pdf_conversion_module._PdfLink, ...] = (),
    ) -> pdf_conversion_module._PdfLine:
        return pdf_conversion_module._PdfLine(
            page_number=1,
            page_width=600,
            page_height=800,
            text=text,
            chars=(),
            x0=x0,
            x1=x1,
            top=top,
            bottom=top + 12,
            font_size=10,
            bold=False,
            links=links,
            soft_hyphen_end=False,
            hard_hyphen_end=False,
            rotated=False,
        )

    source_order = [
        line("Table of Contents", 60, 240, 50),
        line("Opening", 70, 150, 100),
        line("First chapter", 70, 190, 120),
        line("Second chapter", 70, 205, 140),
        line("9", 500, 510, 100, links=(number_link,)),
        line("17", 500, 520, 120),
        line("31", 500, 520, 140),
    ]

    normalized = pdf_conversion_module._normalize_toc_entry_rows(source_order)

    assert [item.text for item in normalized] == [
        "Table of Contents",
        "Opening 9",
        "First chapter 17",
        "Second chapter 31",
    ]
    assert len(normalized[1].links) == 1
    assert normalized[1].links[0].target == number_link.target
    assert (normalized[1].links[0].x0, normalized[1].links[0].x1) == (70, 510)


def test_leaves_an_uncertain_small_page_number_column_untouched() -> None:
    lines = [
        _pdf_model_line(1, "Contents"),
        _pdf_model_line(1, "Opening"),
        replace(_pdf_model_line(1, "9"), x0=500, x1=510),
        _pdf_model_line(1, "First chapter"),
        replace(_pdf_model_line(1, "17"), x0=500, x1=520),
        _pdf_model_line(1, "Body note"),
    ]

    assert pdf_conversion_module._normalize_toc_entry_rows(lines) == lines


def test_pairs_toc_folios_without_interleaving_two_existing_columns() -> None:
    def line(text: str, x0: float, x1: float, top: float) -> pdf_conversion_module._PdfLine:
        return replace(
            _pdf_model_line(1, text),
            x0=x0,
            x1=x1,
            top=top,
            bottom=top + 10,
            font_size=10,
        )

    source_order = [
        line("Contents", 40, 170, 40),
        line("Left one", 40, 130, 100),
        line("Left two", 40, 130, 120),
        line("Left three", 40, 140, 140),
        line("9", 250, 260, 100),
        line("17", 250, 270, 120),
        line("25", 250, 270, 140),
        line("Right one", 330, 430, 100),
        line("Right two", 330, 430, 120),
        line("Right three", 330, 440, 140),
        line("33", 550, 570, 100),
        line("41", 550, 570, 120),
        line("49", 550, 570, 140),
    ]

    normalized = pdf_conversion_module._normalize_toc_entry_rows(source_order)

    assert [item.text for item in normalized] == [
        "Contents",
        "Left one 9",
        "Left two 17",
        "Left three 25",
        "Right one 33",
        "Right two 41",
        "Right three 49",
    ]


def test_repairs_toc_spacing_and_roman_glyphs_only_with_native_heading_consensus() -> None:
    def line(
        page_number: int,
        text: str,
        *,
        top: float = 50,
        size: float = 18,
        bold: bool = True,
    ) -> pdf_conversion_module._PdfLine:
        return pdf_conversion_module._PdfLine(
            page_number=page_number,
            page_width=600,
            page_height=800,
            text=text,
            chars=(),
            x0=70,
            x1=530,
            top=top,
            bottom=top + size,
            font_size=size,
            bold=bold,
            links=(),
            soft_hyphen_end=False,
            hard_hyphen_end=False,
            rotated=False,
        )

    pages = [
        pdf_conversion_module._PdfPage(
            number=number,
            lines=(reference,),
            has_images=False,
            image_area_ratios=(),
            has_table=False,
            image_orientation_mismatch=False,
        )
        for number, reference in enumerate(
            (
                line(10, "ARIES I: THE AXE"),
                line(11, "ARIES II: THE CROWN"),
                line(12, "ARIES III: THE ROSE"),
                line(
                    13,
                    "table 4. Gods and Spirits from The 36 Airs of the Zodiac",
                    size=10,
                    bold=False,
                ),
                line(
                    14,
                    "table 13. Angelic correspondences, from Book T and 777",
                    size=10,
                    bold=False,
                ),
            ),
            start=10,
        )
    ]
    exact, romans = pdf_conversion_module._native_toc_heading_references(
        pages,
        body_size=10,
        heading_sizes={18.0: 2},
        repeated_margins=set(),
    )
    toc_lines = [
        line(1, "ARIES 1 53", size=10, bold=False),
        line(1, "ARIES 11 59", size=10, bold=False),
        line(1, "ARIES III 64", size=10, bold=False),
        line(
            1,
            "4- Gods and Spirits from The 3 6 Airs of the Zodiac 279",
            size=10,
            bold=False,
        ),
        line(
            1,
            "13. Angelic correspondences, from Book Tand 777 310",
            size=10,
            bold=False,
        ),
        line(1, "TAURUS 11 74", size=10, bold=False),
    ]

    repaired = pdf_conversion_module._repair_toc_entries_from_native_headings(
        toc_lines,
        exact,
        romans,
    )

    assert [item.text for item in repaired] == [
        "ARIES I 53",
        "ARIES II 59",
        "ARIES III 64",
        "4- Gods and Spirits from The 36 Airs of the Zodiac 279",
        "13. Angelic correspondences, from Book T and 777 310",
        "TAURUS 11 74",
    ]


def test_renders_toc_entries_as_distinct_markdown_list_rows() -> None:
    def line(
        text: str,
        x0: float,
        x1: float,
        top: float,
        *,
        size: float = 10,
    ) -> pdf_conversion_module._PdfLine:
        return pdf_conversion_module._PdfLine(
            page_number=1,
            page_width=600,
            page_height=800,
            text=text,
            chars=(),
            x0=x0,
            x1=x1,
            top=top,
            bottom=top + size,
            font_size=size,
            bold=False,
            links=(),
            soft_hyphen_end=False,
            hard_hyphen_end=False,
            rotated=False,
        )

    page = pdf_conversion_module._PdfPage(
        number=1,
        lines=(
            line("Table of Contents", 60, 260, 50, size=18),
            line("Opening", 70, 150, 100, size=18),
            line("First chapter", 70, 190, 120),
            line("Second chapter", 70, 205, 140),
            line("9", 500, 510, 100),
            line("17", 500, 520, 120),
            line("31", 500, 520, 140),
        ),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    markdown, _issues = pdf_conversion_module._render_document(
        [page],
        body_size=10,
        heading_sizes={18.0: 2},
        repeated_margins=set(),
        referenced_pages=set(),
        ocr_pages={},
        ocr_failed_pages=set(),
    )

    assert "## Table of Contents" in markdown
    assert "- Opening 9\n- First chapter 17\n- Second chapter 31" in markdown


def test_separates_unnumbered_toc_sections_from_adjacent_list_entries() -> None:
    blocks = [
        pdf_conversion_module._MarkdownBlock("toc", "- PISCES III 258", 6),
        pdf_conversion_module._MarkdownBlock("toc", "**APPENDICES**", 6),
        pdf_conversion_module._MarkdownBlock("toc", "- Decanic Magic 266", 6),
        pdf_conversion_module._MarkdownBlock(
            "toc",
            "**TABLES OF CORRESPONDENCE**",
            6,
        ),
    ]

    assert pdf_conversion_module._blocks_to_markdown(blocks) == (
        "- PISCES III 258\n\n**APPENDICES**\n\n- Decanic Magic 266\n\n**TABLES OF CORRESPONDENCE**"
    )


def test_table_ocr_requires_faithful_size_tokens_and_numbers() -> None:
    def line(text: str, top: float) -> object:
        return pdf_conversion_module._PdfLine(
            page_number=1,
            page_width=600,
            page_height=800,
            text=text,
            chars=(),
            x0=50,
            x1=540,
            top=top,
            bottom=top + 12,
            font_size=12,
            bold=False,
            links=(),
            soft_hyphen_end=False,
            hard_hyphen_end=False,
            rotated=False,
        )

    page = pdf_conversion_module._PdfPage(
        number=1,
        lines=(
            line("Quarter Total", 100),
            line("Q1 120", 120),
            line("Q2 135", 140),
        ),
        has_images=True,
        image_area_ratios=(0.95,),
        has_table=True,
        image_orientation_mismatch=False,
        tables=(
            pdf_conversion_module._PdfTable(
                (40, 80, 560, 160),
                (("Quarter", "Total"), ("Q1", "120"), ("Q2", "135")),
                pdf_conversion_module._TableRendering.MARKDOWN,
            ),
        ),
    )
    faithful = "| Quarter | Total |\n| --- | --- |\n| Q1 | 120 |\n| Q2 | 135 |"
    changed_number = faithful.replace("135", "136")
    exploded = "\n".join(faithful for _ in range(6))

    assert pdf_conversion_module._table_ocr_is_faithful(page, faithful)
    assert not pdf_conversion_module._table_ocr_is_faithful(page, changed_number)
    assert not pdf_conversion_module._table_ocr_is_faithful(page, exploded)


def test_table_ocr_cannot_drop_a_column_with_an_empty_data_cell() -> None:
    def line(text: str, top: float) -> object:
        return pdf_conversion_module._PdfLine(
            page_number=1,
            page_width=432,
            page_height=648,
            text=text,
            chars=(),
            x0=40,
            x1=405,
            top=top,
            bottom=top + 10,
            font_size=10,
            bold=False,
            links=(),
            soft_hyphen_end=False,
            hard_hyphen_end=False,
            rotated=False,
        )

    page = pdf_conversion_module._PdfPage(
        number=1,
        lines=(
            line("DECAN QUALITY IMAGE", 70),
            line("Aries I A complete figure description", 100),
        ),
        has_images=True,
        image_area_ratios=(0.95,),
        has_table=True,
        image_orientation_mismatch=False,
        tables=(
            pdf_conversion_module._PdfTable(
                (30, 60, 410, 120),
                (("DECAN", "QUALITY", "IMAGE"), ("Aries I", "", "A complete figure description")),
                pdf_conversion_module._TableRendering.MARKDOWN,
            ),
        ),
    )
    missing_empty_column = (
        "| DECAN | IMAGE |\n| --- | --- |\n| Aries I | A complete figure description |"
    )

    assert not pdf_conversion_module._table_ocr_is_faithful(page, missing_empty_column)


def test_repairs_one_short_ocr_insertion_from_a_repeated_native_title() -> None:
    target = pdf_conversion_module._PdfPage(
        number=1,
        lines=(),
        has_images=True,
        image_area_ratios=(0.96,),
        has_table=False,
        image_orientation_mismatch=False,
    )
    reference_line = _pdf_model_line(
        2,
        "The History Astrology and Magic of the Decans",
        centered=True,
    )
    reference = pdf_conversion_module._PdfPage(
        number=2,
        lines=(
            reference_line,
            _pdf_model_line(
                2,
                "Additional reliable publication text confirms the native layer quality.",
            ),
        ),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )
    raw = {1: "36 FACES\r\n\r\nThe History Astrology and Magic of Che Decans\r\n\r\nA. Writer"}

    assert pdf_conversion_module._native_page_quality(reference) >= 0.75
    assert pdf_conversion_module._is_reliable_native_title_line(
        reference,
        reference_line,
        reference_line.text,
        pdf_conversion_module._title_word_tokens(reference_line.text),
        12,
    )
    repaired = pdf_conversion_module._repair_repeated_front_matter_ocr_titles(
        [target, reference],
        raw,
        12,
    )

    assert raw[1] == (
        "36 FACES\r\n\r\nThe History Astrology and Magic of Che Decans\r\n\r\nA. Writer"
    )
    assert repaired[1] == (
        "36 FACES\r\n\r\nThe History Astrology and Magic of the Decans\r\n\r\nA. Writer"
    )


def test_preserves_an_expanded_ocr_title_with_two_substantive_words() -> None:
    target = pdf_conversion_module._PdfPage(
        number=1,
        lines=(),
        has_images=True,
        image_area_ratios=(0.96,),
        has_table=False,
        image_orientation_mismatch=False,
    )
    reference = pdf_conversion_module._PdfPage(
        number=2,
        lines=(
            _pdf_model_line(
                2,
                "The Complete History of Stellar Magic",
                centered=True,
            ),
        ),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )
    markdown = "The Complete History Revised Edition of Stellar Magic"

    repaired = pdf_conversion_module._repair_repeated_front_matter_ocr_titles(
        [target, reference],
        {1: markdown},
        12,
    )

    assert repaired[1] == markdown


def test_preserves_an_ocr_title_when_native_repetitions_disagree() -> None:
    target = pdf_conversion_module._PdfPage(
        number=1,
        lines=(),
        has_images=True,
        image_area_ratios=(0.96,),
        has_table=False,
        image_orientation_mismatch=False,
    )
    references = [
        pdf_conversion_module._PdfPage(
            number=number,
            lines=(_pdf_model_line(number, text, centered=True),),
            has_images=False,
            image_area_ratios=(),
            has_table=False,
            image_orientation_mismatch=False,
        )
        for number, text in (
            (2, "The History Astrology and Magic of the Decans"),
            (3, "The History of Astrology and Magic of the Decans"),
        )
    ]
    markdown = "The History of Che Astrology and Magic of the Decans"

    repaired = pdf_conversion_module._repair_repeated_front_matter_ocr_titles(
        [target, *references],
        {1: markdown},
        12,
    )

    assert repaired[1] == markdown


def test_splits_one_extracted_line_when_two_columns_are_separated_by_a_wide_gap() -> None:
    characters = (
        pdf_conversion_module._PdfCharacter("Left", 50, 74, 100, 112, 12, True),
        pdf_conversion_module._PdfCharacter(" ", 74, 78, 100, 112, 12, True),
        pdf_conversion_module._PdfCharacter("text", 78, 102, 100, 112, 12, True),
        pdf_conversion_module._PdfCharacter("Right", 330, 360, 100, 112, 12, True),
        pdf_conversion_module._PdfCharacter(" ", 360, 364, 100, 112, 12, True),
        pdf_conversion_module._PdfCharacter("text", 364, 388, 100, 112, 12, True),
    )
    combined = pdf_conversion_module._PdfLine(
        page_number=1,
        page_width=600,
        page_height=800,
        text="Left text Right text",
        chars=characters,
        x0=50,
        x1=388,
        top=100,
        bottom=112,
        font_size=12,
        bold=False,
        links=(),
        soft_hyphen_end=False,
        hard_hyphen_end=False,
        rotated=False,
    )

    parts = pdf_conversion_module._split_wide_line(combined)

    assert [part.text for part in parts] == ["Left text", "Right text"]


def test_splits_scanned_columns_separated_by_a_moderate_gutter() -> None:
    characters = (
        pdf_conversion_module._PdfCharacter("Left", 50, 74, 100, 112, 10, True),
        pdf_conversion_module._PdfCharacter("text", 78, 102, 100, 112, 10, True),
        pdf_conversion_module._PdfCharacter("Right", 140, 170, 100, 112, 10, True),
        pdf_conversion_module._PdfCharacter("text", 174, 198, 100, 112, 10, True),
    )
    combined = pdf_conversion_module._PdfLine(
        page_number=1,
        page_width=600,
        page_height=800,
        text="Left text Right text",
        chars=characters,
        x0=50,
        x1=198,
        top=100,
        bottom=112,
        font_size=10,
        bold=False,
        links=(),
        soft_hyphen_end=False,
        hard_hyphen_end=False,
        rotated=False,
    )

    assert [part.text for part in pdf_conversion_module._split_wide_line(combined)] == [
        "Left text",
        "Right text",
    ]


def test_recovers_a_clustered_vector_illustration_as_an_image_box() -> None:
    page = SimpleNamespace(
        width=600,
        height=800,
        bbox=(0, 0, 600, 800),
        images=[],
        curves=[
            {"x0": 120, "x1": 220, "top": 180, "bottom": 300},
            {"x0": 210, "x1": 320, "top": 190, "bottom": 310},
            {"x0": 150, "x1": 300, "top": 290, "bottom": 390},
        ],
    )
    model = pdf_conversion_module._PdfPage(
        number=1,
        lines=(),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    boxes = pdf_conversion_module._exportable_image_boxes(page, model, None)

    assert len(boxes) == 1
    x0, top, x1, bottom = boxes[0]
    assert x0 <= 120
    assert top <= 180
    assert x1 >= 320
    assert bottom >= 390


def test_spatial_character_deduplication_preserves_the_original_choice() -> None:
    characters: list[dict[str, object]] = []
    for index in range(200):
        x0 = float((index % 20) * 9)
        top = float((index // 20) * 15)
        original_id = f"original-{index}"
        shared = {
            "text": "A",
            "fontname": "Body",
            "size": 10.0,
            "upright": True,
        }
        characters.extend(
            (
                {
                    **shared,
                    "id": original_id,
                    "x0": x0,
                    "x1": x0 + 5,
                    "top": top,
                    "doctop": top,
                    "bottom": top + 10,
                },
                {
                    **shared,
                    "id": f"duplicate-{index}",
                    "x0": x0 + 0.05,
                    "x1": x0 + 5.05,
                    "top": top + 0.05,
                    "doctop": top + 0.05,
                    "bottom": top + 10.05,
                },
            )
        )

    clusters: list[list[dict[str, object]]] = []
    for character in sorted(
        characters,
        key=lambda item: (float(item["doctop"]), float(item["x0"])),
    ):
        matching = next(
            (
                cluster
                for cluster in clusters
                if any(
                    pdf_conversion_module._character_overlap(character, member) >= 0.55
                    for member in cluster
                )
            ),
            None,
        )
        if matching is None:
            clusters.append([character])
        else:
            matching.append(character)
    source_order = {id(character): index for index, character in enumerate(characters)}
    expected = []
    for cluster in clusters:
        median_x0 = median(float(character["x0"]) for character in cluster)
        median_top = median(float(character["top"]) for character in cluster)
        expected.append(
            min(
                cluster,
                key=lambda character: (
                    abs(float(character["x0"]) - median_x0)
                    + abs(float(character["top"]) - median_top)
                ),
            )
        )
    expected.sort(key=lambda character: source_order[id(character)])

    retained = pdf_conversion_module._deduplicate_characters(characters)

    assert [character["id"] for character in retained] == [
        character["id"] for character in expected
    ]


def test_never_joins_paragraphs_across_pdf_pages() -> None:
    previous = pdf_conversion_module._PdfLine(
        page_number=1,
        page_width=600,
        page_height=800,
        text="A paragraph without final punctuation",
        chars=(),
        x0=72,
        x1=500,
        top=700,
        bottom=714,
        font_size=12,
        bold=False,
        links=(),
        soft_hyphen_end=False,
        hard_hyphen_end=False,
        rotated=False,
    )
    current = pdf_conversion_module._PdfLine(
        page_number=2,
        page_width=600,
        page_height=800,
        text="A new page starts here.",
        chars=(),
        x0=72,
        x1=300,
        top=72,
        bottom=86,
        font_size=12,
        bold=False,
        links=(),
        soft_hyphen_end=False,
        hard_hyphen_end=False,
        rotated=False,
    )

    assert not pdf_conversion_module._should_join_lines(previous, current, 12, 24)


def test_pdf_link_without_visible_text_is_kept_outside_the_paragraph() -> None:
    target = "https://example.com/vector-destination"
    line = pdf_conversion_module._PdfLine(
        page_number=1,
        page_width=600,
        page_height=800,
        text="The paragraph remains untouched.",
        chars=(),
        x0=72,
        x1=500,
        top=200,
        bottom=214,
        font_size=12,
        bold=False,
        links=(pdf_conversion_module._PdfLink(target, 300, 340, 200, 214),),
        soft_hyphen_end=False,
        hard_hyphen_end=False,
        rotated=False,
    )
    page = pdf_conversion_module._PdfPage(
        number=1,
        lines=(line,),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    markdown, _issues = pdf_conversion_module._render_document(
        [page],
        body_size=12,
        heading_sizes={},
        repeated_margins=set(),
        referenced_pages=set(),
        ocr_pages={},
        ocr_failed_pages=set(),
    )

    assert "The paragraph remains untouched." in markdown
    assert "[enlace]" not in markdown.casefold()
    assert "**Destinos conservados**" in markdown
    assert "[Abrir example.com](<https://example.com/vector-destination>)" in markdown

    restored_ocr = pdf_conversion_module._restore_page_links(
        "OCR keeps the recognized paragraph intact.",
        page,
    )
    assert "[enlace]" not in restored_ocr.casefold()
    assert "[Abrir example.com](<https://example.com/vector-destination>)" in restored_ocr


def test_removes_a_native_margin_number_merged_into_ocr_cover_text() -> None:
    margin_number = pdf_conversion_module._PdfLine(
        page_number=3,
        page_width=600,
        page_height=800,
        text="3",
        chars=(),
        x0=295,
        x1=305,
        top=748,
        bottom=762,
        font_size=12,
        bold=False,
        links=(),
        soft_hyphen_end=False,
        hard_hyphen_end=False,
        rotated=False,
    )
    page = pdf_conversion_module._PdfPage(
        number=3,
        lines=(margin_number,),
        has_images=True,
        image_area_ratios=(0.95,),
        has_table=False,
        image_orientation_mismatch=False,
    )

    markdown, _issues = pdf_conversion_module._render_document(
        [page],
        body_size=12,
        heading_sizes={},
        repeated_margins=set(),
        referenced_pages=set(),
        ocr_pages={3: "# Example title\n\n3 Example City\n\nExample Publisher\n\n2024"},
        ocr_failed_pages=set(),
    )

    assert "3 Example City" not in markdown
    assert "Example City" in markdown
    assert "2024" in markdown


def test_omits_a_standalone_page_number_in_the_top_margin() -> None:
    line = pdf_conversion_module._PdfLine(
        page_number=68,
        page_width=600,
        page_height=800,
        text="68",
        chars=(),
        x0=295,
        x1=305,
        top=28,
        bottom=42,
        font_size=10,
        bold=False,
        links=(),
        soft_hyphen_end=False,
        hard_hyphen_end=False,
        rotated=False,
    )

    assert pdf_conversion_module._omit_margin_line(
        line,
        repeated_margins=set(),
        previous=None,
        body_size=12,
    )


def test_omits_an_outer_folio_below_the_narrow_top_margin() -> None:
    line = pdf_conversion_module._PdfLine(
        page_number=96,
        page_width=600,
        page_height=800,
        text="64",
        chars=(),
        x0=85,
        x1=99,
        top=120,
        bottom=134,
        font_size=10,
        bold=False,
        links=(),
        soft_hyphen_end=False,
        hard_hyphen_end=False,
        rotated=False,
    )

    assert pdf_conversion_module._omit_margin_line(
        line,
        repeated_margins=set(),
        previous=None,
        body_size=12,
    )


def test_omits_a_folio_joined_to_a_chapter_running_header() -> None:
    line = pdf_conversion_module._PdfLine(
        page_number=96,
        page_width=600,
        page_height=800,
        text="64 CHAPTER 5",
        chars=(),
        x0=120,
        x1=330,
        top=120,
        bottom=134,
        font_size=10,
        bold=False,
        links=(),
        soft_hyphen_end=False,
        hard_hyphen_end=False,
        rotated=False,
    )

    assert pdf_conversion_module._omit_margin_line(
        line,
        repeated_margins=set(),
        previous=None,
        body_size=12,
    )


def test_removes_a_native_numbered_running_header_from_ocr() -> None:
    line = pdf_conversion_module._PdfLine(
        page_number=96,
        page_width=600,
        page_height=800,
        text="64 CHAPTER 5",
        chars=(),
        x0=120,
        x1=330,
        top=120,
        bottom=134,
        font_size=10,
        bold=False,
        links=(),
        soft_hyphen_end=False,
        hard_hyphen_end=False,
        rotated=False,
    )
    page = pdf_conversion_module._PdfPage(
        number=96,
        lines=(line,),
        has_images=True,
        image_area_ratios=(0.95,),
        has_table=False,
        image_orientation_mismatch=False,
    )

    markdown = pdf_conversion_module._strip_native_margin_numbers_from_ocr(
        page,
        "64 CHAPTER 5\n\nVisible content",
        12,
    )

    assert "64 CHAPTER 5" not in markdown
    assert "Visible content" in markdown


def test_omits_a_short_running_header_joined_to_a_trailing_folio() -> None:
    line = pdf_conversion_module._PdfLine(
        page_number=87,
        page_width=600,
        page_height=800,
        text="GENDER 63",
        chars=(),
        x0=300,
        x1=500,
        top=120,
        bottom=134,
        font_size=8,
        bold=False,
        links=(),
        soft_hyphen_end=False,
        hard_hyphen_end=False,
        rotated=False,
    )

    assert pdf_conversion_module._omit_margin_line(
        line,
        repeated_margins=set(),
        previous=None,
        body_size=10,
    )


def test_keeps_a_numbered_figure_label_inside_the_text_column() -> None:
    line = pdf_conversion_module._PdfLine(
        page_number=87,
        page_width=600,
        page_height=800,
        text="FIGURE 5",
        chars=(),
        x0=120,
        x1=240,
        top=120,
        bottom=134,
        font_size=8,
        bold=False,
        links=(),
        soft_hyphen_end=False,
        hard_hyphen_end=False,
        rotated=False,
    )

    assert not pdf_conversion_module._omit_margin_line(
        line,
        repeated_margins=set(),
        previous=None,
        body_size=10,
    )


def test_keeps_a_centered_section_number_in_the_same_top_band() -> None:
    line = pdf_conversion_module._PdfLine(
        page_number=96,
        page_width=600,
        page_height=800,
        text="5",
        chars=(),
        x0=296,
        x1=304,
        top=120,
        bottom=134,
        font_size=12,
        bold=True,
        links=(),
        soft_hyphen_end=False,
        hard_hyphen_end=False,
        rotated=False,
    )

    assert not pdf_conversion_module._omit_margin_line(
        line,
        repeated_margins=set(),
        previous=None,
        body_size=12,
    )


def test_does_not_merge_toc_blocks_from_different_pages() -> None:
    blocks = [
        pdf_conversion_module._MarkdownBlock("toc", "First 1", 1),
        pdf_conversion_module._MarkdownBlock("toc", "Second 2", 2),
    ]

    assert pdf_conversion_module._blocks_to_markdown(blocks) == "First 1\n\nSecond 2"


def test_pdf_nests_explicit_chapters_under_a_container_only_with_two_siblings() -> None:
    blocks = [
        pdf_conversion_module._MarkdownBlock("heading", "Part I — Origins", 1, level=2),
        pdf_conversion_module._MarkdownBlock("paragraph", "Opening context.", 1),
        pdf_conversion_module._MarkdownBlock("heading", "Chapter 1 — Roots", 1, level=2),
        pdf_conversion_module._MarkdownBlock("paragraph", "Roots.", 1),
        pdf_conversion_module._MarkdownBlock("heading", "Chapter 2 — Branches", 1, level=2),
        pdf_conversion_module._MarkdownBlock("paragraph", "Branches.", 1),
        pdf_conversion_module._MarkdownBlock("heading", "Part II — Return", 1, level=2),
        pdf_conversion_module._MarkdownBlock("heading", "Chapter 3 — Home", 1, level=2),
    ]

    assert pdf_conversion_module._blocks_to_markdown(blocks) == (
        "## Part I — Origins\n\nOpening context.\n\n"
        "### Chapter 1 — Roots\n\nRoots.\n\n"
        "### Chapter 2 — Branches\n\nBranches.\n\n"
        "## Part II — Return\n\n## Chapter 3 — Home"
    )


def test_pdf_leaves_an_ambiguous_container_flat() -> None:
    blocks = [
        pdf_conversion_module._MarkdownBlock("heading", "Parte I", 1, level=4),
        pdf_conversion_module._MarkdownBlock("heading", "Chapter 1", 1, level=5),
    ]

    assert pdf_conversion_module._blocks_to_markdown(blocks) == "#### Parte I\n\n##### Chapter 1"


def test_repairs_a_hyphenated_word_across_a_pdf_page_marker() -> None:
    blocks = [
        pdf_conversion_module._MarkdownBlock("paragraph", "muchos proble-", 1),
        pdf_conversion_module._MarkdownBlock(
            "provenance",
            "<!-- PZDOC PDF PAGE 2 -->",
            2,
        ),
        pdf_conversion_module._MarkdownBlock(
            "paragraph",
            "mas se resuelven con calma.",
            2,
        ),
    ]

    markdown = pdf_conversion_module._blocks_to_markdown(blocks)

    assert markdown == "muchos proble<!-- PZDOC PDF PAGE 2 -->mas se resuelven con calma."
    assert (
        pdf_conversion_module.strip_pdf_page_markers(markdown)
        == "muchos problemas se resuelven con calma."
    )


def test_does_not_join_a_markdown_separator_across_a_pdf_page_marker() -> None:
    blocks = [
        pdf_conversion_module._MarkdownBlock("paragraph", "---", 1),
        pdf_conversion_module._MarkdownBlock(
            "provenance",
            "<!-- PZDOC PDF PAGE 2 -->",
            2,
        ),
        pdf_conversion_module._MarkdownBlock("paragraph", "nuevo capítulo", 2),
    ]

    markdown = pdf_conversion_module._blocks_to_markdown(blocks)

    assert markdown == "---\n\n<!-- PZDOC PDF PAGE 2 -->\n\nnuevo capítulo"


def test_repairs_a_hyphenated_word_split_across_false_heading_blocks() -> None:
    def line(
        text: str, *, hard_hyphen_end: bool, bold: bool = False
    ) -> pdf_conversion_module._PdfLine:
        return pdf_conversion_module._PdfLine(
            page_number=1,
            page_width=600,
            page_height=800,
            text=text,
            chars=(),
            x0=72,
            x1=500,
            top=200,
            bottom=214,
            font_size=12,
            bold=bold,
            links=(),
            soft_hyphen_end=False,
            hard_hyphen_end=hard_hyphen_end,
            rotated=False,
        )

    first = line("Knowledge desper-", hard_hyphen_end=True)
    second = line("tarte in time and atra-", hard_hyphen_end=True)
    third = line("pado in a dream.", hard_hyphen_end=False, bold=True)
    blocks = [
        pdf_conversion_module._MarkdownBlock(
            "paragraph", "Knowledge desper-", 1, source_line=first
        ),
        pdf_conversion_module._MarkdownBlock(
            "heading", "tarte in time and atra-", 1, level=3, source_line=second
        ),
        pdf_conversion_module._MarkdownBlock(
            "paragraph", "**pado in a dream.** More text.", 1, source_line=third
        ),
    ]

    assert pdf_conversion_module._blocks_to_markdown(blocks) == (
        "Knowledge despertarte in time and **atrapado in a dream.** More text."
    )


def _pdf_model_line(
    page_number: int,
    text: str,
    *,
    centered: bool = False,
    bold: bool = False,
) -> pdf_conversion_module._PdfLine:
    return pdf_conversion_module._PdfLine(
        page_number=page_number,
        page_width=600,
        page_height=800,
        text=text,
        chars=(),
        x0=100 if centered else 72,
        x1=500 if centered else 420,
        top=180,
        bottom=194,
        font_size=12,
        bold=bold,
        links=(),
        soft_hyphen_end=False,
        hard_hyphen_end=False,
        rotated=False,
    )


def test_omits_a_vertical_decorative_glyph_run_and_warns(tmp_path: Path) -> None:
    source = tmp_path / "vertical-glyphs.pdf"
    _write_vertical_glyph_pdf(source)

    reports: list[PdfQualityReport] = []
    markdown = convert_pdf_document(source, on_quality_report=reports.append).markdown

    surrounded = f"\n{markdown}\n"
    assert "Readable content remains." in markdown
    assert all(f"\n{glyph}\n" not in surrounded for glyph in "XQZJ")
    assert "Aviso de conversión" not in markdown
    assert reports[-1].issues[0].page_number == 1


def _write_structured_pdf(destination: Path) -> None:
    page_one_content = b"""BT
/F2 24 Tf
72 700 Td
(Parsezen PDF) Tj
ET
BT
/F2 24 Tf
72.4 700.2 Td
(Parsezen PDF) Tj
ET
BT
/F2 24 Tf
72.8 700.4 Td
(Parsezen PDF) Tj
ET
BT
/F1 12 Tf
72 660 Td
(Faithful paragraph.) Tj
ET
BT
/F1 12 Tf
72 640 Td
(All letters remain.) Tj
ET
BT
/F1 12 Tf
72 620 Td
(Official site) Tj
ET
BT
/F1 12 Tf
72 580 Td
(Next page) Tj
ET
BT
/F1 12 Tf
72 540 Td
(his-) Tj
ET
BT
/F1 12 Tf
72 526 Td
(tory continues.) Tj
ET
BT
/F1 12 Tf
72 490 Td
(Use printer!**) Tj
ET
BT
/F1 10 Tf
72 45 Td
(History 11) Tj
ET"""
    page_two_content = b"""BT
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
            b"/Contents 7 0 R /Annots [9 0 R 10 0 R] >>"
        ),
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> "
            b"/Contents 8 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
        _stream(page_one_content),
        _stream(page_two_content),
        (
            b"<< /Type /Annot /Subtype /Link /Rect [72 615 145 635] "
            b"/Border [0 0 0] /A << /S /URI /URI (https://example.com/docs) >> >>"
        ),
        b"<< /Type /Annot /Subtype /Link /Rect [72 575 135 595] /Border [0 0 0] "
        b"/Dest [4 0 R /Fit] >>",
    ]
    _write_pdf(destination, objects)


def _write_empty_pdf(destination: Path) -> None:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>",
        _stream(b""),
    ]
    _write_pdf(destination, objects)


def _write_raster_ruled_table_pdf(destination: Path, *, linked: bool = False) -> None:
    width, height = 600, 800
    pixels = bytearray(b"\xff" * (width * height))
    for top in (80, 115, 165, 205, 245):
        for y in (top, top + 1):
            pixels[y * width + 20 : y * width + 580] = b"\x00" * 560
    compressed = zlib.compress(bytes(pixels), level=9)
    image = (
        (
            f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
            f"/ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode "
            f"/Length {len(compressed)} >>\nstream\n"
        ).encode("ascii")
        + compressed
        + b"\nendstream"
    )
    text_rows = (
        (700, ("Key", "Count", "Description")),
        (670, ("Alpha", "17", "First description")),
        (655, ("", "", "continues here")),
        (610, ("Beta", "23", "Second description")),
        (570, ("Gamma", "41", "Third description")),
        (520, ("Following prose remains outside the table.", "", "")),
    )
    operations = [f"q\n{width} 0 0 {height} 0 0 cm\n/Im1 Do\nQ"]
    for y, cells in text_rows:
        for x, value in zip((50, 180, 300), cells, strict=True):
            if value:
                operations.append(f"BT\n/F1 12 Tf\n{x} {y} Td\n({value}) Tj\nET")
    page = (
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] "
        b"/Resources << /XObject << /Im1 4 0 R >> /Font << /F1 5 0 R >> >> "
        b"/Contents 6 0 R" + (b" /Annots [7 0 R]" if linked else b"") + b" >>"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        page,
        image,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        _stream("\n".join(operations).encode("latin-1")),
    ]
    if linked:
        objects.append(
            b"<< /Type /Annot /Subtype /Link /Rect [50 665 90 680] /Border [0 0 0] "
            b"/A << /S /URI /URI (https://example.invalid) >> >>"
        )
    _write_pdf(destination, objects)


def _write_short_raster_ruled_table_pdf(destination: Path) -> None:
    width, height = 600, 800
    pixels = bytearray(b"\xff" * (width * height))
    for top in (150, 350):
        for y in (top, top + 1):
            pixels[y * width + 40 : y * width + 560] = b"\x00" * 520
    compressed = zlib.compress(bytes(pixels), level=9)
    image = (
        (
            f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
            f"/ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode "
            f"/Length {len(compressed)} >>\nstream\n"
        ).encode("ascii")
        + compressed
        + b"\nendstream"
    )
    text_cells = (
        (700, 80, "Table 5. Short fragment"),
        (630, 50, "Decan"),
        (630, 145, "Name"),
        (630, 250, "Source"),
        (632, 345, "Image"),
        (575, 50, "Aries 1"),
        (575, 145, "Chontare"),
        (575, 250, "Aulathamas"),
        (578, 345, "First description continues here"),
        (475, 50, "Aries 2"),
        (475, 145, "Chontachre"),
        (475, 250, "Sabaoth"),
        (478, 345, "Second description continues here"),
    )
    operations = [f"q\n{width} 0 0 {height} 0 0 cm\n/Im1 Do\nQ"]
    for y, x, value in text_cells:
        operations.append(f"BT\n/F1 11 Tf\n{x} {y} Td\n({value}) Tj\nET")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] "
            b"/Resources << /XObject << /Im1 4 0 R >> /Font << /F1 5 0 R >> >> "
            b"/Contents 6 0 R >>"
        ),
        image,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        _stream("\n".join(operations).encode("latin-1")),
    ]
    _write_pdf(destination, objects)


def _write_image_pdf(
    destination: Path,
    native_text: str | None = None,
    *,
    discrete_image: bool = False,
) -> None:
    image_width = 180 if discrete_image else 612
    image_height = 180 if discrete_image else 792
    image_x = 360 if discrete_image else 0
    image_y = 430 if discrete_image else 0
    content = f"q\n{image_width} 0 0 {image_height} {image_x} {image_y} cm\n/Im1 Do\nQ"
    if native_text is not None:
        content += f"\nBT\n/F1 12 Tf\n72 700 Td\n({native_text}) Tj\nET"
    image_data = b"\xff"
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
            + image_data
            + b"\nendstream"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        _stream(content.encode("latin-1")),
    ]
    _write_pdf(destination, objects)


def _write_vertical_glyph_pdf(destination: Path) -> None:
    content = b"""BT
/F1 12 Tf
72 700 Td
(Readable content remains.) Tj
ET
BT
/F1 12 Tf
500 580 Td
(X) Tj
ET
BT
/F1 12 Tf
500 564 Td
(Q) Tj
ET
BT
/F1 12 Tf
500 548 Td
(Z) Tj
ET
BT
/F1 12 Tf
500 532 Td
(J) Tj
ET"""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        _stream(content),
    ]
    _write_pdf(destination, objects)


def _write_vector_pdf(destination: Path) -> None:
    content = b"""BT
/F1 12 Tf
72 700 Td
(Readable native text remains available.) Tj
ET
q
2 w
100 400 m 130 460 190 460 220 400 c S
180 400 m 210 470 270 470 300 400 c S
140 330 m 180 390 240 390 280 330 c S
Q"""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        _stream(content),
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
