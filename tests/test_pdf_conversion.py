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
from parsezen.pdf_layout import _PdfLine


def _margin_line(page_number: int, text: str, *, top: float = 20.0) -> _PdfLine:
    return _PdfLine(
        page_number=page_number,
        page_width=600,
        page_height=800,
        text=text,
        chars=(),
        x0=40,
        x1=560,
        top=top,
        bottom=top + 12,
        font_size=10,
        bold=False,
        links=(),
        soft_hyphen_end=False,
        hard_hyphen_end=False,
        rotated=False,
    )


def test_repeated_margin_lines_detect_local_running_headers_in_a_long_book() -> None:
    lines = [
        *(_margin_line(page, "CHAPTER 6") for page in range(20, 25)),
        _margin_line(30, "ONE-OFF HEADER"),
        _margin_line(30, "ONE-OFF HEADER", top=32),
    ]

    repeated = pdf_conversion_module._repeated_margin_lines(lines, page_count=700)

    assert repeated == {pdf_conversion_module._margin_key("CHAPTER 6")}


def test_repeated_margin_lines_detect_centered_headers_below_the_strict_margin() -> None:
    lines = [
        _margin_line(20, "CHAPTER 40", top=112),
        _margin_line(21, "CHAPTER 4 0", top=112),
        _margin_line(22, "CHAPTER 40", top=112),
    ]

    repeated = pdf_conversion_module._repeated_margin_lines(lines, page_count=700)

    assert repeated == {pdf_conversion_module._margin_key("CHAPTER 40")}
    assert pdf_conversion_module._omit_margin_line(
        lines[1],
        repeated_margins=repeated,
        previous=None,
        body_size=12,
    )


def test_repairs_a_malformed_astrological_roman_from_nearby_sibling_consensus() -> None:
    lines = tuple(
        replace(
            _margin_line(145, text, top=100 + index * 80),
            italic=True,
            font_size=11,
        )
        for index, text in enumerate(("Jupiter in Virgo n", "Mars in Virgo II", "Sun in Virgo II"))
    )
    page = pdf_conversion_module._PdfPage(
        145,
        lines,
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    repaired = pdf_conversion_module._repair_astrological_series_roman_glyphs([page], 11)

    assert repaired[0].lines[0].text == "Jupiter in Virgo II"
    assert tuple(line.text for line in repaired[0].lines[1:]) == (
        "Mars in Virgo II",
        "Sun in Virgo II",
    )


def test_keeps_a_malformed_astrological_roman_without_unanimous_sibling_evidence() -> None:
    lines = tuple(
        replace(
            _margin_line(145, text, top=100 + index * 80),
            italic=True,
            font_size=11,
        )
        for index, text in enumerate(("Jupiter in Virgo n", "Mars in Virgo I", "Sun in Virgo II"))
    )
    page = pdf_conversion_module._PdfPage(
        145,
        lines,
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    repaired = pdf_conversion_module._repair_astrological_series_roman_glyphs([page], 11)

    assert repaired[0].lines[0].text == "Jupiter in Virgo n"


def test_repairs_a_broken_decan_title_from_the_complete_three_part_series() -> None:
    pages = [
        pdf_conversion_module._PdfPage(
            number,
            (
                replace(
                    _margin_line(number, text, top=120),
                    font_size=16,
                ),
            ),
            has_images=False,
            image_area_ratios=(),
            has_table=False,
            image_orientation_mismatch=False,
        )
        for number, text in (
            (160, "SCORPIO i: THE FIRST TITLE"),
            (176, "SCORPIO IE AN APPARATUS FOR MUTUAL DISTILLATION"),
            (190, "SCORPIO III: THE THIRD TITLE"),
        )
    ]

    repaired = pdf_conversion_module._repair_astrological_series_roman_glyphs(pages, 11)

    assert repaired[1].lines[0].text == "SCORPIO II: AN APPARATUS FOR MUTUAL DISTILLATION"


def test_repairs_broken_placement_romans_from_the_preceding_decan_title() -> None:
    pages = [
        pdf_conversion_module._PdfPage(
            number,
            tuple(
                replace(
                    _margin_line(number, text, top=120 + index * 80),
                    font_size=11 if " in " in text else 16,
                    italic=" in " in text,
                )
                for index, text in enumerate(lines)
            ),
            has_images=False,
            image_area_ratios=(),
            has_table=False,
            image_orientation_mismatch=False,
        )
        for number, lines in (
            (140, ("VIRGO I: THE FIRST TITLE",)),
            (142, ("VIRGO IE THE SECOND TITLE",)),
            (145, ("Jupiter in Virgo n", "Venus in Virgo it")),
            (150, ("VIRGO III: THE THIRD TITLE",)),
        )
    ]

    repaired = pdf_conversion_module._repair_astrological_series_roman_glyphs(pages, 11)

    assert repaired[1].lines[0].text == "VIRGO II: THE SECOND TITLE"
    assert tuple(line.text for line in repaired[2].lines) == (
        "Jupiter in Virgo II",
        "Venus in Virgo II",
    )


def test_keeps_a_broken_decan_title_without_a_complete_series_consensus() -> None:
    pages = [
        pdf_conversion_module._PdfPage(
            number,
            (
                replace(
                    _margin_line(number, text, top=120),
                    font_size=16,
                ),
            ),
            has_images=False,
            image_area_ratios=(),
            has_table=False,
            image_orientation_mismatch=False,
        )
        for number, text in (
            (160, "SCORPIO I: THE FIRST TITLE"),
            (176, "SCORPIO IE AN APPARATUS FOR MUTUAL DISTILLATION"),
        )
    ]

    repaired = pdf_conversion_module._repair_astrological_series_roman_glyphs(pages, 11)

    assert repaired[1].lines[0].text == "SCORPIO IE AN APPARATUS FOR MUTUAL DISTILLATION"


def test_repeated_chapter_label_survives_only_on_its_visual_opening_page() -> None:
    opening_label = _margin_line(20, "CHAPTER 14", top=190)
    title = replace(
        _margin_line(20, "EXALTATION LORDS", top=230),
        bottom=258,
        font_size=22,
    )
    exercise_label = _margin_line(21, "CHAPTER 14", top=112)
    exercise_title = replace(
        _margin_line(21, "EXERCISE 14", top=160),
        bottom=188,
        font_size=22,
    )
    running_label = _margin_line(22, "CHAPTER 14", top=112)
    later_running_label = _margin_line(23, "CHAPTER 14", top=112)
    pages = [
        pdf_conversion_module._PdfPage(
            20,
            (opening_label, title),
            has_images=False,
            image_area_ratios=(),
            has_table=False,
            image_orientation_mismatch=False,
        ),
        pdf_conversion_module._PdfPage(
            21,
            (exercise_label, exercise_title),
            has_images=False,
            image_area_ratios=(),
            has_table=False,
            image_orientation_mismatch=False,
        ),
        pdf_conversion_module._PdfPage(
            22,
            (running_label,),
            has_images=False,
            image_area_ratios=(),
            has_table=False,
            image_orientation_mismatch=False,
        ),
        pdf_conversion_module._PdfPage(
            23,
            (later_running_label,),
            has_images=False,
            image_area_ratios=(),
            has_table=False,
            image_orientation_mismatch=False,
        ),
    ]
    repeated = pdf_conversion_module._repeated_margin_lines(
        [line for page in pages for line in page.lines],
        page_count=700,
    )

    preserved = pdf_conversion_module._preserved_structural_margin_headings(
        pages,
        body_size=12,
        repeated_margins=repeated,
    )

    assert pdf_conversion_module._visual_line_key(opening_label) in preserved
    assert not pdf_conversion_module._omit_margin_line(
        opening_label,
        repeated,
        None,
        12,
        preserved_repeated_headings=preserved,
    )
    assert pdf_conversion_module._omit_margin_line(
        exercise_label,
        repeated,
        None,
        12,
        preserved_repeated_headings=preserved,
    )
    assert pdf_conversion_module._omit_margin_line(
        running_label,
        repeated,
        None,
        12,
        preserved_repeated_headings=preserved,
    )


def test_visual_chapter_opening_is_rendered_as_structure_not_body_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opening_label = _margin_line(20, "CHAPTER 14", top=190)
    opening_title = replace(
        _margin_line(20, "EXALTATION LORDS", top=230),
        bottom=258,
        font_size=22,
    )
    exercise_label = _margin_line(21, "CHAPTER 14", top=112)
    exercise_title = replace(
        _margin_line(21, "EXERCISE 14", top=160),
        bottom=188,
        font_size=22,
    )
    running_label = _margin_line(22, "CHAPTER 14", top=112)
    later_running_label = _margin_line(23, "CHAPTER 14", top=112)
    pages = [
        pdf_conversion_module._PdfPage(
            20,
            (opening_label, opening_title),
            has_images=False,
            image_area_ratios=(),
            has_table=False,
            image_orientation_mismatch=False,
        ),
        pdf_conversion_module._PdfPage(
            21,
            (exercise_label, exercise_title),
            has_images=False,
            image_area_ratios=(),
            has_table=False,
            image_orientation_mismatch=False,
        ),
        pdf_conversion_module._PdfPage(
            22,
            (running_label,),
            has_images=False,
            image_area_ratios=(),
            has_table=False,
            image_orientation_mismatch=False,
        ),
        pdf_conversion_module._PdfPage(
            23,
            (later_running_label,),
            has_images=False,
            image_area_ratios=(),
            has_table=False,
            image_orientation_mismatch=False,
        ),
    ]
    repeated = pdf_conversion_module._repeated_margin_lines(
        [line for page in pages for line in page.lines],
        page_count=700,
    )
    monkeypatch.setattr(
        pdf_conversion_module,
        "_should_replace_with_ocr",
        lambda page, _markdown: page.number == 20,
    )

    markdown, _issues = pdf_conversion_module._render_document(
        pages,
        body_size=12,
        heading_sizes={22: 1},
        repeated_margins=repeated,
        referenced_pages=set(),
        ocr_pages={20: "CHAPTER 14\n\n# EXALTATION LORDS"},
        ocr_failed_pages=set(),
    )

    assert markdown.count("## CHAPTER 14") == 1
    assert "\nCHAPTER 14\n" not in markdown


def test_repeated_margin_lines_confirm_a_confusable_folio_from_neighboring_pages() -> None:
    def folio(page_number: int, text: str) -> _PdfLine:
        return replace(
            _margin_line(page_number, text, top=100),
            x0=90,
            x1=110,
            font_size=7,
        )

    lines = [
        folio(210, "178"),
        folio(211, "179"),
        folio(212, "i8o"),
        folio(213, "181"),
    ]

    repeated = pdf_conversion_module._repeated_margin_lines(lines, page_count=4)

    assert pdf_conversion_module._margin_key("i8o") in repeated


def test_repeated_margin_lines_do_not_guess_an_unsupported_margin_code() -> None:
    line = replace(
        _margin_line(212, "i8o", top=100),
        x0=90,
        x1=110,
        font_size=7,
    )

    assert pdf_conversion_module._repeated_margin_lines([line], page_count=1) == set()


def test_omits_an_isolated_confusable_folio_only_with_strict_margin_geometry() -> None:
    folio = replace(
        _margin_line(212, "i8o", top=100),
        x0=90,
        x1=110,
        font_size=7,
    )
    centered = replace(folio, x0=290, x1=310)
    distant_code = replace(folio, text="O1")

    assert pdf_conversion_module._omit_margin_line(folio, set(), None, 9)
    assert not pdf_conversion_module._omit_margin_line(centered, set(), None, 9)
    assert not pdf_conversion_module._omit_margin_line(distant_code, set(), None, 9)


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


def test_preserves_a_full_page_table_image_beside_its_ocr_text(tmp_path: Path) -> None:
    source = tmp_path / "scanned-form.pdf"
    _write_image_pdf(source)
    ocr_table = (
        "| Date | Income source | Amount | Expense category | Spending |\n"
        "| --- | --- | --- | --- | --- |\n"
        "|  |  |  |  |  |\n"
        "| Total income |  |  | Total expenses |  |"
    )

    converted = convert_pdf_document(
        source,
        include_images=True,
        load_ocr_checkpoint=lambda _page: ocr_table,
    )

    assert len(converted.resources) == 1
    assert ocr_table in converted.markdown
    assert "__parsezen_resources__/pdf/page-0001-image-01.jpg" in converted.markdown


def test_does_not_duplicate_a_reliable_reflow_table_as_a_full_page_image() -> None:
    page = SimpleNamespace(
        width=600,
        height=800,
        bbox=(0, 0, 600, 800),
        images=[{"x0": 0, "x1": 600, "top": 0, "bottom": 800}],
        curves=[],
    )
    table = pdf_conversion_module._PdfTable(
        (90, 180, 510, 420),
        (("Label", "Value"), ("Condition", "Result")),
        pdf_conversion_module._TableRendering.HTML,
    )
    model = pdf_conversion_module._PdfPage(
        number=1,
        lines=(),
        has_images=True,
        image_area_ratios=(1.0,),
        has_table=True,
        image_orientation_mismatch=False,
        tables=(table,),
    )
    ocr_table = "| Label | Value |\n| --- | --- |\n| Condition | Result |"

    boxes = pdf_conversion_module._exportable_image_boxes(page, model, ocr_table)

    assert boxes == ()


def test_omits_a_full_page_contents_scan_when_ocr_recovers_its_entries(tmp_path: Path) -> None:
    source = tmp_path / "scanned-contents.pdf"
    _write_image_pdf(source)
    ocr_toc = (
        "| Section | Page |\n"
        "| --- | --- |\n"
        "| Introduction | 9 |\n"
        "| First chapter | 16 |\n"
        "| Second chapter | 28 |\n"
        "| Third chapter | 37 |"
    )

    converted = convert_pdf_document(
        source,
        include_images=True,
        load_ocr_checkpoint=lambda _page: ocr_toc,
    )

    assert converted.resources == ()
    assert ocr_toc in converted.markdown


def test_omits_a_nearly_blank_full_page_background(tmp_path: Path) -> None:
    source = tmp_path / "blank-background.pdf"
    _write_image_pdf(source, native_text="Front matter")

    converted = convert_pdf_document(
        source,
        include_images=True,
        load_ocr_checkpoint=lambda _page: "",
    )

    assert converted.resources == ()
    assert "Front matter" in converted.markdown


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


def test_does_not_promote_a_long_body_sized_small_caps_sentence_to_heading() -> None:
    sentence = pdf_conversion_module._PdfLine(
        page_number=1,
        page_width=600,
        page_height=800,
        text="IT IS AN HONOR FOR ME TO WELCOME AND INTRODUCE THE PUBLICATION",
        chars=(),
        x0=50,
        x1=550,
        top=100,
        bottom=112,
        font_size=10,
        bold=True,
        links=(),
        soft_hyphen_end=False,
        hard_hyphen_end=False,
        rotated=False,
    )
    concise_heading = replace(sentence, text="ANCIENT ASTROLOGY")
    oversized_title = replace(sentence, font_size=14)

    assert (
        pdf_conversion_module._heading_level(
            sentence,
            body_size=10,
            heading_sizes={},
            gap_before=12,
            toc_page=False,
        )
        is None
    )
    assert (
        pdf_conversion_module._heading_level(
            concise_heading,
            body_size=10,
            heading_sizes={},
            gap_before=12,
            toc_page=False,
        )
        == 2
    )
    assert (
        pdf_conversion_module._heading_level(
            oversized_title,
            body_size=10,
            heading_sizes={14: 1},
            gap_before=12,
            toc_page=False,
        )
        == 1
    )


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

    multiline = (("Name", "Notes"), ("A", "First item.\nSecond item."))
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


def test_table_renderer_flags_sparse_continuation_rows_for_visual_review() -> None:
    fragmented = (
        ("A", "B", "C", "D"),
        ("record one", "value", "value", "value"),
        ("continuation", "value", "value", "value"),
        ("continued", "", "value", ""),
        ("record two", "value", "value", "value"),
        ("continued", "", "value", ""),
    )

    assert (
        pdf_conversion_module._table_rendering(fragmented, 4)
        is pdf_conversion_module._TableRendering.STRUCTURED_TEXT
    )


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


def test_ocr_failure_preserves_cached_pages_and_reports_only_pending_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = pdf_conversion_module._PdfOcrPlan(
        page_numbers={1, 2},
        force_full_page_numbers=set(),
        required_page_numbers=set(),
    )
    monkeypatch.setattr(
        pdf_conversion_module,
        "convert_pdf_pages_with_ocr",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConversionError("OCR unavailable")),
    )

    pages, failed, required_failed = pdf_conversion_module._run_planned_ocr(
        Path("book.pdf"),
        plan,
        None,
        None,
        None,
        lambda page: "cached page" if page == 1 else None,
        None,
    )

    assert pages == {1: "cached page"}
    assert failed == {2}
    assert required_failed == set()


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


def test_keeps_legitimate_k_in_uppercase_english_words() -> None:
    assert pdf_conversion_module._normalize_text("MAKE A PROMISE, TAKE ACTION") == (
        "MAKE A PROMISE, TAKE ACTION"
    )


def test_keeps_a_suspicious_numeric_glyph_until_ocr_can_arbitrate_it() -> None:
    assert pdf_conversion_module._normalize_text("1$. TRIPLICITY RULERSHIPS") == (
        "1$. TRIPLICITY RULERSHIPS"
    )


def test_ordinary_english_ordinals_do_not_trigger_visual_ocr() -> None:
    assert not pdf_conversion_module._is_mixed_visual_glyph("1st")
    assert not pdf_conversion_module._is_mixed_visual_glyph("22nd")
    assert not pdf_conversion_module._is_mixed_visual_glyph("3rd")
    assert not pdf_conversion_module._is_mixed_visual_glyph("14th")
    assert pdf_conversion_module._is_mixed_visual_glyph("6S")


def test_legitimate_currency_amounts_do_not_trigger_local_ocr() -> None:
    page = pdf_conversion_module._PdfPage(
        1,
        (
            _pdf_model_line(
                1,
                "A $10 million prize, $1,000,000 goal, $50k/year salary, and $$$ rewards.",
            ),
        ),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    assert not pdf_conversion_module._has_suspicious_glyph_encoding(page)
    assert pdf_conversion_module._pages_requiring_ocr([page]) == set()


def test_single_replacement_glyph_triggers_local_ocr_and_review_warning() -> None:
    line = _pdf_model_line(1, "Antiochus Thesaurus·.")
    page = pdf_conversion_module._PdfPage(
        1,
        (line,),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    assert pdf_conversion_module._has_suspicious_glyph_encoding(page)
    assert pdf_conversion_module._pages_requiring_ocr([page]) == {1}
    warning = pdf_conversion_module._page_conversion_warning(page, [line], False, "")
    assert warning is not None
    assert "glifo ilegible" in warning


@pytest.mark.parametrize(
    "text",
    (
        "Try to look for the beginnings of 3D shapes.",
        "Evidence: https://imgur.com/a/4l74MHe",
        "Water is H2O.",
    ),
)
def test_ordinary_alphanumeric_terms_do_not_trigger_numeric_ocr(text: str) -> None:
    page = pdf_conversion_module._PdfPage(
        1,
        (_pdf_model_line(1, text),),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    assert not pdf_conversion_module._has_suspicious_numeric_glyph_encoding(page)
    assert pdf_conversion_module._pages_requiring_ocr([page]) == set()


def test_number_shaped_header_glyphs_trigger_numeric_ocr() -> None:
    page = pdf_conversion_module._PdfPage(
        1,
        (_pdf_model_line(1, "I8O"),),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    assert pdf_conversion_module._has_suspicious_numeric_glyph_encoding(page)
    assert pdf_conversion_module._pages_requiring_ocr([page]) == {1}


@pytest.mark.parametrize("native", ("IO63", "Broken quote ”$"))
def test_self_suspicious_visual_lines_receive_fallback_priority(native: str) -> None:
    line = _pdf_model_line(1, native)
    page = pdf_conversion_module._PdfPage(
        1,
        (line,),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    disagreements = pdf_conversion_module._visual_text_disagreements([page], {1: native})

    assert len(disagreements) == 1
    assert disagreements[0].priority >= 126


@pytest.mark.parametrize("include_page_number", [False, True])
def test_blank_or_page_number_only_vector_page_does_not_trigger_optional_ocr(
    include_page_number: bool,
) -> None:
    lines = (
        (
            pdf_conversion_module._PdfLine(
                page_number=1,
                page_width=600,
                page_height=800,
                text="16",
                chars=(),
                x0=290,
                x1=310,
                top=760,
                bottom=775,
                font_size=10,
                bold=False,
                links=(),
                soft_hyphen_end=False,
                hard_hyphen_end=False,
                rotated=False,
            ),
        )
        if include_page_number
        else ()
    )
    page = pdf_conversion_module._PdfPage(
        1,
        lines,
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    plan = pdf_conversion_module._build_ocr_plan([page], force_ocr=False)

    assert plan.page_numbers == set()
    assert plan.force_full_page_numbers == set()
    assert plan.required_page_numbers == set()


def test_reconciles_only_uniquely_confirmed_toc_numeric_glyphs() -> None:
    lines = [
        _pdf_model_line(1, "1$. TRIPLICITY RULERSHIPS 199"),
        _pdf_model_line(1, "SUMMARY AND SOURCE LEGENDS 5^5"),
        _pdf_model_line(1, "PRICE 1$"),
        _pdf_model_line(1, "6S. THE FIFTH HOUSE 697"),
    ]
    ocr = (
        "15. TRIPLICITY RULERSHIPS 199\n"
        "SUMMARY AND SOURCE LEGENDS 525\n"
        "PRICE 19\n"
        "68. THE FIFTH HOUSE 697"
    )

    reconciled = pdf_conversion_module._reconcile_suspicious_toc_numbers(lines, ocr)

    assert [line.text for line in reconciled] == [
        "15. TRIPLICITY RULERSHIPS 199",
        "SUMMARY AND SOURCE LEGENDS 525",
        # Both 15 and 19 appear in OCR, so this token remains explicitly uncertain.
        "PRICE 1$",
        "68. THE FIFTH HOUSE 697",
    ]


def test_reconciles_detached_toc_folio_from_unique_native_corroboration() -> None:
    lines = [
        replace(_pdf_model_line(1, "522"), x0=370, top=170),
        replace(_pdf_model_line(1, "54. SUMMARY AND SOURCE READINGS"), top=195),
        replace(_pdf_model_line(1, "5^5"), x0=370, top=195),
        replace(_pdf_model_line(1, "Main Points of the Aspect Doctrine"), top=220),
        replace(_pdf_model_line(1, "525"), x0=370, top=220),
    ]

    reconciled = pdf_conversion_module._reconcile_suspicious_toc_numbers(
        lines,
        "Exercise 38 515\nSummary 525\nFinal synthesis 535",
    )

    assert [line.text for line in reconciled] == [
        "522",
        "54. SUMMARY AND SOURCE READINGS",
        "525",
        "Main Points of the Aspect Doctrine",
        "525",
    ]


def test_reconciles_alpha_shaped_detached_toc_folio_from_ocr_and_sequence() -> None:
    lines = [
        replace(_pdf_model_line(1, "625"), x0=370, top=170),
        replace(_pdf_model_line(1, "Joys of the Houses"), top=195),
        replace(_pdf_model_line(1, "62S"), x0=370, top=195),
        replace(_pdf_model_line(1, "Derived Houses"), top=220),
        replace(_pdf_model_line(1, "635"), x0=370, top=220),
    ]

    reconciled = pdf_conversion_module._reconcile_suspicious_toc_numbers(
        lines,
        "The Houses and Chronological Ages 625\nJoys of the Houses 628\nDerived Houses 635",
    )

    assert [line.text for line in reconciled] == [
        "625",
        "Joys of the Houses",
        "628",
        "Derived Houses",
        "635",
    ]


def test_reconciles_broken_ligature_folios_from_neighbouring_toc_entries() -> None:
    lines = [
        _pdf_model_line(1, "Exercise 49 1030"),
        _pdf_model_line(1, "90. THE ULTIMATE RULERS OF THE CHART IO35"),
        _pdf_model_line(1, "91. THE PREDOMINATOR IO39"),
        _pdf_model_line(1, "Procedure 1040"),
        _pdf_model_line(1, "The Horimea 1135"),
        _pdf_model_line(1, "97. LENGTH OF LIFE H37"),
        _pdf_model_line(1, "The Predominator as Releaser 1137"),
        _pdf_model_line(1, "98. SOURCE READINGS 1147"),
        _pdf_model_line(1, "Primary Source Readings n47"),
    ]

    reconciled = pdf_conversion_module._reconcile_suspicious_toc_numbers(lines, None)

    assert [line.text for line in reconciled] == [
        "Exercise 49 1030",
        "90. THE ULTIMATE RULERS OF THE CHART 1035",
        "91. THE PREDOMINATOR 1039",
        "Procedure 1040",
        "The Horimea 1135",
        "97. LENGTH OF LIFE 1137",
        "The Predominator as Releaser 1137",
        "98. SOURCE READINGS 1147",
        "Primary Source Readings 1147",
    ]


def test_reconciles_broken_toc_folios_when_the_page_also_contains_a_table() -> None:
    page = pdf_conversion_module._PdfPage(
        number=8,
        lines=(
            _pdf_model_line(8, "Prelude 1029"),
            _pdf_model_line(8, "Exercise 49 1030"),
            _pdf_model_line(8, "90. THE ULTIMATE RULERS OF THE CHART IO35"),
            _pdf_model_line(8, "91. THE PREDOMINATOR IO39"),
            _pdf_model_line(8, "Procedure 1040"),
            _pdf_model_line(8, "Summary 1043"),
        ),
        has_images=False,
        image_area_ratios=(),
        has_table=True,
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

    assert "1035" in markdown
    assert "1039" in markdown
    assert "IO35" not in markdown
    assert "IO39" not in markdown


def test_reasserts_repaired_toc_folios_after_visual_spacing_reconstruction(
    monkeypatch,
) -> None:
    page = pdf_conversion_module._PdfPage(
        number=8,
        lines=(
            _pdf_model_line(8, "Prelude 1029"),
            _pdf_model_line(8, "Exercise 49 1030"),
            _pdf_model_line(8, "90. THE ULTIMATE RULERS OF THE CHART IO35"),
            _pdf_model_line(8, "91. THE PREDOMINATOR IO39"),
            _pdf_model_line(8, "Procedure 1040"),
            _pdf_model_line(8, "Summary 1043"),
        ),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )
    original_display = pdf_conversion_module._display_heading_text

    def reconstruct_from_original_glyphs(line):
        reconstructed = original_display(line)
        return reconstructed.replace("1035", "IO35").replace("1039", "IO39")

    monkeypatch.setattr(
        pdf_conversion_module,
        "_display_heading_text",
        reconstruct_from_original_glyphs,
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

    assert "1035" in markdown
    assert "1039" in markdown
    assert "IO35" not in markdown
    assert "IO39" not in markdown


def test_reconciles_spaced_numeric_year_only_with_unique_local_ocr_evidence() -> None:
    lines = [
        _pdf_model_line(1, "RUBEDO"),
        _pdf_model_line(1, "i o i i"),
    ]

    reconciled = pdf_conversion_module._reconcile_spaced_numeric_year(
        lines,
        "RUBEDO\n2022",
    )
    ambiguous = pdf_conversion_module._reconcile_spaced_numeric_year(
        lines,
        "First published in 2019\nReissued in 2022",
    )
    copyright_page = pdf_conversion_module._PdfPage(
        2,
        (_pdf_model_line(2, "First published in 2022 by Rubedo Press"),),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )
    publication_years = pdf_conversion_module._publication_year_evidence(
        [copyright_page],
        {},
    )
    reconciled_from_front_matter = pdf_conversion_module._reconcile_spaced_numeric_year(
        lines,
        "RUBEDO",
        publication_years,
    )

    assert [line.text for line in reconciled] == ["RUBEDO", "2022"]
    assert ambiguous == lines
    assert publication_years == {"2022"}
    assert [line.text for line in reconciled_from_front_matter] == ["RUBEDO", "2022"]


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


def test_ocr_winner_restores_spacing_only_from_an_exact_native_line() -> None:
    native = _pdf_model_line(
        1,
        "RULERSHIPS through which a planet may derive power. When a planet arrives.",
    )
    page = pdf_conversion_module._PdfPage(
        1,
        (native,),
        has_images=True,
        image_area_ratios=(1.0,),
        has_table=False,
        image_orientation_mismatch=False,
    )

    repaired = pdf_conversion_module._repair_ocr_spacing_from_native(
        page,
        "Earlier. RULERSHIPS th ro u g h which a planet may derive power. "
        "W hen a planet arrives. Later.",
    )
    different = pdf_conversion_module._repair_ocr_spacing_from_native(
        page,
        "RULERSHIPS th r o u g h which a planet may derive powers. W h e n a planet arrives.",
    )

    assert repaired == f"Earlier. {native.text} Later."
    assert different.endswith("derive powers. W h e n a planet arrives.")


def test_ocr_repairs_repeated_degree_zeros_only_with_a_confirmed_peer() -> None:
    broken = (
        "The values are 190 *Aries*, 30 *Taurus*, 190 *Libra*, 150 *Cancer*, "
        "28° *Capricorn*, 270 *Pisces*, and 150 *Virgo*."
    )

    assert pdf_conversion_module._repair_ocr_degree_marker_consensus(broken) == (
        "The values are 19° *Aries*, 3° *Taurus*, 19° *Libra*, 15° *Cancer*, "
        "28° *Capricorn*, 27° *Pisces*, and 15° *Virgo*."
    )
    assert (
        pdf_conversion_module._repair_ocr_degree_marker_consensus(
            "The shipment includes 100 Boxes, 150 Cases, and 200 Packages."
        )
        == "The shipment includes 100 Boxes, 150 Cases, and 200 Packages."
    )


def test_ocr_repairs_repeated_degree_zeros_across_markdown_table_cells() -> None:
    broken = (
        "| Body | Degree | Sign |\n"
        "| --- | ---: | --- |\n"
        "| Sun | 190 | Aries |\n"
        "| Moon | 30 | Taurus |\n"
        "| Mars | 28° | Capricorn |\n"
        "| Venus | 270 | Pisces |"
    )

    assert pdf_conversion_module._repair_ocr_degree_marker_consensus(broken) == (
        "| Body | Degree | Sign |\n"
        "| --- | ---: | --- |\n"
        "| Sun | 19° | Aries |\n"
        "| Moon | 3° | Taurus |\n"
        "| Mars | 28° | Capricorn |\n"
        "| Venus | 27° | Pisces |"
    )


def test_native_degree_zero_repair_requires_superscript_geometry() -> None:
    def character(
        text: str,
        *,
        size: float = 9.0,
        bottom: float = 10.0,
    ) -> pdf_conversion_module._PdfCharacter:
        return pdf_conversion_module._PdfCharacter(
            text=text,
            x0=0.0,
            x1=1.0,
            top=0.0,
            bottom=bottom,
            size=size,
            upright=True,
        )

    def line(text: str, *, superscript_zeroes: bool) -> _PdfLine:
        characters = tuple(
            character(
                value,
                size=6.0 if value == "0" and superscript_zeroes else 9.0,
                bottom=6.5 if value == "0" and superscript_zeroes else 10.0,
            )
            for value in text
            if not value.isspace()
        )
        return replace(
            _pdf_model_line(1, text),
            chars=characters,
            top=0.0,
            bottom=10.0,
            font_size=9.0,
        )

    broken = line("Sun 190 Aries and Moon 30 Taurus", superscript_zeroes=True)
    ordinary = line("Shipment 190 Boxes and 30 Cases", superscript_zeroes=False)
    pages = [
        pdf_conversion_module._PdfPage(
            1,
            (broken, ordinary),
            has_images=False,
            image_area_ratios=(),
            has_table=False,
            image_orientation_mismatch=False,
        )
    ]

    repaired, accepted = pdf_conversion_module._repair_native_degree_markers(pages)

    assert accepted == 2
    assert repaired[0].lines[0].text == "Sun 19° Aries and Moon 3° Taurus"
    assert repaired[0].lines[1].text == ordinary.text


def test_native_invalid_zodiac_degree_repairs_only_the_superscript_marker() -> None:
    def line(text: str) -> _PdfLine:
        characters = tuple(
            pdf_conversion_module._PdfCharacter(
                text=value,
                x0=float(index),
                x1=float(index + 1),
                top=0.0,
                bottom=6.5 if value == "0" else 10.0,
                size=6.0 if value == "0" else 9.0,
                upright=True,
            )
            for index, value in enumerate(text)
            if not value.isspace()
        )
        return replace(
            _pdf_model_line(1, text),
            chars=characters,
            top=0.0,
            bottom=10.0,
            font_size=9.0,
        )

    broken = line("Sun 750 Virgo")
    ordinary = line("The arc is 750 Degrees")
    page = pdf_conversion_module._PdfPage(
        1,
        (broken, ordinary),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    repaired, accepted = pdf_conversion_module._repair_native_degree_markers([page])

    assert accepted == 1
    assert repaired[0].lines[0].text == "Sun 75° Virgo"
    assert repaired[0].lines[1].text == ordinary.text


def test_native_degree_geometry_does_not_require_a_following_proper_name() -> None:
    characters = tuple(
        pdf_conversion_module._PdfCharacter(
            text=value,
            x0=float(index),
            x1=float(index + 1),
            top=0.0,
            bottom=6.5 if value == "0" else 10.0,
            size=6.0 if value == "0" else 9.0,
            upright=True,
        )
        for index, value in enumerate("15° behind".replace("°", "0"))
        if not value.isspace()
    )
    line = replace(
        _pdf_model_line(1, "The phase extends from 150 behind the Sun."),
        chars=characters,
        top=0.0,
        bottom=10.0,
        font_size=9.0,
    )
    page = pdf_conversion_module._PdfPage(
        1,
        (line,),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    repaired, accepted = pdf_conversion_module._repair_native_degree_markers([page])

    assert accepted == 1
    assert repaired[0].lines[0].text == "The phase extends from 15° behind the Sun."


def test_invalid_within_sign_degree_is_flagged_without_guessing_its_value() -> None:
    suspicious = _pdf_model_line(1, "Sun at 75° Virgo")
    ordinary = _pdf_model_line(1, "The arc spans 75° above the horizon")
    page = pdf_conversion_module._PdfPage(
        1,
        (suspicious, ordinary),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    assert pdf_conversion_module._has_suspicious_numeric_glyph_encoding(page)
    assert pdf_conversion_module._line_has_suspicious_numeric_glyph(suspicious)
    assert not pdf_conversion_module._line_has_suspicious_numeric_glyph(ordinary)
    assert "glifo numérico ambiguo" in pdf_conversion_module._page_conversion_warning(
        page,
        [suspicious, ordinary],
        False,
        None,
    )


def test_unconfirmed_degree_ocr_remains_evidence_without_adding_structure() -> None:
    page = pdf_conversion_module._PdfPage(
        1,
        (
            _pdf_model_line(
                1,
                "The native paragraph remains useful while the diagram says 75° Virgo.",
            ),
        ),
        has_images=True,
        image_area_ratios=(0.4,),
        has_table=False,
        image_orientation_mismatch=False,
    )
    ocr_pages = {1: "FIGURE 38\n\n- 158 Virgo\n- 258 Libra\n- unrelated OCR-only additions"}

    assert (
        pdf_conversion_module._ocr_pages_safe_for_structural_rendering(
            [page],
            ocr_pages,
        )
        == {}
    )

    clean_page = replace(
        page,
        lines=(_pdf_model_line(1, "The native paragraph remains useful."),),
    )
    assert (
        pdf_conversion_module._ocr_pages_safe_for_structural_rendering(
            [clean_page],
            ocr_pages,
        )
        == ocr_pages
    )


def test_overlapping_native_symbol_repair_requires_matching_geometry() -> None:
    def symbol_line(*, overlap: bool) -> _PdfLine:
        characters = tuple(
            pdf_conversion_module._PdfCharacter(
                text=value,
                x0=x0,
                x1=x1,
                top=0.0,
                bottom=9.0,
                size=9.0,
                upright=True,
            )
            for value, x0, x1 in (
                ("A", 0.0, 5.0),
                ("(", 6.0, 8.0),
                ("%", 7.5 if overlap else 8.5, 12.0),
                ("B", 13.0, 18.0),
            )
        )
        return replace(_pdf_model_line(1, "A (% B"), chars=characters)

    repaired, accepted = pdf_conversion_module._repair_overlapping_native_symbols(
        [
            pdf_conversion_module._PdfPage(
                1,
                (symbol_line(overlap=True), symbol_line(overlap=False)),
                has_images=False,
                image_area_ratios=(),
                has_table=False,
                image_orientation_mismatch=False,
            )
        ]
    )

    assert accepted == 1
    assert repaired[0].lines[0].text == "A & B"
    assert pdf_conversion_module._display_heading_text(repaired[0].lines[0]) == "A & B"
    assert repaired[0].lines[1].text == "A (% B"


def test_overlapping_native_ampersand_triplet_requires_matching_geometry() -> None:
    def symbol_line(*, overlap: bool) -> _PdfLine:
        middle_x0 = 7.5 if overlap else 8.5
        right_x0 = 11.5 if overlap else 13.5
        characters = tuple(
            pdf_conversion_module._PdfCharacter(
                text=value,
                x0=x0,
                x1=x1,
                top=0.0,
                bottom=9.0,
                size=9.0,
                upright=True,
            )
            for value, x0, x1 in (
                ("A", 0.0, 5.0),
                ("(", 6.0, 8.0),
                ("3", middle_x0, 12.0),
                ("[", right_x0, 14.0),
                ("B", 15.0, 20.0),
            )
        )
        return replace(_pdf_model_line(1, "A (3[ B"), chars=characters)

    repaired, accepted = pdf_conversion_module._repair_overlapping_native_symbols(
        [
            pdf_conversion_module._PdfPage(
                1,
                (symbol_line(overlap=True), symbol_line(overlap=False)),
                has_images=False,
                image_area_ratios=(),
                has_table=False,
                image_orientation_mismatch=False,
            )
        ]
    )

    assert accepted == 1
    assert repaired[0].lines[0].text == "A & B"
    assert pdf_conversion_module._display_heading_text(repaired[0].lines[0]) == "A & B"
    assert repaired[0].lines[1].text == "A (3[ B"


def test_reliable_native_table_is_not_replaced_by_an_ocr_table() -> None:
    table = pdf_conversion_module._PdfTable(
        (50.0, 100.0, 550.0, 300.0),
        (("Name", "Value"), ("Alpha", "First\nSecond")),
        pdf_conversion_module._TableRendering.HTML,
    )
    page = pdf_conversion_module._PdfPage(
        1,
        (_pdf_model_line(1, "Readable native introduction and table."),),
        has_images=False,
        image_area_ratios=(),
        has_table=True,
        image_orientation_mismatch=False,
        tables=(table,),
    )
    ocr = "| Name | Value |\n| --- | --- |\n| Alpha | First Second |"

    assert not pdf_conversion_module._should_replace_with_ocr(page, ocr)


def test_uppercase_sentence_leadin_is_not_promoted_to_a_heading() -> None:
    leadin = replace(
        _pdf_model_line(1, "THIS OPENING CONTINUES AS ONE SENTENCE", centered=True),
        top=100.0,
        bottom=109.0,
        font_size=9.0,
    )
    continuation = replace(
        _pdf_model_line(1, "through the rest of the paragraph."),
        top=112.0,
        bottom=121.0,
        font_size=9.0,
    )
    separated = replace(continuation, top=140.0, bottom=149.0)

    assert pdf_conversion_module._uppercase_leadin_continues(
        leadin,
        continuation,
        9.0,
    )
    assert not pdf_conversion_module._uppercase_leadin_continues(
        leadin,
        separated,
        9.0,
    )


def test_repeated_native_lines_repair_only_a_spacing_variant_confirmed_twice() -> None:
    clean = "Visibility/Beams/Chariot"
    pages = [
        pdf_conversion_module._PdfPage(
            page_number,
            (_pdf_model_line(page_number, clean),),
            has_images=False,
            image_area_ratios=(),
            has_table=False,
            image_orientation_mismatch=False,
        )
        for page_number in range(1, 4)
    ]
    broken = "Vis ib il it y/Be am s/Ch ar io t"
    pages.append(
        pdf_conversion_module._PdfPage(
            4,
            (_pdf_model_line(4, broken),),
            has_images=False,
            image_area_ratios=(),
            has_table=False,
            image_orientation_mismatch=False,
        )
    )

    repaired, changes = pdf_conversion_module._repair_repeated_native_spacing(pages)

    assert changes == 1
    assert repaired[-1].lines[0].text == clean


def test_repeated_native_spacing_abstains_without_three_pages_of_evidence() -> None:
    clean = _pdf_model_line(1, "Visibility/Beams/Chariot")
    broken = _pdf_model_line(
        2,
        "Vis ib il it y/Be am s/Ch ar io t",
    )
    pages = [
        pdf_conversion_module._PdfPage(
            page_number,
            (line,),
            has_images=False,
            image_area_ratios=(),
            has_table=False,
            image_orientation_mismatch=False,
        )
        for page_number, line in enumerate((clean, broken), start=1)
    ]

    repaired, changes = pdf_conversion_module._repair_repeated_native_spacing(pages)

    assert changes == 0
    assert repaired == pages


def test_literal_asterisk_legend_is_not_rendered_as_a_markdown_list() -> None:
    line = _pdf_model_line(1, "* = PLANET IS ITS OWN LORD", bold=True)
    page = pdf_conversion_module._PdfPage(
        1,
        (line,),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    markdown, issues = pdf_conversion_module._render_document(
        [page],
        body_size=12,
        heading_sizes={},
        repeated_margins=set(),
        referenced_pages=set(),
        ocr_pages={},
        ocr_failed_pages=set(),
    )

    assert r"\* = PLANET IS ITS OWN LORD" in markdown
    assert issues == ()


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


def test_orders_the_column_that_continues_a_full_width_line_before_a_figure_caption() -> None:
    def line(
        text: str,
        x0: float,
        x1: float,
        top: float,
        *,
        chars: tuple[object, ...] = (),
    ) -> object:
        return pdf_conversion_module._PdfLine(
            page_number=1,
            page_width=400,
            page_height=600,
            text=text,
            chars=chars,
            x0=x0,
            x1=x1,
            top=top,
            bottom=top + 9,
            font_size=9,
            bold=text.startswith("FIGURE"),
            links=(),
            soft_hyphen_end=text.endswith("Can"),
            hard_hyphen_end=False,
            rotated=False,
        )

    merged_chars = (
        pdf_conversion_module._PdfCharacter("cer), placed in Taurus", 54, 194, 526, 535, 9, True),
        pdf_conversion_module._PdfCharacter("twelfth house continues", 207, 348, 526, 535, 9, True),
    )
    source = [
        line("it contributes a share", 56, 346, 299),
        line("FIGURE IO9. TWELFTH-HOUSE LORD", 54, 176, 495),
        line("IN THE TENTH", 54, 102, 504),
        line("The Moon is the lord (Can", 54, 196, 519),
        line("the favorable tenth house.", 54, 132, 539),
        line("of its own good fortune", 207, 348, 311),
        line("the house it rules", 207, 347, 323),
        line("house topics prosper", 207, 346, 335),
        line("the example continues", 207, 347, 347),
        line(
            "cer), placed in Taurus twelfth house continues",
            54,
            348,
            526,
            chars=merged_chars,
        ),
        line("is in the tenth house", 207, 348, 539),
    ]

    split = pdf_conversion_module._split_lines_at_established_gutters(source)
    ordered = pdf_conversion_module._reading_order_lines(split)
    texts = [item.text for item in ordered]

    assert "cer), placed in Taurus" in texts
    assert "twelfth house continues" in texts
    assert texts.index("of its own good fortune") < texts.index("FIGURE IO9. TWELFTH-HOUSE LORD")


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


def test_table_cells_join_visual_line_wraps_but_preserve_explicit_items() -> None:
    assert (
        pdf_conversion_module._render_table_cell(
            "It has the face of\nIsis. It wears a winged queen's\ncrown."
        )
        == "It has the face of Isis. It wears a winged queen's crown."
    )
    assert (
        pdf_conversion_module._render_table_cell("First instruction.\nSecond instruction.")
        == "First instruction.\nSecond instruction."
    )
    assert pdf_conversion_module._render_table_cell("Name\n1. First item") == "Name\n1. First item"


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


def test_recovers_an_open_two_column_table_with_multiline_cells(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "open-two-column-table.pdf"
    _write_open_raster_table_pdf(source, layout="labels")
    monkeypatch.setattr(
        pdf_conversion_module,
        "convert_pdf_pages_with_ocr",
        lambda *_args, **_kwargs: {},
    )

    page = pdf_conversion_module._extract_pages(source, None)[0]
    reports: list[PdfQualityReport] = []
    converted = convert_pdf_document(
        source,
        include_images=True,
        on_quality_report=reports.append,
    )

    assert len(page.tables) == 1
    assert page.tables[0].inferred_from_raster
    assert page.tables[0].rows == (
        ("Authors", "Significations"),
        ("HERMES\nEgypt E", "Life and livelihood\ncontinues below"),
        ("THRASYLLUS\nAlexandria", "Fortune and death"),
        ("VALENS\nAntioch", "Benefits and lawsuits"),
    )
    assert (
        pdf_conversion_module._deserialize_page_checkpoint(
            pdf_conversion_module._serialize_page_checkpoint(page),
            1,
        )
        == page
    )
    assert len(converted.resources) == 1
    assert converted.resources[0].page_number == 1
    assert len(reports) == 1
    assert len(reports[0].issues) == 1
    assert "tabla escaneada" in reports[0].issues[0].message


def test_recovers_a_sparse_open_matrix_from_repeated_header_gutters(tmp_path: Path) -> None:
    source = tmp_path / "open-sparse-matrix.pdf"
    _write_open_raster_table_pdf(source, layout="matrix")

    page = pdf_conversion_module._extract_pages(source, None)[0]

    assert len(page.tables) == 1
    assert page.tables[0].inferred_from_raster
    assert page.tables[0].rows == (
        ("KEY\nKIND", "SUN\nFIRE", "MOON\nWATR", "MARS\nAIR"),
        ("C1", "-", "-", "Hit"),
        ("C2", "-", "-", "Hit"),
        ("C3", "-", "-", "Hit"),
        ("C4", "-", "-", "Hit"),
    )


def test_restores_sparse_matrix_dashes_visible_only_in_the_scan(tmp_path: Path) -> None:
    source = tmp_path / "open-sparse-matrix-visual-placeholders.pdf"
    _write_open_raster_table_pdf(source, layout="matrix_visual_placeholders")

    page = pdf_conversion_module._extract_pages(source, None)[0]

    assert len(page.tables) == 1
    assert page.tables[0].rows == (
        ("KEY\nKIND", "SUN\nFIRE", "MOON\nWATR", "MARS\nAIR"),
        ("C1", "—", "—", "Hit"),
        ("C2", "—", "—", "Hit"),
        ("C3", "—", "—", "Hit"),
        ("C4", "—", "—", "Hit"),
    )


def test_repairs_a_degree_glyph_only_from_repeated_matrix_header_evidence() -> None:
    rows = (
        ("KEY", "SUN\n5° FIRE", "MOON\n9°WATR", "MARS\n240 AIR"),
        ("C1", "—", "—", "Hit"),
    )

    assert pdf_conversion_module._repair_consensus_degree_markers(rows) == (
        ("KEY", "SUN\n5° FIRE", "MOON\n9° WATR", "MARS\n24° AIR"),
        ("C1", "—", "—", "Hit"),
    )
    assert pdf_conversion_module._repair_consensus_degree_markers(
        (("KEY", "Value 240 AIR", "Other", "Third"), ("C1", "1", "2", "3"))
    ) == (("KEY", "Value 240 AIR", "Other", "Third"), ("C1", "1", "2", "3"))


def test_open_rules_do_not_turn_two_column_prose_into_a_table(tmp_path: Path) -> None:
    source = tmp_path / "open-two-column-prose.pdf"
    _write_open_raster_table_pdf(source, layout="prose")

    page = pdf_conversion_module._extract_pages(source, None)[0]

    assert page.tables == ()
    assert not page.has_table


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


def test_pairs_four_digit_folios_in_a_long_book_toc() -> None:
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
        line("List of Figures", 50, 180, 50),
        line("First figure", 55, 220, 100),
        line("Second figure", 55, 230, 120),
        line("Third figure", 55, 220, 140),
        line("1023", 335, 350, 100),
        line("1125", 335, 350, 120),
        line("1155", 335, 350, 140),
    ]

    normalized = pdf_conversion_module._normalize_toc_entry_rows(source_order)

    assert [item.text for item in normalized] == [
        "List of Figures",
        "First figure 1023",
        "Second figure 1125",
        "Third figure 1155",
    ]
    assert pdf_conversion_module._split_toc_entry_text("Final figure 1155") == (
        "Final figure",
        "1155",
    )


def test_restores_only_toc_word_boundaries_confirmed_by_ocr() -> None:
    lines = [
        _pdf_model_line(1, "92. Names and Topics ofthe Twelve Houses 600"),
        _pdf_model_line(1, "121. Example Chart One: Horoscope ofJacqueline Onassis 1154"),
        _pdf_model_line(1, "Native wording stays authoritative 1200"),
    ]
    ocr = (
        "| 92. Names and Topics of thc Twelve Houses | 600 |\n"
        "| 121. Example Chart Onc: Horoscope of Jacqueline Onassis | 1154 |\n"
        "| Different OCR wording must not replace native text | 1200 |"
    )

    reconciled = pdf_conversion_module._reconcile_toc_spacing_from_ocr(lines, ocr)

    assert [line.text for line in reconciled] == [
        "92. Names and Topics of the Twelve Houses 600",
        "121. Example Chart One: Horoscope of Jacqueline Onassis 1154",
        "Native wording stays authoritative 1200",
    ]


def test_restores_all_caps_toc_word_boundaries_from_glyph_geometry() -> None:
    def toc_line(raw_text: str, visual_parts: tuple[str, ...], top: float) -> _PdfLine:
        characters = []
        x = 72.0
        for part in visual_parts:
            for character in part:
                characters.append(
                    pdf_conversion_module._PdfCharacter(
                        character,
                        x,
                        x + 6,
                        top,
                        top + 12,
                        12,
                        True,
                    )
                )
                x += 6
            x += 6
        return replace(
            _pdf_model_line(1, raw_text),
            chars=tuple(characters),
            x1=x,
            top=top,
            bottom=top + 12,
        )

    lines = (
        toc_line("CONTENIDO", ("CONTENIDO",), 80),
        toc_line("I-ENERGÍAFUNDAMENTAL 10", ("I", "-", "ENERGÍA", "FUNDAMENTAL", "10"), 110),
        toc_line("II-NUTRICIÓNEFICAZ 20", ("II", "-", "NUTRICIÓN", "EFICAZ", "20"), 130),
        toc_line("III-MOVIMIENTOEFICIENTE 30", ("III", "-", "MOVIMIENTO", "EFICIENTE", "30"), 150),
        toc_line("IV-DESCANSOPROFUNDO 40", ("IV", "-", "DESCANSO", "PROFUNDO", "40"), 170),
    )
    page = pdf_conversion_module._PdfPage(
        number=1,
        lines=lines,
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

    assert "II - NUTRICIÓN EFICAZ" in markdown
    assert "III - MOVIMIENTO EFICIENTE" in markdown
    assert "NUTRICIÓNEFICAZ" not in markdown


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


def test_normalizes_only_footnotes_backed_by_small_bottom_definitions() -> None:
    body = replace(
        _pdf_model_line(1, "A sourced statement.6 Another value.7"),
        top=180,
        bottom=192,
        font_size=12,
    )
    definition = replace(
        _pdf_model_line(1, "6 Paulus, Introduction 3."),
        top=700,
        bottom=708,
        font_size=8,
    )

    normalized = pdf_conversion_module._normalize_page_footnotes(
        [body, definition],
        body_size=12,
    )

    assert [line.text for line in normalized] == [
        "A sourced statement.⁶ Another value.7",
        "6. Paulus, Introduction 3.",
    ]


def test_does_not_guess_footnotes_without_matching_bottom_geometry() -> None:
    body = _pdf_model_line(1, "A version.6")
    ordinary_line = replace(
        _pdf_model_line(1, "6 Ordinary numbered prose"),
        top=700,
        bottom=712,
        font_size=12,
    )

    normalized = pdf_conversion_module._normalize_page_footnotes(
        [body, ordinary_line],
        body_size=12,
    )

    assert normalized == [body, ordinary_line]


def test_renders_toc_entries_as_a_reflowable_aligned_table() -> None:
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
    assert '<table class="document-toc">' in markdown
    assert '<td class="toc-label toc-level-0">Opening</td><td class="toc-folio">9</td>' in markdown
    assert '<td class="toc-folio">17</td>' in markdown
    assert '<td class="toc-folio">31</td>' in markdown


def test_renders_dotted_toc_entries_as_reflowable_aligned_rows() -> None:
    def line(
        text: str,
        top: float,
        *,
        size: float = 10,
        links: tuple[pdf_conversion_module._PdfLink, ...] = (),
    ) -> pdf_conversion_module._PdfLine:
        return replace(
            _pdf_model_line(1, text),
            x0=70,
            x1=520,
            top=top,
            bottom=top + size,
            font_size=size,
            links=links,
        )

    target = pdf_conversion_module._PdfLink("#page-9", 70, 520, 100, 110)
    page = pdf_conversion_module._PdfPage(
        number=1,
        lines=(
            line("Índice", 50, size=18),
            line("Introducción................................9", 100, links=(target,)),
            line("Primer capítulo..........................1 2", 120),
            line("Segundo capítulo..........................31", 140),
            line("Apéndice..................................iv", 160),
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

    assert "## Índice" in markdown
    assert '<a href="#page-9">Introducción</a>' in markdown
    assert '<td class="toc-folio">9</td>' in markdown
    assert '<td class="toc-label toc-level-0">Primer capítulo</td>' in markdown
    assert '<td class="toc-folio">12</td>' in markdown
    assert '<td class="toc-label toc-level-0">Segundo capítulo</td>' in markdown
    assert '<td class="toc-folio">31</td>' in markdown
    assert '<td class="toc-label toc-level-0">Apéndice</td>' in markdown
    assert '<td class="toc-folio">iv</td>' in markdown
    assert "..." not in markdown


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


def test_generated_toc_table_keeps_an_adjacent_unpaginated_part_as_a_root_row() -> None:
    blocks = [
        pdf_conversion_module._MarkdownBlock(
            "heading",
            "PART II: PROFILES",
            6,
        ),
        pdf_conversion_module._MarkdownBlock(
            "toc-entry",
            "Profile I",
            6,
            toc_folio="53",
            toc_level=1,
        ),
        pdf_conversion_module._MarkdownBlock(
            "toc-entry",
            "Profile II",
            6,
            toc_folio="59",
            toc_level=1,
        ),
    ]

    markdown = pdf_conversion_module._blocks_to_markdown(blocks)

    assert '<td class="toc-label toc-level-0">PART II: PROFILES</td>' in markdown
    assert '<td class="toc-folio"></td>' in markdown
    assert '<td class="toc-label toc-level-1">Profile I</td>' in markdown


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


def test_recovers_a_two_curve_vector_plot_as_an_image_box() -> None:
    page = SimpleNamespace(
        width=536,
        height=697,
        bbox=(0, 0, 536, 697),
        images=[],
        curves=[
            {"x0": 169, "x1": 388, "top": 222, "bottom": 224},
            {"x0": 174, "x1": 384, "top": 150, "bottom": 289},
        ],
    )
    y_axis_label = replace(
        _pdf_model_line(1, "-1.0"),
        x0=150,
        x1=166,
        top=275,
        bottom=283,
    )
    model = pdf_conversion_module._PdfPage(
        number=1,
        lines=(y_axis_label,),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    boxes = pdf_conversion_module._exportable_image_boxes(page, model, None)

    assert len(boxes) == 1
    x0, top, x1, bottom = boxes[0]
    assert x0 <= 150
    assert top <= 150
    assert x1 >= 388
    assert bottom >= 289


def test_two_thin_vector_rules_do_not_become_an_image() -> None:
    page = SimpleNamespace(
        width=600,
        height=800,
        bbox=(0, 0, 600, 800),
        images=[],
        curves=[
            {"x0": 100, "x1": 500, "top": 100, "bottom": 101},
            {"x0": 100, "x1": 500, "top": 120, "bottom": 121},
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

    assert pdf_conversion_module._exportable_image_boxes(page, model, None) == ()


@pytest.mark.parametrize(("letters", "expected"), [(150, 1), (400, 0)])
def test_preserves_only_sparse_hybrid_full_page_illustrations(
    letters: int,
    expected: int,
) -> None:
    page = SimpleNamespace(
        width=600,
        height=800,
        bbox=(0, 0, 600, 800),
        images=[{"x0": 0, "x1": 600, "top": 0, "bottom": 800}],
        curves=[],
    )
    model = pdf_conversion_module._PdfPage(
        number=1,
        lines=(_pdf_model_line(1, "A" * letters),),
        has_images=True,
        image_area_ratios=(1.0,),
        has_table=False,
        image_orientation_mismatch=False,
    )

    boxes = pdf_conversion_module._exportable_image_boxes(page, model, None)

    assert len(boxes) == expected


def test_preserves_a_terminal_full_page_visual_beside_dense_ocr() -> None:
    page = SimpleNamespace(
        width=600,
        height=800,
        bbox=(0, 0, 600, 800),
        images=[{"x0": 0, "x1": 600, "top": 0, "bottom": 800}],
        curves=[],
    )
    model = pdf_conversion_module._PdfPage(
        number=2,
        lines=(),
        has_images=True,
        image_area_ratios=(1.0,),
        has_table=False,
        image_orientation_mismatch=False,
    )
    dense_ocr = "Recognized back cover copy. " * 30

    assert pdf_conversion_module._exportable_image_boxes(page, model, dense_ocr) == ()
    assert pdf_conversion_module._exportable_image_boxes(
        page,
        model,
        dense_ocr,
        preserve_full_page_visual=True,
    ) == ((0.0, 0.0, 600.0, 800.0),)


@pytest.mark.parametrize(
    ("caption", "bold"),
    (
        ("FIGURE 62.", False),
        ("FIGURES 10 Q 11 SECT REJOICING BY HEMISPHERE", True),
        ("F i G U R E 6 8.", False),
    ),
)
def test_preserves_a_dense_full_page_scan_with_a_numbered_figure_caption(
    caption: str,
    bold: bool,
) -> None:
    page = SimpleNamespace(
        width=600,
        height=800,
        bbox=(0, 0, 600, 800),
        images=[{"x0": 0, "x1": 600, "top": 0, "bottom": 800}],
        curves=[],
    )
    model = pdf_conversion_module._PdfPage(
        number=1,
        lines=(
            _pdf_model_line(1, "A" * 400),
            _pdf_model_line(1, caption, bold=bold),
        ),
        has_images=True,
        image_area_ratios=(1.0,),
        has_table=False,
        image_orientation_mismatch=False,
    )

    boxes = pdf_conversion_module._exportable_image_boxes(page, model, None)

    assert boxes == ((0.0, 0.0, 600.0, 800.0),)


def test_native_numbered_figure_outweighs_a_false_positive_ocr_contents_label() -> None:
    page = SimpleNamespace(
        width=600,
        height=800,
        bbox=(0, 0, 600, 800),
        images=[{"x0": 0, "x1": 600, "top": 0, "bottom": 800}],
        curves=[],
    )
    model = pdf_conversion_module._PdfPage(
        number=1,
        lines=(
            _pdf_model_line(1, "A" * 400),
            _pdf_model_line(1, "FIGURE 38. MORNING AND EVENING PLANETS"),
        ),
        has_images=True,
        image_area_ratios=(1.0,),
        has_table=False,
        image_orientation_mismatch=False,
    )
    misleading_ocr = "CONTENTS\nChapter One 1\nChapter Two 2\nChapter Three 3\nChapter Four 4"

    assert pdf_conversion_module._is_toc_markdown(misleading_ocr)
    assert pdf_conversion_module._exportable_image_boxes(
        page,
        model,
        misleading_ocr,
    ) == ((0.0, 0.0, 600.0, 800.0),)


def test_does_not_treat_a_prose_figure_reference_as_a_figure_caption() -> None:
    page = SimpleNamespace(
        width=600,
        height=800,
        bbox=(0, 0, 600, 800),
        images=[{"x0": 0, "x1": 600, "top": 0, "bottom": 800}],
        curves=[],
    )
    model = pdf_conversion_module._PdfPage(
        number=1,
        lines=(
            _pdf_model_line(1, "A" * 400),
            _pdf_model_line(1, "Figure 62 shows the relevant relationship."),
        ),
        has_images=True,
        image_area_ratios=(1.0,),
        has_table=False,
        image_orientation_mismatch=False,
    )

    boxes = pdf_conversion_module._exportable_image_boxes(page, model, None)

    assert boxes == ()


def test_does_not_preserve_a_full_page_list_of_figures_as_a_figure() -> None:
    page = SimpleNamespace(
        width=600,
        height=800,
        bbox=(0, 0, 600, 800),
        images=[{"x0": 0, "x1": 600, "top": 0, "bottom": 800}],
        curves=[],
    )
    model = pdf_conversion_module._PdfPage(
        number=1,
        lines=tuple(
            _pdf_model_line(1, f"FIGURE {number}. Entry {number * 10}") for number in range(1, 5)
        ),
        has_images=True,
        image_area_ratios=(1.0,),
        has_table=False,
        image_orientation_mismatch=False,
    )

    boxes = pdf_conversion_module._exportable_image_boxes(page, model, None)

    assert boxes == ()


def test_reports_a_numbered_figure_when_its_scan_cannot_be_exported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "numbered-figure-scan.pdf"
    _write_image_pdf(source)
    model = pdf_conversion_module._PdfPage(
        number=1,
        lines=(
            _pdf_model_line(1, "A" * 400),
            _pdf_model_line(1, "FIGURES 10 & 11."),
        ),
        has_images=True,
        image_area_ratios=(1.0,),
        has_table=False,
        image_orientation_mismatch=False,
    )

    def fail_render(*_args: object, **_kwargs: object) -> bytes:
        raise OSError("synthetic render failure")

    monkeypatch.setattr(pdf_conversion_module, "_render_pdf_image", fail_render)

    resources, omitted, failed_pages = pdf_conversion_module._extract_embedded_images(
        source,
        [model],
        {},
        None,
        None,
        None,
    )
    _markdown, issues = pdf_conversion_module._render_document(
        [model],
        body_size=12,
        heading_sizes={},
        repeated_margins=set(),
        referenced_pages=set(),
        ocr_pages={},
        ocr_failed_pages=set(),
        required_figure_failure_pages=failed_pages,
    )

    assert resources == {}
    assert omitted == 1
    assert failed_pages == {1}
    assert len(issues) == 1
    assert "figura numerada" in issues[0].message


def test_fragmented_graphic_labels_keep_the_complete_page_image() -> None:
    page = SimpleNamespace(
        width=600,
        height=800,
        bbox=(0, 0, 600, 800),
        images=[
            {"x0": 0, "x1": 600, "top": 0, "bottom": 800},
            {"x0": 100, "x1": 500, "top": 100, "bottom": 770},
        ],
        curves=[],
    )
    lines = tuple(
        replace(_pdf_model_line(1, f"A{index}"), top=80 + index * 20, bottom=92 + index * 20)
        for index in range(10)
    )
    model = pdf_conversion_module._PdfPage(
        number=1,
        lines=lines,
        has_images=True,
        image_area_ratios=(1.0, 0.56),
        has_table=False,
        image_orientation_mismatch=False,
    )
    ocr = "\n".join(f"A{index}" for index in range(10))

    boxes = pdf_conversion_module._exportable_image_boxes(page, model, ocr)

    assert boxes == ((0.0, 0.0, 600.0, 800.0),)


def test_dense_graphic_label_mosaic_stays_visual_even_with_many_ocr_letters() -> None:
    page = pdf_conversion_module._PdfPage(
        number=52,
        lines=(),
        has_images=True,
        image_area_ratios=(1.0,),
        has_table=False,
        image_orientation_mismatch=False,
    )
    ocr = "\n".join(f"HOODIES {index:02d}" for index in range(18))

    assert pdf_conversion_module._fragmented_graphic_text_should_stay_in_image(page, ocr)


def test_dense_prose_scan_is_not_mistaken_for_a_graphic_label_mosaic() -> None:
    page = pdf_conversion_module._PdfPage(
        number=20,
        lines=(),
        has_images=True,
        image_area_ratios=(1.0,),
        has_table=False,
        image_orientation_mismatch=False,
    )
    ocr = "\n".join(
        f"This is a complete prose sentence number {index} with ordinary reading flow."
        for index in range(18)
    )

    assert not pdf_conversion_module._fragmented_graphic_text_should_stay_in_image(page, ocr)


def test_rotated_visual_with_removed_vertical_stack_does_not_add_unverified_ocr() -> None:
    page = pdf_conversion_module._PdfPage(
        number=5,
        lines=(),
        has_images=True,
        image_area_ratios=(0.4,),
        has_table=False,
        image_orientation_mismatch=True,
    )
    resource = pdf_conversion_module.PdfEmbeddedResource(
        pdf_conversion_module.PurePosixPath("pdf/page-0005-image-01.jpg"),
        b"rotated check",
        "image/jpeg",
        5,
        (100, 200, 500, 700),
    )

    assert not pdf_conversion_module._should_include_ocr_additions(
        page,
        skipped_vertical=True,
        resources=(resource,),
    )
    assert pdf_conversion_module._should_include_ocr_additions(
        page,
        skipped_vertical=False,
        resources=(resource,),
    )


def test_fragmented_graphic_labels_are_not_reflowed_beside_the_page_image() -> None:
    lines = tuple(
        replace(_pdf_model_line(1, f"A{index}"), top=80 + index * 20, bottom=92 + index * 20)
        for index in range(10)
    )
    page = pdf_conversion_module._PdfPage(
        number=1,
        lines=lines,
        has_images=True,
        image_area_ratios=(1.0,),
        has_table=False,
        image_orientation_mismatch=False,
    )
    resource = pdf_conversion_module.PdfEmbeddedResource(
        pdf_conversion_module.PurePosixPath("pdf/page-0001-image-01.jpg"),
        b"image",
        "image/jpeg",
        1,
    )
    ocr = "\n".join(f"A{index}" for index in range(10))

    markdown, _issues = pdf_conversion_module._render_document(
        [page],
        body_size=12,
        heading_sizes={},
        repeated_margins=set(),
        referenced_pages=set(),
        ocr_pages={1: ocr},
        ocr_failed_pages=set(),
        page_images={1: (resource,)},
    )

    assert "A0" not in markdown
    assert "A9" not in markdown
    assert "__parsezen_resources__/pdf/page-0001-image-01.jpg" in markdown


def test_sparse_raster_cover_uses_its_image_instead_of_unverified_ocr() -> None:
    page = pdf_conversion_module._PdfPage(
        number=1,
        lines=(),
        has_images=True,
        image_area_ratios=(0.99,),
        has_table=False,
        image_orientation_mismatch=False,
    )
    resource = pdf_conversion_module.PdfEmbeddedResource(
        pdf_conversion_module.PurePosixPath("pdf/page-0001-image-01.jpg"),
        b"cover",
        "image/jpeg",
        1,
        (0, 0, 600, 800),
        True,
    )

    markdown, _issues = pdf_conversion_module._render_document(
        [page],
        body_size=12,
        heading_sizes={},
        repeated_margins=set(),
        referenced_pages=set(),
        ocr_pages={1: "BROKEN COVER OCR"},
        ocr_failed_pages=set(),
        page_images={1: (resource,)},
    )

    assert "BROKEN COVER OCR" not in markdown
    assert "__parsezen_resources__/pdf/page-0001-image-01.jpg" in markdown


def test_unresolved_raster_table_uses_the_scan_instead_of_losing_a_diacritic() -> None:
    native_line = replace(
        _pdf_model_line(1, "Connection sunaphê with a malefic"),
        top=120,
        bottom=132,
    )
    table = pdf_conversion_module._PdfTable(
        bbox=(40, 80, 560, 720),
        rows=(("Topic",), ("Connection sunaphê with a malefic",)),
        rendering=pdf_conversion_module._TableRendering.MARKDOWN,
    )
    page = pdf_conversion_module._PdfPage(
        number=1,
        lines=(native_line,),
        has_images=True,
        image_area_ratios=(0.99,),
        has_table=True,
        image_orientation_mismatch=False,
        tables=(table,),
    )
    resource = pdf_conversion_module.PdfEmbeddedResource(
        pdf_conversion_module.PurePosixPath("pdf/page-0001-image-01.jpg"),
        b"table",
        "image/jpeg",
        1,
        table.bbox,
    )
    ocr = "| Topic |\n| --- |\n| Connection sunaphe with a malefic |"

    markdown, issues = pdf_conversion_module._render_document(
        [page],
        body_size=12,
        heading_sizes={},
        repeated_margins=set(),
        referenced_pages=set(),
        ocr_pages={1: ocr},
        ocr_failed_pages=set(),
        page_images={1: (resource,)},
    )

    assert "sunaph" not in markdown.casefold()
    assert "__parsezen_resources__/pdf/page-0001-image-01.jpg" in markdown
    assert any("grafía" in issue.message for issue in issues)


def test_unresolved_text_line_uses_its_visual_crop_and_keeps_reliable_prose() -> None:
    uncertain = replace(
        _pdf_model_line(1, "The sunaph� term remains uncertain."),
        x0=60,
        x1=400,
        top=120,
        bottom=132,
    )
    reliable = replace(
        _pdf_model_line(1, "Reliable prose remains reflowable."),
        x0=60,
        x1=400,
        top=160,
        bottom=172,
    )
    page = pdf_conversion_module._PdfPage(
        number=1,
        lines=(uncertain, reliable),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )
    bbox = pdf_conversion_module._visual_text_crop_bbox((0, 0, 600, 800), uncertain)
    resource = pdf_conversion_module.PdfEmbeddedResource(
        pdf_conversion_module.PurePosixPath("pdf/page-0001-image-01.jpg"),
        b"visual evidence",
        "image/jpeg",
        1,
        bbox,
        visual_text_authority=True,
    )

    markdown, issues = pdf_conversion_module._render_document(
        [page],
        body_size=12,
        heading_sizes={},
        repeated_margins=set(),
        referenced_pages=set(),
        ocr_pages={1: "The sunaphe term remains uncertain.\nReliable prose remains reflowable."},
        ocr_failed_pages=set(),
        page_images={1: (resource,)},
    )

    assert "sunaphê" not in markdown
    assert "sunaphe" not in markdown
    assert "Reliable prose remains reflowable." in markdown
    assert "__parsezen_resources__/pdf/page-0001-image-01.jpg" in markdown
    assert any("recortes visuales" in issue.message for issue in issues)


def test_native_diacritic_stays_reflowable_when_ocr_only_strips_it() -> None:
    native = replace(
        _pdf_model_line(1, "The sunaphê term remains reflowable."),
        x0=60,
        x1=400,
        top=120,
        bottom=132,
    )
    page = pdf_conversion_module._PdfPage(
        number=1,
        lines=(native,),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )
    ocr = "The sunaphe term remains reflowable."

    assert (
        pdf_conversion_module._unresolved_text_visual_boxes(
            page,
            ocr,
            (0, 0, 600, 800),
        )
        == ()
    )

    markdown, issues = pdf_conversion_module._render_document(
        [page],
        body_size=12,
        heading_sizes={},
        repeated_margins=set(),
        referenced_pages=set(),
        ocr_pages={1: ocr},
        ocr_failed_pages=set(),
        page_images={},
    )

    assert "sunaphê" in markdown
    assert "sunaphe" not in markdown
    assert not any("recortes visuales" in issue.message for issue in issues)


def test_ocr_additions_ignore_a_copy_that_only_drops_native_diacritics() -> None:
    page = pdf_conversion_module._PdfPage(
        number=1,
        lines=(
            _pdf_model_line(
                1,
                "La conversación continúa con precisión y mantiene la información.",
            ),
        ),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    additions = pdf_conversion_module._ocr_additions(
        page,
        "La conversacion continua con precision y mantiene la informacion.",
    )

    assert additions == ""


def test_visual_text_crop_does_not_capture_adjacent_lines() -> None:
    line = replace(
        _pdf_model_line(1, "One objectively damaged line."),
        x0=60,
        x1=400,
        top=120,
        bottom=132,
    )

    bbox = pdf_conversion_module._visual_text_crop_bbox((0, 0, 600, 800), line)

    assert bbox[1] >= 118
    assert bbox[3] <= 134


def test_many_unresolved_text_lines_preserve_the_complete_page_visual() -> None:
    lines = tuple(
        replace(
            _pdf_model_line(1, f"Concept {index} sunaph� remains uncertain."),
            x0=60,
            x1=420,
            top=80 + index * 30,
            bottom=92 + index * 30,
        )
        for index in range(9)
    )
    page = pdf_conversion_module._PdfPage(
        number=1,
        lines=lines,
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )
    ocr = "\n".join(f"Concept {index} sunaphe remains uncertain." for index in range(9))

    assert pdf_conversion_module._unresolved_text_visual_boxes(
        page,
        ocr,
        (0, 0, 600, 800),
    ) == ((0, 0, 600, 800),)


def test_generic_low_priority_ocr_difference_does_not_demote_prose_to_an_image() -> None:
    line = replace(
        _pdf_model_line(1, "A rare epikataphoray reading remains reflowable."),
        x0=60,
        x1=420,
        top=120,
        bottom=132,
    )
    page = pdf_conversion_module._PdfPage(
        number=1,
        lines=(line,),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )
    ocr = "A rare epikataphora reading remains reflowable."

    disagreements = pdf_conversion_module._visual_text_disagreements([page], {1: ocr})

    assert disagreements
    assert max(item.priority for item in disagreements) < 100
    assert (
        pdf_conversion_module._unresolved_text_visual_boxes(
            page,
            ocr,
            (0, 0, 600, 800),
        )
        == ()
    )


def test_unresolved_raster_table_accepts_plain_ocr_as_disagreement_evidence() -> None:
    native_line = replace(
        _pdf_model_line(1, "Connection sunaphê with a malefic"),
        top=120,
        bottom=132,
    )
    outside_prose = replace(
        _pdf_model_line(1, "Reliable prose outside the table."),
        top=740,
        bottom=752,
    )
    table = pdf_conversion_module._PdfTable(
        bbox=(40, 80, 560, 720),
        rows=(("Topic",), ("Connection sunaphê with a malefic",)),
        rendering=pdf_conversion_module._TableRendering.HTML,
    )
    page = pdf_conversion_module._PdfPage(
        number=1,
        lines=(native_line, outside_prose),
        has_images=True,
        image_area_ratios=(0.99,),
        has_table=True,
        image_orientation_mismatch=False,
        tables=(table,),
    )
    resource = pdf_conversion_module.PdfEmbeddedResource(
        pdf_conversion_module.PurePosixPath("pdf/page-0001-image-01.jpg"),
        b"table",
        "image/jpeg",
        1,
        table.bbox,
    )

    markdown, issues = pdf_conversion_module._render_document(
        [page],
        body_size=12,
        heading_sizes={},
        repeated_margins=set(),
        referenced_pages=set(),
        ocr_pages={1: "Topic\nConnection sunaphe with a malefic"},
        ocr_failed_pages=set(),
        page_images={1: (resource,)},
    )

    assert "sunaph" not in markdown.casefold()
    assert "Reliable prose outside the table." in markdown
    assert "__parsezen_resources__/pdf/page-0001-image-01.jpg" in markdown
    assert any("grafía" in issue.message for issue in issues)


def test_raster_table_does_not_use_an_alternate_ocr_occurrence_over_an_exact_one() -> None:
    native_line = replace(
        _pdf_model_line(1, "Connection sunaphê"),
        top=120,
        bottom=132,
    )
    table = pdf_conversion_module._PdfTable(
        bbox=(40, 80, 560, 720),
        rows=(("Connection sunaphê",),),
        rendering=pdf_conversion_module._TableRendering.HTML,
    )
    page = pdf_conversion_module._PdfPage(
        number=1,
        lines=(native_line,),
        has_images=True,
        image_area_ratios=(0.99,),
        has_table=True,
        image_orientation_mismatch=False,
        tables=(table,),
    )

    assert not pdf_conversion_module._unresolved_raster_table_text(
        page,
        "Connection sunaphê\nAlternative sunaphe",
    )


def test_raster_table_ignores_an_ocr_disagreement_outside_its_bbox() -> None:
    table_line = replace(
        _pdf_model_line(1, "Exact table value"),
        top=120,
        bottom=132,
    )
    outside_line = replace(
        _pdf_model_line(1, "Outside sunaphê note"),
        top=740,
        bottom=752,
    )
    table = pdf_conversion_module._PdfTable(
        bbox=(40, 80, 560, 720),
        rows=(("Exact table value",),),
        rendering=pdf_conversion_module._TableRendering.HTML,
    )
    page = pdf_conversion_module._PdfPage(
        number=1,
        lines=(table_line, outside_line),
        has_images=True,
        image_area_ratios=(0.99,),
        has_table=True,
        image_orientation_mismatch=False,
        tables=(table,),
    )

    assert not pdf_conversion_module._unresolved_raster_table_text(
        page,
        "Exact table value\nOutside sunaphe note",
    )


def test_raster_table_abstains_on_one_missing_letter_with_repeated_native_evidence() -> None:
    table = pdf_conversion_module._PdfTable(
        bbox=(40, 80, 560, 720),
        rows=(
            ("First epikataphoray value",),
            ("Second epikataphora value",),
        ),
        rendering=pdf_conversion_module._TableRendering.HTML,
    )
    page = pdf_conversion_module._PdfPage(
        number=1,
        lines=(),
        has_images=True,
        image_area_ratios=(0.99,),
        has_table=True,
        image_orientation_mismatch=False,
        tables=(table,),
    )

    assert pdf_conversion_module._unresolved_raster_table_text(
        page,
        "First epikataphora value\nSecond epikataphora value",
    )


def test_dense_raster_table_requires_a_visual_fallback() -> None:
    table = pdf_conversion_module._PdfTable(
        bbox=(40, 80, 560, 720),
        rows=tuple((str(index), "A", "B", "C") for index in range(20)),
        rendering=pdf_conversion_module._TableRendering.HTML,
    )
    page = pdf_conversion_module._PdfPage(
        number=1,
        lines=(),
        has_images=True,
        image_area_ratios=(0.99,),
        has_table=True,
        image_orientation_mismatch=False,
        tables=(table,),
    )

    assert pdf_conversion_module._dense_raster_table_requires_visual(page, table)


def test_complex_raster_table_keeps_outside_prose_and_uses_only_its_visual_crop() -> None:
    table_line = replace(
        _pdf_model_line(1, "Unreliable row association"),
        x0=60,
        x1=540,
        top=180,
        bottom=192,
    )
    prose = replace(
        _pdf_model_line(1, "Reliable prose after the table."),
        x0=60,
        x1=540,
        top=500,
        bottom=512,
    )
    table = pdf_conversion_module._PdfTable(
        bbox=(50, 150, 550, 450),
        rows=(
            ("A", "B", "C", "D"),
            ("value", "", "", "other"),
            ("", "continuation", "", ""),
            ("", "", "continuation", ""),
            ("last", "row", "is", "complete"),
        ),
        rendering=pdf_conversion_module._TableRendering.STRUCTURED_TEXT,
    )
    page = pdf_conversion_module._PdfPage(
        number=1,
        lines=(table_line, prose),
        has_images=True,
        image_area_ratios=(1.0,),
        has_table=True,
        image_orientation_mismatch=False,
        tables=(table,),
    )
    resource = pdf_conversion_module.PdfEmbeddedResource(
        pdf_conversion_module.PurePosixPath("pdf/page-0001-image-01.jpg"),
        b"table crop",
        "image/jpeg",
        1,
        table.bbox,
    )

    markdown, issues = pdf_conversion_module._render_document(
        [page],
        body_size=12,
        heading_sizes={},
        repeated_margins=set(),
        referenced_pages=set(),
        ocr_pages={},
        ocr_failed_pages=set(),
        page_images={1: (resource,)},
    )

    assert "Unreliable row association" not in markdown
    assert "Reliable prose after the table." in markdown
    assert "__parsezen_resources__/pdf/page-0001-image-01.jpg" in markdown
    assert any("asociación o grafía" in issue.message for issue in issues)


def test_final_sparse_raster_uses_the_preserved_back_cover_as_authority() -> None:
    page = pdf_conversion_module._PdfPage(
        number=704,
        lines=(),
        has_images=True,
        image_area_ratios=(1.0,),
        has_table=False,
        image_orientation_mismatch=False,
    )
    resource = pdf_conversion_module.PdfEmbeddedResource(
        pdf_conversion_module.PurePosixPath("pdf/page-0704-image-01.jpg"),
        b"back cover",
        "image/jpeg",
        704,
        (0, 0, 600, 800),
        True,
    )

    markdown, _issues = pdf_conversion_module._render_document(
        [page],
        body_size=12,
        heading_sizes={},
        repeated_margins=set(),
        referenced_pages=set(),
        ocr_pages={704: "A long but uncertain OCR rendering of the back cover."},
        ocr_failed_pages=set(),
        page_images={704: (resource,)},
    )

    assert "uncertain OCR" not in markdown
    assert "__parsezen_resources__/pdf/page-0704-image-01.jpg" in markdown


def test_places_a_discrete_image_between_the_heading_and_following_body() -> None:
    heading = replace(
        _pdf_model_line(1, "ARIES I: THE AXE"),
        x0=170,
        x1=430,
        top=50,
        bottom=66,
        font_size=16,
        bold=True,
    )
    body = replace(
        _pdf_model_line(1, "The body begins after the illustration."),
        x0=64,
        x1=400,
        top=300,
        bottom=312,
    )
    page = pdf_conversion_module._PdfPage(
        number=1,
        lines=(heading, body),
        has_images=True,
        image_area_ratios=(0.25,),
        has_table=False,
        image_orientation_mismatch=False,
    )
    resource = pdf_conversion_module.PdfEmbeddedResource(
        pdf_conversion_module.PurePosixPath("pdf/page-0001-image-01.jpg"),
        b"image",
        "image/jpeg",
        1,
        (125, 100, 350, 250),
    )

    markdown, _issues = pdf_conversion_module._render_document(
        [page],
        body_size=12,
        heading_sizes={16: 1},
        repeated_margins=set(),
        referenced_pages=set(),
        ocr_pages={},
        ocr_failed_pages=set(),
        page_images={1: (resource,)},
    )

    image_marker = "![](<__parsezen_resources__/pdf/page-0001-image-01.jpg>)"
    assert markdown.index("ARIES I: THE AXE") < markdown.index(image_marker)
    assert markdown.index(image_marker) < markdown.index("The body begins")


def test_places_a_discrete_image_immediately_before_its_confusable_figure_caption() -> None:
    resource = pdf_conversion_module.PdfEmbeddedResource(
        pdf_conversion_module.PurePosixPath("pdf/page-0338-image-01.jpg"),
        b"image",
        "image/jpeg",
        338,
        (58, 326, 199, 469),
    )
    right_column = replace(
        _pdf_model_line(338, "Following prose in the right column."),
        x0=207,
        x1=348,
        top=490,
        bottom=499,
    )
    caption = replace(
        _pdf_model_line(338, "FIGURE IO9. TWELFTH-HOUSE LORD"),
        x0=54,
        x1=176,
        top=495,
        bottom=501,
        font_size=7,
        bold=True,
    )

    insertions = pdf_conversion_module._page_image_insertions(
        (resource,),
        [right_column, caption],
    )

    assert insertions == [(1, resource)]


def test_short_cover_title_remains_reflowable_text() -> None:
    page = pdf_conversion_module._PdfPage(
        number=1,
        lines=(_pdf_model_line(1, "A SHORT TITLE"),),
        has_images=True,
        image_area_ratios=(1.0,),
        has_table=False,
        image_orientation_mismatch=False,
    )

    assert not pdf_conversion_module._fragmented_graphic_text_should_stay_in_image(
        page,
        "A SHORT TITLE\nBY AN AUTHOR",
    )


def test_dense_pdf_image_mosaic_is_preserved_as_one_composite_crop() -> None:
    images = [
        {"x0": x, "x1": x + 180, "top": y, "bottom": y + 130}
        for y in (100, 240, 380)
        for x in (80, 270)
    ]
    page = SimpleNamespace(
        width=600,
        height=800,
        bbox=(0, 0, 600, 800),
        images=images,
        curves=[],
    )
    model = pdf_conversion_module._PdfPage(
        number=1,
        lines=(),
        has_images=True,
        image_area_ratios=tuple(180 * 130 / (600 * 800) for _image in images),
        has_table=False,
        image_orientation_mismatch=False,
    )

    boxes = pdf_conversion_module._exportable_image_boxes(page, model, None)

    assert boxes == ((80.0, 100.0, 450.0, 510.0),)


def test_distant_pdf_images_remain_independent_crops() -> None:
    page = SimpleNamespace(
        width=600,
        height=800,
        bbox=(0, 0, 600, 800),
        images=[
            {"x0": 40, "x1": 160, "top": 40, "bottom": 160},
            {"x0": 440, "x1": 560, "top": 640, "bottom": 760},
        ],
        curves=[],
    )
    model = pdf_conversion_module._PdfPage(
        number=1,
        lines=(),
        has_images=True,
        image_area_ratios=(0.03, 0.03),
        has_table=False,
        image_orientation_mismatch=False,
    )

    boxes = pdf_conversion_module._exportable_image_boxes(page, model, None)

    assert boxes == (
        (40.0, 40.0, 160.0, 160.0),
        (440.0, 640.0, 560.0, 760.0),
    )


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


def test_renders_a_visually_confirmed_numbered_sequence_as_one_ordered_list() -> None:
    def line(text: str, top: float, *, x0: float = 90) -> pdf_conversion_module._PdfLine:
        return replace(
            _pdf_model_line(1, text),
            x0=x0,
            top=top,
            bottom=top + 9,
            font_size=9,
        )

    page = pdf_conversion_module._PdfPage(
        number=1,
        lines=(
            line("SUMMARY OF IMPORTANT POINTS", 90, x0=140),
            line("1. First point continues", 120),
            line("on an indented second line.", 132, x0=105),
            line("2. Second point continues", 150),
            line("on another indented line.", 162, x0=105),
            line("3. Third point is complete.", 180),
        ),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    markdown, _issues = pdf_conversion_module._render_document(
        [page],
        body_size=9,
        heading_sizes={},
        repeated_margins=set(),
        referenced_pages=set(),
        ocr_pages={},
        ocr_failed_pages=set(),
    )

    assert (
        "1. First point continues on an indented second line.\n"
        "2. Second point continues on another indented line.\n"
        "3. Third point is complete."
    ) in markdown


def test_repairs_a_split_numbered_label_only_with_native_page_evidence() -> None:
    def line(text: str, top: float, *, x0: float = 90) -> pdf_conversion_module._PdfLine:
        return replace(
            _pdf_model_line(1, text),
            x0=x0,
            top=top,
            bottom=top + 9,
            font_size=9,
        )

    page = pdf_conversion_module._PdfPage(
        number=1,
        lines=(
            line("VISIBILITY AT THE HORIZON", 90, x0=150),
            line("1. visibil it y: Record whether visibility is possible.", 120),
            line("2. char iot : Check whether the planet is in its chariot.", 144),
            line("3. hear t : Check whether it is in the heart of the Sun.", 168),
        ),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    markdown, _issues = pdf_conversion_module._render_document(
        [page],
        body_size=9,
        heading_sizes={},
        repeated_margins=set(),
        referenced_pages=set(),
        ocr_pages={},
        ocr_failed_pages=set(),
    )

    assert "1. visibility: Record whether visibility is possible." in markdown
    assert "2. chariot: Check whether the planet is in its chariot." in markdown
    assert "3. heart: Check whether it is in the heart of the Sun." in markdown


def test_keeps_one_emphasis_span_across_a_numbered_item_wrap() -> None:
    def line(
        text: str,
        top: float,
        *,
        x0: float = 90,
        soft_hyphen_end: bool = False,
    ) -> pdf_conversion_module._PdfLine:
        return replace(
            _pdf_model_line(1, text),
            x0=x0,
            top=top,
            bottom=top + 9,
            font_size=9,
            italic=True,
            soft_hyphen_end=soft_hyphen_end,
        )

    page = pdf_conversion_module._PdfPage(
        number=1,
        lines=(
            line("1. A Hellenis", 120, soft_hyphen_end=True),
            line("tic example continues.", 132, x0=105),
            line("2. The second item is complete.", 156),
            line("3. The third item is complete.", 180),
        ),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    markdown, _issues = pdf_conversion_module._render_document(
        [page],
        body_size=9,
        heading_sizes={},
        repeated_margins=set(),
        referenced_pages=set(),
        ocr_pages={},
        ocr_failed_pages=set(),
    )

    assert "1. *A Hellenistic example continues.*" in markdown
    assert "Hellenis**tic" not in markdown


@pytest.mark.parametrize(
    ("font_name", "marker"),
    (
        ("Book-Italic", "*"),
        ("Book-Bold", "**"),
        ("Book-BoldItalic", "***"),
    ),
)
def test_preserves_inline_source_emphasis_from_pdf_font_runs(
    font_name: str,
    marker: str,
) -> None:
    text = "These are the rulers of the nativity. They remain."
    emphasized = "rulers of the nativity"
    start = text.index(emphasized)
    end = start + len(emphasized)
    raw_characters: list[dict[str, object]] = []
    x = 50.0
    for index, character in enumerate(text):
        if character.isspace():
            x += 4
            continue
        raw_characters.append(
            {
                "text": character,
                "x0": x,
                "x1": x + 5,
                "top": 100,
                "bottom": 112,
                "size": 10,
                "upright": True,
                "fontname": font_name if start <= index < end else "Book-Regular",
            }
        )
        x += 5
    line = pdf_conversion_module._build_line(
        1,
        600,
        800,
        {
            "text": text,
            "chars": raw_characters,
            "x0": 50,
            "x1": x,
            "top": 100,
            "bottom": 112,
        },
        (),
    )

    assert line is not None
    assert pdf_conversion_module._apply_source_emphasis(line, line.text) == (
        f"These are the {marker}{emphasized}{marker}. They remain."
    )
    normalized_later = replace(line, text=f"2. {line.text}")
    assert (
        pdf_conversion_module._apply_source_emphasis(
            normalized_later,
            normalized_later.text,
        )
        == f"2. These are the {marker}{emphasized}{marker}. They remain."
    )


def test_pdf_checkpoint_round_trips_and_validates_inline_emphasis_spans() -> None:
    span = pdf_conversion_module._PdfEmphasisSpan("styled", False, True)
    line = replace(
        _pdf_model_line(1, "The styled phrase remains."),
        emphasis_spans=(span,),
    )
    page = pdf_conversion_module._PdfPage(
        number=1,
        lines=(line,),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )
    payload = pdf_conversion_module._serialize_page_checkpoint(page)

    assert pdf_conversion_module._deserialize_page_checkpoint(payload, 1) == page

    invalid = json.loads(payload)
    invalid["lines"][0]["emphasis_spans"] = [["", False, True]]
    assert pdf_conversion_module._deserialize_page_checkpoint(json.dumps(invalid), 1) is None


def test_does_not_expand_a_stale_inline_span_to_the_whole_line() -> None:
    line = replace(
        _pdf_model_line(1, "Normalized replacement text."),
        italic=True,
        emphasis_spans=(pdf_conversion_module._PdfEmphasisSpan("Previous wording", False, True),),
    )

    assert pdf_conversion_module._apply_source_emphasis(line, line.text) == line.text


def test_renders_only_the_source_emphasized_part_of_a_toc_label() -> None:
    line = replace(
        _pdf_model_line(1, "1. Ancient Egypt: The Body of Nut 11"),
        emphasis_spans=(pdf_conversion_module._PdfEmphasisSpan("The Body of Nut", False, True),),
    )
    block = pdf_conversion_module._MarkdownBlock(
        "toc-entry",
        "1. Ancient Egypt: The Body of Nut",
        1,
        source_line=line,
        toc_folio="11",
    )

    rendered = pdf_conversion_module._toc_entries_html([block])

    assert "1. Ancient Egypt: <em>The Body of Nut</em>" in rendered
    assert "<em>1. Ancient Egypt" not in rendered


def test_joins_an_inline_emphasis_span_across_a_hyphenated_line_wrap() -> None:
    previous = replace(
        _pdf_model_line(1, "The Master (Oikodes-"),
        hard_hyphen_end=True,
    )
    following = _pdf_model_line(1, "potes) remains.")

    assert (
        pdf_conversion_module._join_line_text(
            "The Master *(Oikodes-*",
            "*potes)* remains.",
            previous,
            following,
        )
        == "The Master *(Oikodespotes)* remains."
    )


def test_does_not_join_a_soft_hyphen_across_two_text_columns() -> None:
    previous = replace(
        _pdf_model_line(1, "The Moon is the lord (Can"),
        x0=54,
        x1=196,
        top=519,
        bottom=528,
        soft_hyphen_end=True,
    )
    other_column = replace(
        _pdf_model_line(1, "of its own good fortune"),
        x0=207,
        x1=348,
        top=311,
        bottom=320,
    )
    same_column = replace(
        _pdf_model_line(1, "cer), placed in Taurus"),
        x0=54,
        x1=195,
        top=530,
        bottom=539,
    )

    assert not pdf_conversion_module._should_join_lines(previous, other_column, 9, -217)
    assert pdf_conversion_module._should_join_lines(previous, same_column, 9, 2)


def test_leaves_distant_or_incomplete_numbered_paragraphs_outside_lists() -> None:
    lines = [
        replace(_pdf_model_line(1, "1. An isolated numbered paragraph."), top=100),
        replace(_pdf_model_line(1, "Ordinary prose separates the sections."), top=220),
        replace(_pdf_model_line(1, "2. A distant numbered paragraph."), top=340),
        replace(_pdf_model_line(1, "2024. A publication year is not an ordinal."), top=460),
    ]

    assert pdf_conversion_module._ordered_list_item_indexes(lines, 12) == set()


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


def test_invisible_internal_page_link_does_not_create_artificial_prose() -> None:
    target = "#page-20"
    line = replace(
        _pdf_model_line(4, "Index entry without a usable annotation label"),
        links=(pdf_conversion_module._PdfLink(target, 300, 340, 200, 214),),
    )
    page = pdf_conversion_module._PdfPage(
        number=4,
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
        referenced_pages={20},
        ocr_pages={},
        ocr_failed_pages=set(),
    )

    assert "Destinos conservados" not in markdown
    assert "Página 20" not in markdown
    assert "Destinos conservados" not in pdf_conversion_module._restore_page_links(
        "Recognized index entry.",
        page,
    )


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


def test_omits_a_roman_page_footer_even_on_a_toc_page() -> None:
    line = replace(
        _pdf_model_line(7, "vii"),
        x0=295,
        x1=315,
        top=760,
        bottom=774,
    )

    assert pdf_conversion_module._omit_margin_line(
        line,
        repeated_margins=set(),
        previous=None,
        body_size=12,
        toc_page=True,
    )


def test_strips_an_emphasized_ocr_footer_confirmed_by_the_native_margin() -> None:
    footer = replace(
        _pdf_model_line(9, "vii"),
        top=728,
        bottom=742,
    )
    page = pdf_conversion_module._PdfPage(
        number=9,
        lines=(footer,),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    markdown = pdf_conversion_module._strip_native_margin_numbers_from_ocr(
        page,
        "Visible index entry 873\n\n*vii*",
        12,
    )

    assert "Visible index entry 873" in markdown
    assert "vii" not in markdown


def test_strips_an_ocr_footer_embedded_in_an_otherwise_empty_table_row() -> None:
    footer = replace(
        _pdf_model_line(9, "vii"),
        top=728,
        bottom=742,
    )
    page = pdf_conversion_module._PdfPage(
        number=9,
        lines=(footer,),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    markdown = pdf_conversion_module._strip_native_margin_numbers_from_ocr(
        page,
        "| Visible index entry | 891 |\n| --- | --- |\n| *vii* | |",
        12,
    )

    assert "Visible index entry" in markdown
    assert "vii" not in markdown


def test_repairs_broken_native_toc_folios_inside_an_ocr_page_replacement() -> None:
    page = pdf_conversion_module._PdfPage(
        number=11,
        lines=(
            _pdf_model_line(11, "Exercise 49 1030"),
            _pdf_model_line(11, "90. THE ULTIMATE RULERS OF THE CHART IO35"),
            _pdf_model_line(11, "91. THE PREDOMINATOR IO39"),
            _pdf_model_line(11, "Procedure 1040"),
        ),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    repaired = pdf_conversion_module._repair_toc_ocr_numeric_glyphs(
        page,
        "Exercise 49 1030\n90. THE ULTIMATE RULERS IO35\n91. THE PREDOMINATOR IO39",
    )

    assert "1035" in repaired
    assert "1039" in repaired
    assert "IO35" not in repaired
    assert "IO39" not in repaired


def test_ocr_additions_ignore_a_long_ordered_copy_with_scattered_ocr_typos() -> None:
    sentence = (
        "This house system divides the celestial circle into equal sections and preserves "
        "every boundary for timing."
    )
    native = " ".join(sentence for _index in range(5))
    noisy_copy = native.replace("celestial", "celestlal", 1).replace(
        "boundary",
        "boundarv",
        1,
    )
    page = pdf_conversion_module._PdfPage(
        number=36,
        lines=(_pdf_model_line(36, native),),
        has_images=True,
        image_area_ratios=(1.0,),
        has_table=False,
        image_orientation_mismatch=False,
    )

    assert (
        len(
            pdf_conversion_module._comparison_tokens(noisy_copy)
            & pdf_conversion_module._comparison_tokens(native)
        )
        / len(pdf_conversion_module._comparison_tokens(noisy_copy))
        < 0.9
    )
    assert pdf_conversion_module._ocr_additions(page, noisy_copy) == ""


def test_ocr_additions_keep_a_substantially_new_prose_block() -> None:
    native = "The native layer contains the main paragraph and preserves its structure."
    addition = (
        "A newly visible footnote identifies a separate primary source, explains the disputed "
        "reading, and adds evidence that is absent from the selectable layer."
    )
    page = pdf_conversion_module._PdfPage(
        number=36,
        lines=(_pdf_model_line(36, native),),
        has_images=True,
        image_area_ratios=(1.0,),
        has_table=False,
        image_orientation_mismatch=False,
    )

    assert pdf_conversion_module._ocr_additions(page, addition) == addition


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


def test_renders_toc_entries_with_aligned_folios_and_source_emphasis() -> None:
    line = pdf_conversion_module._PdfLine(
        page_number=1,
        page_width=600,
        page_height=800,
        text="Chapter One 12",
        chars=(),
        x0=72,
        x1=520,
        top=100,
        bottom=114,
        font_size=12,
        bold=True,
        links=(),
        soft_hyphen_end=False,
        hard_hyphen_end=False,
        rotated=False,
        italic=True,
    )
    blocks = [
        pdf_conversion_module._MarkdownBlock(
            "toc-entry",
            "Chapter One",
            1,
            source_line=line,
            toc_folio="12",
            toc_level=1,
        )
    ]

    markdown = pdf_conversion_module._blocks_to_markdown(blocks)

    assert '<table class="document-toc">' in markdown
    assert (
        '<td class="toc-label toc-level-1"><strong><em>Chapter One</em></strong></td>' in markdown
    )
    assert '<td class="toc-folio">12</td>' in markdown
    assert "- Chapter One" not in markdown


def test_groups_toc_subtitles_into_one_continuous_table() -> None:
    lines: list[pdf_conversion_module._PdfLine] = []
    for index in range(4):
        top = 80 + index * 50
        lines.extend(
            (
                pdf_conversion_module._PdfLine(
                    page_number=1,
                    page_width=600,
                    page_height=800,
                    text=f"{index + 1}. MAIN ENTRY {100 + index}",
                    chars=(),
                    x0=60,
                    x1=520,
                    top=top,
                    bottom=top + 12,
                    font_size=10,
                    bold=True,
                    links=(),
                    soft_hyphen_end=False,
                    hard_hyphen_end=False,
                    rotated=False,
                ),
                pdf_conversion_module._PdfLine(
                    page_number=1,
                    page_width=600,
                    page_height=800,
                    text=f"Indented detail {('alpha', 'beta', 'gamma', 'delta')[index]}",
                    chars=(),
                    x0=82,
                    x1=420,
                    top=top + 15,
                    bottom=top + 27,
                    font_size=10,
                    bold=False,
                    links=(),
                    soft_hyphen_end=False,
                    hard_hyphen_end=False,
                    rotated=False,
                    italic=True,
                ),
            )
        )
    page = pdf_conversion_module._PdfPage(
        1,
        tuple(lines),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    markdown, _issues = pdf_conversion_module._render_document(
        [page],
        body_size=10,
        heading_sizes={},
        repeated_margins=set(),
        referenced_pages=set(),
        ocr_pages={},
        ocr_failed_pages=set(),
    )

    assert markdown.count('<table class="document-toc">') == 1
    assert markdown.count("<tr>") == 9
    assert '<td class="toc-label toc-level-1"><em>Indented detail alpha</em></td>' in markdown
    assert '<td class="toc-folio"></td>' in markdown


def test_visual_reading_accepts_only_the_disputed_glyph_span() -> None:
    accepted = pdf_conversion_module._validated_visual_reading(
        "The Horimiea 1135",
        "The Horim\ufffda 1135",
        "The Horimæa 1135",
    )
    rejected = pdf_conversion_module._validated_visual_reading(
        "The Horimiea 1135",
        "The Horim\ufffda 1135",
        "A different invention 1135",
    )

    assert accepted == "The Horimæa 1135"
    assert rejected is None


def test_visual_disagreement_aligns_a_detached_toc_folio() -> None:
    line = pdf_conversion_module._PdfLine(
        page_number=12,
        page_width=600,
        page_height=800,
        text="The Horimiea",
        chars=(),
        x0=72,
        x1=300,
        top=120,
        bottom=134,
        font_size=12,
        bold=False,
        links=(),
        soft_hyphen_end=False,
        hard_hyphen_end=False,
        rotated=False,
    )
    page = pdf_conversion_module._PdfPage(
        12,
        (line,),
        has_images=True,
        image_area_ratios=(1.0,),
        has_table=False,
        image_orientation_mismatch=False,
    )

    disagreements = pdf_conversion_module._visual_text_disagreements(
        [page],
        {12: "|     | The Horim\ufffda |   1135 |"},
    )

    assert len(disagreements) == 1
    assert disagreements[0].ocr_text == "The Horim\ufffda"


def test_visual_disagreement_prioritizes_a_rare_ligature() -> None:
    similarity = pdf_conversion_module.SequenceMatcher(
        None,
        "The Horimiea".casefold(),
        "The Horimæa".casefold(),
        autojunk=False,
    ).ratio()

    priority = pdf_conversion_module._visual_disagreement_priority(
        "The Horimiea",
        "The Horimæa",
        similarity,
    )

    assert priority is not None
    assert priority >= 115


def test_visual_disagreement_prioritizes_mixed_case_and_diacritic_tokens() -> None:
    mixed = pdf_conversion_module._visual_disagreement_priority(
        "waxing xMoon",
        "waxing Moon",
        0.95,
    )
    diacritic = pdf_conversion_module._visual_disagreement_priority(
        "Sunaphê",
        "Sunaphe",
        0.95,
    )
    accepted_third_reading = pdf_conversion_module._validated_visual_reading(
        "Sunaphê",
        "Sunaphe",
        "Sunaphē",
    )

    assert mixed is not None and mixed >= 120
    assert diacritic is not None and diacritic >= 108
    assert accepted_third_reading == "Sunaphē"


def test_visual_disagreement_prioritizes_an_alphanumeric_toc_number() -> None:
    similarity = pdf_conversion_module.SequenceMatcher(
        None,
        "6S. THE FIFTH HOUSE".casefold(),
        "68. THE FIFTH HOUSE".casefold(),
        autojunk=False,
    ).ratio()

    priority = pdf_conversion_module._visual_disagreement_priority(
        "6S. THE FIFTH HOUSE",
        "68. THE FIFTH HOUSE",
        similarity,
    )
    accepted = pdf_conversion_module._validated_visual_reading(
        "6S. THE FIFTH HOUSE",
        "68. THE FIFTH HOUSE",
        "68. THE FIFTH HOUSE",
    )

    assert priority is not None
    assert priority >= 112
    assert accepted == "68. THE FIFTH HOUSE"


def test_visual_arbiter_can_override_a_self_suspicious_mixed_glyph() -> None:
    priority = pdf_conversion_module._visual_disagreement_priority(
        "6S. THE FIFTH HOUSE",
        "6S. THE FIFTH HOUSE",
        1.0,
    )
    accepted_number = pdf_conversion_module._validated_visual_reading(
        "6S. THE FIFTH HOUSE",
        "6S. THE FIFTH HOUSE",
        "68. THE FIFTH HOUSE",
    )
    accepted_third_numeric_reading = pdf_conversion_module._validated_visual_reading(
        "6S. THE FIFTH HOUSE",
        "65. THE FIFTH HOUSE",
        "68. THE FIFTH HOUSE",
    )
    accepted_word = pdf_conversion_module._validated_visual_reading(
        "77ie Subterranean Place",
        "77ie Subterranean Place",
        "The Subterranean Place",
    )

    assert priority is not None
    assert priority >= 118
    assert accepted_number == "68. THE FIFTH HOUSE"
    assert accepted_third_numeric_reading == "68. THE FIFTH HOUSE"
    assert accepted_word == "The Subterranean Place"


def test_visual_arbiter_can_resolve_a_replacement_glyph_without_trusting_bad_ocr() -> None:
    priority = pdf_conversion_module._visual_disagreement_priority(
        "Primary Directions and the 90� Arc",
        "Primary Directions and the 90� Arc",
        1.0,
    )
    accepted = pdf_conversion_module._validated_visual_reading(
        "Primary Directions and the 90� Arc",
        "Primary Directions and the 90� Arc",
        "Primary Directions and the 90° Arc",
    )

    assert priority is not None
    assert priority >= 125
    assert accepted == "Primary Directions and the 90° Arc"


def test_visual_disagreements_include_a_self_suspicious_line_when_ocr_agrees() -> None:
    line = pdf_conversion_module._PdfLine(
        page_number=8,
        page_width=600,
        page_height=800,
        text="6S. THE FIFTH HOUSE",
        chars=(),
        x0=72,
        x1=300,
        top=120,
        bottom=134,
        font_size=12,
        bold=True,
        links=(
            pdf_conversion_module._PdfLink(
                target="#page-68",
                x0=72,
                x1=300,
                top=120,
                bottom=134,
            ),
        ),
        soft_hyphen_end=False,
        hard_hyphen_end=False,
        rotated=False,
    )
    page = pdf_conversion_module._PdfPage(
        8,
        (line,),
        has_images=True,
        image_area_ratios=(1.0,),
        has_table=False,
        image_orientation_mismatch=False,
    )

    disagreements = pdf_conversion_module._visual_text_disagreements(
        [page],
        {8: "6S. THE FIFTH HOUSE"},
    )

    assert len(disagreements) == 1
    assert disagreements[0].priority >= 118
    assert pdf_conversion_module._has_suspicious_numeric_glyph_encoding(page)


def test_visual_disagreement_keeps_a_number_from_an_ocr_table_cell() -> None:
    line = pdf_conversion_module._PdfLine(
        page_number=8,
        page_width=600,
        page_height=800,
        text="6S. THE FIFTH HOUSE",
        chars=(),
        x0=72,
        x1=300,
        top=120,
        bottom=134,
        font_size=12,
        bold=True,
        links=(),
        soft_hyphen_end=False,
        hard_hyphen_end=False,
        rotated=False,
    )
    page = pdf_conversion_module._PdfPage(
        8,
        (line,),
        has_images=True,
        image_area_ratios=(1.0,),
        has_table=False,
        image_orientation_mismatch=False,
    )

    disagreements = pdf_conversion_module._visual_text_disagreements(
        [page],
        {8: "| 68. | THE FIFTH HOUSE | 697 |"},
    )

    assert len(disagreements) == 1
    assert disagreements[0].ocr_text == "68. THE FIFTH HOUSE"


def test_visual_disagreement_can_reconcile_tokens_from_interleaved_columns() -> None:
    candidate = pdf_conversion_module._token_reconciled_visual_candidate(
        "A rare epikataphoray reading",
        (
            "unrelated left-column words epikataphora right-column words",
            "a repeated ordinary reading",
        ),
    )

    assert candidate == "A rare epikataphora reading"


def test_visual_replacement_updates_the_matching_structured_table_cell() -> None:
    line = pdf_conversion_module._PdfLine(
        page_number=8,
        page_width=600,
        page_height=800,
        text="A rare epikataphoray reading",
        chars=(),
        x0=320,
        x1=520,
        top=180,
        bottom=194,
        font_size=12,
        bold=False,
        links=(),
        soft_hyphen_end=False,
        hard_hyphen_end=False,
        rotated=False,
    )
    table = pdf_conversion_module._PdfTable(
        (60, 100, 540, 300),
        (
            ("Label", "Value"),
            ("Case", "A rare epikataphoray reading"),
        ),
        pdf_conversion_module._TableRendering.HTML,
        inferred_from_raster=True,
    )
    page = pdf_conversion_module._PdfPage(
        8,
        (line,),
        has_images=True,
        image_area_ratios=(1.0,),
        has_table=True,
        image_orientation_mismatch=False,
        tables=(table,),
    )

    updated = pdf_conversion_module._apply_page_text_replacements(
        page,
        {
            pdf_conversion_module._visual_line_key(line): "A rare epikataphora reading",
        },
    )

    assert updated.lines[0].text == "A rare epikataphora reading"
    assert updated.tables[0].rows[1][1] == "A rare epikataphora reading"


def test_visual_replacement_updates_one_unique_atom_in_a_longer_table_cell() -> None:
    line = pdf_conversion_module._PdfLine(
        page_number=8,
        page_width=600,
        page_height=800,
        text="A rare epikataphoray reading",
        chars=(),
        x0=320,
        x1=520,
        top=180,
        bottom=194,
        font_size=12,
        bold=False,
        links=(),
        soft_hyphen_end=False,
        hard_hyphen_end=False,
        rotated=False,
    )
    table = pdf_conversion_module._PdfTable(
        (60, 100, 540, 300),
        (
            ("Label", "Value"),
            ("Case", "Earlier context and an epikataphoray reading\nwith a continuation"),
        ),
        pdf_conversion_module._TableRendering.HTML,
        inferred_from_raster=True,
    )
    page = pdf_conversion_module._PdfPage(
        8,
        (line,),
        has_images=True,
        image_area_ratios=(1.0,),
        has_table=True,
        image_orientation_mismatch=False,
        tables=(table,),
    )

    updated = pdf_conversion_module._apply_page_text_replacements(
        page,
        {
            pdf_conversion_module._visual_line_key(line): "A rare epikataphora reading",
        },
    )

    assert updated.tables[0].rows[1][1] == (
        "Earlier context and an epikataphora reading\nwith a continuation"
    )


def test_hidden_text_budget_selects_scanned_contents_but_not_ordinary_prose() -> None:
    def page(number: int, lines: tuple[pdf_conversion_module._PdfLine, ...]):
        return pdf_conversion_module._PdfPage(
            number,
            lines,
            has_images=True,
            image_area_ratios=(1.0,),
            has_table=False,
            image_orientation_mismatch=False,
        )

    toc_lines = tuple(
        pdf_conversion_module._PdfLine(
            page_number=1,
            page_width=600,
            page_height=800,
            text=f"Chapter {index} {index * 10}",
            chars=(),
            x0=72,
            x1=520,
            top=100 + index * 20,
            bottom=114 + index * 20,
            font_size=12,
            bold=False,
            links=(),
            soft_hyphen_end=False,
            hard_hyphen_end=False,
            rotated=False,
        )
        for index in range(1, 7)
    )
    prose_line = replace(
        toc_lines[0],
        page_number=2,
        text="This ordinary scanned paragraph has a useful selectable text layer.",
    )

    selected = pdf_conversion_module._hidden_text_audit_pages(
        [page(1, toc_lines), page(2, (prose_line,))]
    )

    assert selected == {1}


def test_hidden_text_audit_budget_scales_with_the_selected_interval() -> None:
    pages = []
    for page_number in range(1, 21):
        lines = tuple(
            pdf_conversion_module._PdfLine(
                page_number=page_number,
                page_width=600,
                page_height=800,
                text=f"Chapter {index} {index * 10}",
                chars=(),
                x0=72,
                x1=520,
                top=100 + index * 20,
                bottom=114 + index * 20,
                font_size=12,
                bold=False,
                links=(),
                soft_hyphen_end=False,
                hard_hyphen_end=False,
                rotated=False,
            )
            for index in range(1, 7)
        )
        pages.append(
            pdf_conversion_module._PdfPage(
                page_number,
                lines,
                has_images=True,
                image_area_ratios=(1.0,),
                has_table=False,
                image_orientation_mismatch=False,
            )
        )

    selected = pdf_conversion_module._hidden_text_audit_pages(pages)

    assert selected == {1, 2, 3, 4, 5}


def test_secondary_native_engine_can_confirm_one_ocr_spelling() -> None:
    line = pdf_conversion_module._PdfLine(
        page_number=1,
        page_width=600,
        page_height=800,
        text="The Horimiea 1135",
        chars=(),
        x0=72,
        x1=520,
        top=100,
        bottom=114,
        font_size=12,
        bold=False,
        links=(),
        soft_hyphen_end=False,
        hard_hyphen_end=False,
        rotated=False,
    )
    page = pdf_conversion_module._PdfPage(
        1,
        (line,),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    reconciled, changes = pdf_conversion_module._reconcile_secondary_native_text(
        [page],
        {1: "The Horimaea 1135"},
        {1: "The Horimaea 1135"},
    )

    assert changes == 1
    assert reconciled[0].lines[0].text == "The Horimaea 1135"


def test_geometry_aligned_secondary_engine_can_confirm_one_ocr_spelling() -> None:
    line = _pdf_model_line(1, "The Horimiea 1135")
    page = pdf_conversion_module._PdfPage(
        1,
        (line,),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    reconciled, changes = pdf_conversion_module._reconcile_secondary_native_text_lines(
        [page],
        {1: "The Horimæa 1135"},
        {pdf_conversion_module._visual_line_key(line): "The Horimæa 1135"},
    )

    assert changes == 1
    assert reconciled[0].lines[0].text == "The Horimæa 1135"


def test_geometry_aligned_secondary_engine_rejects_a_third_reading() -> None:
    line = _pdf_model_line(1, "The Horimiea 1135")
    page = pdf_conversion_module._PdfPage(
        1,
        (line,),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    reconciled, changes = pdf_conversion_module._reconcile_secondary_native_text_lines(
        [page],
        {1: "The Horimæa 1135"},
        {pdf_conversion_module._visual_line_key(line): "The Horimiea 1135"},
    )

    assert changes == 0
    assert reconciled == [page]


def test_secondary_native_repairs_a_broken_dollar_font_mapping_before_ocr() -> None:
    first = _pdf_model_line(1, "$Que ocurre ahora en este ejemplo?")
    second = replace(
        _pdf_model_line(1, "La $atencion conserva 2026 intacto."),
        top=140,
        bottom=152,
    )
    page = pdf_conversion_module._PdfPage(
        1,
        (first, second),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    reconciled, changes = pdf_conversion_module._reconcile_secondary_dollar_glyphs(
        [page],
        {1: "¿Qué ocurre ahora en este ejemplo?\nLa atención conserva 2026 intacto."},
    )

    assert changes == 2
    assert reconciled[0].lines[0].text == "¿Qué ocurre ahora en este ejemplo?"
    assert reconciled[0].lines[1].text == "La atención conserva 2026 intacto."


def test_secondary_native_rejects_ambiguous_dollar_font_readings() -> None:
    line = _pdf_model_line(1, "$Como sigue")
    page = pdf_conversion_module._PdfPage(
        1,
        (line,),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    reconciled, changes = pdf_conversion_module._reconcile_secondary_dollar_glyphs(
        [page],
        {1: "¿Cómo sigue\n¡Cómo sigue"},
    )

    assert changes == 0
    assert reconciled == [page]


def test_secondary_native_keeps_native_heading_capitalization() -> None:
    line = _pdf_model_line(1, "$QUE OCURRE EN ESTE EJEMPLO")
    page = pdf_conversion_module._PdfPage(
        1,
        (line,),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    reconciled, changes = pdf_conversion_module._reconcile_secondary_dollar_glyphs(
        [page],
        {1: "¿Qué ocurre en este ejemplo"},
    )

    assert changes == 1
    assert reconciled[0].lines[0].text == "¿QUÉ OCURRE EN ESTE EJEMPLO"


def test_geometry_aligned_secondary_line_repairs_a_grouped_font_glyph() -> None:
    line = _pdf_model_line(1, "La $atencion conserva 2026 intacto.")
    page = pdf_conversion_module._PdfPage(
        1,
        (line,),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    reconciled, changes = pdf_conversion_module._reconcile_secondary_dollar_glyph_lines(
        [page],
        {pdf_conversion_module._visual_line_key(line): "La atención conserva 2026 intacto."},
    )

    assert changes == 1
    assert reconciled[0].lines[0].text == "La atención conserva 2026 intacto."


def test_geometry_aligned_secondary_line_rejects_changed_words_or_numbers() -> None:
    line = _pdf_model_line(1, "La $atencion conserva 2026 intacto.")
    page = pdf_conversion_module._PdfPage(
        1,
        (line,),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    reconciled, changes = pdf_conversion_module._reconcile_secondary_dollar_glyph_lines(
        [page],
        {pdf_conversion_module._visual_line_key(line): "La edición conserva 2027 intacto."},
    )

    assert changes == 0
    assert reconciled == [page]


@pytest.mark.parametrize(
    ("native", "secondary"),
    (
        ("THE RESULT WILLK ARRIVE", "THE RESULT WILL ARRIVE"),
        ("Service to EKI remains.", "Service to Él remains."),
        ("La cancion continúa.", "La canción continúa."),
    ),
)
def test_systemic_secondary_native_repairs_bounded_font_damage(
    native: str,
    secondary: str,
) -> None:
    line = _pdf_model_line(1, native)
    page = pdf_conversion_module._PdfPage(
        1,
        (line,),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    reconciled, changes = pdf_conversion_module._reconcile_systemic_secondary_native_lines(
        [page],
        {pdf_conversion_module._visual_line_key(line): secondary},
    )

    assert changes == 1
    assert reconciled[0].lines[0].text == secondary


def test_systemic_secondary_native_does_not_import_pdfium_word_splitting() -> None:
    line = _pdf_model_line(1, "The service remains correctly spaced.")
    page = pdf_conversion_module._PdfPage(
        1,
        (line,),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    reconciled, changes = pdf_conversion_module._reconcile_systemic_secondary_native_lines(
        [page],
        {pdf_conversion_module._visual_line_key(line): ("The ser vice remains cor rectly spaced.")},
    )

    assert changes == 0
    assert reconciled == [page]


def test_systemic_secondary_native_joins_only_a_repeated_pdfium_word() -> None:
    first = _pdf_model_line(1, "The sacer dotisa returns.")
    second = replace(
        _pdf_model_line(1, "Another sacerdotisa appears."),
        top=140,
        bottom=152,
    )
    page = pdf_conversion_module._PdfPage(
        1,
        (first, second),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    reconciled, changes = pdf_conversion_module._reconcile_systemic_secondary_native_lines(
        [page],
        {
            pdf_conversion_module._visual_line_key(first): "The sacerdotisa returns.",
            pdf_conversion_module._visual_line_key(second): "Another sacerdotisa appears.",
        },
    )

    assert changes == 1
    assert reconciled[0].lines[0].text == "The sacerdotisa returns."
    assert reconciled[0].lines[1].text == second.text


@pytest.mark.parametrize(
    "secondary",
    (
        "The altered sentence keeps 2026.",
        "The original sentence keeps 2027.",
        "The original sentence omits content.",
    ),
)
def test_systemic_secondary_native_rejects_content_or_number_changes(
    secondary: str,
) -> None:
    line = _pdf_model_line(1, "The original sentence keeps 2026 intact.")
    page = pdf_conversion_module._PdfPage(
        1,
        (line,),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    reconciled, changes = pdf_conversion_module._reconcile_systemic_secondary_native_lines(
        [page],
        {pdf_conversion_module._visual_line_key(line): secondary},
    )

    assert changes == 0
    assert reconciled == [page]


def test_pdf_outline_repairs_and_promotes_a_matching_broken_heading() -> None:
    line = replace(
        _pdf_model_line(8, "$QUE OCURRE AHORA"),
        top=100,
        bottom=118,
        font_size=14,
        bold=True,
    )
    page = pdf_conversion_module._PdfPage(
        8,
        (line,),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    reconciled, changes = pdf_conversion_module._reconcile_pdf_outline_headings(
        [page],
        (pdf_conversion_module._PdfOutlineEntry(1, "¿QUÉ OCURRE AHORA?", 8),),
    )

    assert changes == 1
    assert reconciled[0].lines[0].text == "¿QUÉ OCURRE AHORA?"
    assert reconciled[0].lines[0].outline_level == 2
    assert (
        pdf_conversion_module._heading_level(
            reconciled[0].lines[0],
            body_size=12,
            heading_sizes={},
            gap_before=24,
            toc_page=False,
        )
        == 2
    )


def test_pdf_outline_heading_carries_private_navigation_evidence() -> None:
    line = replace(
        _pdf_model_line(8, "Confirmed section"),
        outline_level=3,
    )
    blocks: list[pdf_conversion_module._MarkdownBlock] = []

    pdf_conversion_module._append_heading(blocks, line, 3, 24)
    markdown = pdf_conversion_module._blocks_to_markdown(blocks)

    assert markdown == ("<!-- PZDOC PDF OUTLINE 3 -->\n\n### Confirmed section")
    assert pdf_conversion_module.strip_pdf_page_markers(markdown) == markdown
    assert pdf_conversion_module.strip_pdf_public_markers(markdown) == ("\n\n### Confirmed section")


def test_pdf_outline_joins_a_uniquely_matching_multiline_heading() -> None:
    first = replace(_pdf_model_line(12, "A LONG CHAPTER"), top=100, bottom=118)
    second = replace(_pdf_model_line(12, "HEADING"), top=120, bottom=138)
    page = pdf_conversion_module._PdfPage(
        12,
        (first, second),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    reconciled, changes = pdf_conversion_module._reconcile_pdf_outline_headings(
        [page],
        (pdf_conversion_module._PdfOutlineEntry(2, "A Long Chapter Heading", 12),),
    )

    assert changes == 1
    assert len(reconciled[0].lines) == 1
    assert reconciled[0].lines[0].text == "A Long Chapter Heading"
    assert reconciled[0].lines[0].outline_level == 3


def test_pdf_outline_rejects_an_ambiguous_or_numerically_changed_match() -> None:
    first = _pdf_model_line(12, "Chapter 2 Overview")
    second = replace(first, top=140, bottom=152)
    page = pdf_conversion_module._PdfPage(
        12,
        (first, second),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    reconciled, changes = pdf_conversion_module._reconcile_pdf_outline_headings(
        [page],
        (
            pdf_conversion_module._PdfOutlineEntry(1, "Chapter 2 Overview", 12),
            pdf_conversion_module._PdfOutlineEntry(1, "Chapter 3 Overview", 12),
        ),
    )

    assert changes == 0
    assert reconciled == [page]


@pytest.mark.parametrize(
    ("native_token", "consensus_token"),
    (
        ("epikataphoray", "epikataphora"),
        ("Sunaphê", "Sunaphē"),
        ("xMoon", "Moon"),
    ),
)
def test_document_consensus_repairs_one_ocr_token_confirmed_elsewhere(
    native_token: str,
    consensus_token: str,
) -> None:
    target = _pdf_model_line(1, f"A rare {native_token} reading")
    donor = _pdf_model_line(2, f"Another {consensus_token} source")
    table = pdf_conversion_module._PdfTable(
        (60, 80, 560, 220),
        (("Label", "Value"), ("Case", target.text)),
        pdf_conversion_module._TableRendering.HTML,
        inferred_from_raster=True,
    )
    pages = [
        pdf_conversion_module._PdfPage(
            1,
            (target,),
            has_images=True,
            image_area_ratios=(1.0,),
            has_table=True,
            image_orientation_mismatch=False,
            tables=(table,),
        ),
        pdf_conversion_module._PdfPage(
            2,
            (donor,),
            has_images=False,
            image_area_ratios=(),
            has_table=False,
            image_orientation_mismatch=False,
        ),
    ]

    reconciled, changes = pdf_conversion_module._reconcile_document_token_consensus(
        pages,
        {
            1: f"A rare {consensus_token} reading",
            2: f"Another {consensus_token} source",
        },
    )

    assert changes == 1
    assert reconciled[0].lines[0].text == f"A rare {consensus_token} reading"
    assert reconciled[0].tables[0].rows[1][1] == f"A rare {consensus_token} reading"


def test_document_consensus_rejects_an_ocr_only_spelling() -> None:
    line = _pdf_model_line(1, "A rare epikataphoray reading")
    page = pdf_conversion_module._PdfPage(
        1,
        (line,),
        has_images=True,
        image_area_ratios=(1.0,),
        has_table=False,
        image_orientation_mismatch=False,
    )

    reconciled, changes = pdf_conversion_module._reconcile_document_token_consensus(
        [page],
        {1: "A rare epikataphora reading"},
    )

    assert changes == 0
    assert reconciled == [page]


def test_document_consensus_does_not_remove_a_diacritic() -> None:
    target = _pdf_model_line(1, "A succèdent place")
    donor = _pdf_model_line(2, "Another succedent place")
    pages = [
        pdf_conversion_module._PdfPage(
            1,
            (target,),
            has_images=True,
            image_area_ratios=(1.0,),
            has_table=False,
            image_orientation_mismatch=False,
        ),
        pdf_conversion_module._PdfPage(
            2,
            (donor,),
            has_images=False,
            image_area_ratios=(),
            has_table=False,
            image_orientation_mismatch=False,
        ),
    ]

    reconciled, changes = pdf_conversion_module._reconcile_document_token_consensus(
        pages,
        {1: "A succedent place", 2: "Another succedent place"},
    )

    assert changes == 0
    assert reconciled == pages


def test_document_consensus_does_not_normalize_an_internal_capital() -> None:
    target = _pdf_model_line(1, "A printed worlD form")
    donor = _pdf_model_line(2, "An ordinary world form")
    pages = [
        pdf_conversion_module._PdfPage(
            1,
            (target,),
            has_images=True,
            image_area_ratios=(1.0,),
            has_table=False,
            image_orientation_mismatch=False,
        ),
        pdf_conversion_module._PdfPage(
            2,
            (donor,),
            has_images=False,
            image_area_ratios=(),
            has_table=False,
            image_orientation_mismatch=False,
        ),
    ]

    reconciled, changes = pdf_conversion_module._reconcile_document_token_consensus(
        pages,
        {1: "A printed world form", 2: "An ordinary world form"},
    )

    assert changes == 0
    assert reconciled == pages


def test_numeric_glyph_consensus_repairs_only_values_confirmed_by_aligned_ocr() -> None:
    line = _pdf_model_line(1, "Degrees 6o and folio 3OI")
    page = pdf_conversion_module._PdfPage(
        1,
        (line,),
        has_images=True,
        image_area_ratios=(1.0,),
        has_table=False,
        image_orientation_mismatch=False,
    )

    reconciled, changes = pdf_conversion_module._reconcile_suspicious_numbers_from_ocr(
        [page],
        {1: "Degrees 60 and folio 301"},
    )

    assert changes == 1
    assert reconciled[0].lines[0].text == "Degrees 60 and folio 301"


def test_numeric_glyph_consensus_keeps_an_unconfirmed_identifier() -> None:
    line = _pdf_model_line(1, "Reference A1O7")
    page = pdf_conversion_module._PdfPage(
        1,
        (line,),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    reconciled, changes = pdf_conversion_module._reconcile_suspicious_numbers_from_ocr(
        [page],
        {1: "Reference A107"},
    )

    assert changes == 0
    assert reconciled == [page]


def test_repeated_non_currency_dollar_glyphs_trigger_local_ocr() -> None:
    line = _pdf_model_line(1, "Broken punctuation ”$ and quote $word remains")
    page = pdf_conversion_module._PdfPage(
        1,
        (line,),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    assert pdf_conversion_module._has_suspicious_glyph_encoding(page)


def test_one_unresolved_non_currency_dollar_glyph_triggers_local_ocr() -> None:
    line = _pdf_model_line(1, "One isolated $word remains unresolved")
    page = pdf_conversion_module._PdfPage(
        1,
        (line,),
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )

    assert pdf_conversion_module._has_suspicious_glyph_encoding(page)


def test_one_dollar_font_glyph_does_not_replace_an_otherwise_useful_native_page() -> None:
    lines = tuple(
        replace(
            _pdf_model_line(
                1,
                (
                    "This useful native paragraph remains authoritative even when "
                    f"one {'$' if index == 0 else ''}word is unresolved in line {index}."
                ),
            ),
            top=100 + index * 24,
            bottom=112 + index * 24,
        )
        for index in range(4)
    )
    page = pdf_conversion_module._PdfPage(
        1,
        lines,
        has_images=False,
        image_area_ratios=(),
        has_table=False,
        image_orientation_mismatch=False,
    )
    ocr = "\n".join(line.text.replace("$", "") for line in lines)

    assert not pdf_conversion_module._should_replace_with_ocr(page, ocr)


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


def test_pdf_demotes_a_merged_multisentence_heading_and_rejoins_its_continuation() -> None:
    previous_line = replace(
        _pdf_model_line(1, "continues into"),
        top=180,
        bottom=194,
    )
    current_line = replace(
        _pdf_model_line(1, "the final clause."),
        top=196,
        bottom=210,
    )
    false_heading = (
        "This is ordinary prose that was assigned a misleading local font size. "
        "It contains several complete sentences and enough explanatory detail to be a paragraph. "
        "A third sentence makes the classification unambiguous while the last line continues into"
    )
    blocks = [
        pdf_conversion_module._MarkdownBlock(
            "heading",
            false_heading,
            1,
            level=1,
            source_line=previous_line,
        ),
        pdf_conversion_module._MarkdownBlock(
            "paragraph",
            "the final clause.",
            1,
            source_line=current_line,
        ),
    ]

    markdown = pdf_conversion_module._blocks_to_markdown(blocks)

    assert not markdown.startswith("#")
    assert "\n\n" not in markdown
    assert markdown.endswith("continues into the final clause.")


def test_pdf_keeps_a_long_single_phrase_heading_structural() -> None:
    title = " ".join(["Extended"] * 40)
    blocks = [pdf_conversion_module._MarkdownBlock("heading", title, 1, level=2)]

    assert pdf_conversion_module._blocks_to_markdown(blocks) == f"## {title}"


@pytest.mark.parametrize(
    "sentence",
    [
        "Part One contains some traditional lists that were taught by the Buddha",
        "Chapter one, ‘The Three Trainings’, introduces morality and concentration",
        "Part Three, so I will give it a short treatment here",
        "Part One. An old dhamma friend explained the method to me",
    ],
)
def test_pdf_demotes_prose_that_only_begins_like_a_numbered_heading(sentence: str) -> None:
    blocks = [pdf_conversion_module._MarkdownBlock("heading", sentence, 1, level=2)]

    assert pdf_conversion_module._blocks_to_markdown(blocks) == sentence


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


def test_does_not_merge_a_complete_heading_after_hyphenated_prose() -> None:
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

    paragraph = line("Previous prose ends with a hyphen-", hard_hyphen_end=True)
    heading = line("complete chapter heading", hard_hyphen_end=False, bold=True)
    blocks = [
        pdf_conversion_module._MarkdownBlock(
            "paragraph",
            "Previous prose ends with a hyphen-",
            1,
            source_line=paragraph,
        ),
        pdf_conversion_module._MarkdownBlock(
            "heading",
            "complete chapter heading",
            1,
            level=2,
            source_line=heading,
        ),
    ]

    assert pdf_conversion_module._blocks_to_markdown(blocks) == (
        "Previous prose ends with a hyphen-\n\n## complete chapter heading"
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


def _write_open_raster_table_pdf(destination: Path, *, layout: str) -> None:
    width, height = 600, 800
    pixels = bytearray(b"\xff" * (width * height))
    matrix_layout = layout in {"matrix", "matrix_visual_placeholders"}
    rule_tops = (100, 170, 700) if matrix_layout else (100, 700)
    for top in rule_tops:
        for y in (top, top + 1):
            pixels[y * width + 115 : y * width + 485] = b"\x00" * 370
    if layout == "matrix_visual_placeholders":
        for baseline in (590, 470, 350, 230):
            top = height - baseline - 5
            for x in (170, 220):
                for y in (top, top + 1):
                    pixels[y * width + x : y * width + x + 12] = b"\x00" * 12
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
    if matrix_layout:
        first_column_x = 120
        text_cells = (
            (665, first_column_x, "KEY", "F2"),
            (665, 170, "SUN", "F2"),
            (665, 220, "MOON", "F2"),
            (665, 270, "MARS", "F2"),
            (650, first_column_x, "KIND", "F2"),
            (650, 170, "FIRE", "F2"),
            (650, 220, "WATR", "F2"),
            (650, 270, "AIR", "F2"),
            (590, first_column_x, "C1", "F1"),
            (590, 170, "-", "F1"),
            (590, 220, "-", "F1"),
            (585, 330, "Hit", "F1"),
            (470, first_column_x, "C2", "F1"),
            (470, 170, "-", "F1"),
            (470, 220, "-", "F1"),
            (465, 330, "Hit", "F1"),
            (350, first_column_x, "C3", "F1"),
            (350, 170, "-", "F1"),
            (350, 220, "-", "F1"),
            (345, 330, "Hit", "F1"),
            (230, first_column_x, "C4", "F1"),
            (230, 170, "-", "F1"),
            (230, 220, "-", "F1"),
            (225, 330, "Hit", "F1"),
        )
        if layout == "matrix_visual_placeholders":
            text_cells = tuple(cell for cell in text_cells if cell[2] != "-")
    else:
        labels = layout == "labels"
        text_cells = (
            (670, 125, "Authors" if labels else "First section", "F2" if labels else "F1"),
            (670, 330, "Significations" if labels else "Parallel prose", "F2" if labels else "F1"),
            (590, 125, "HERMES" if labels else "Second section", "F1"),
            (575, 125, "Egypt" if labels else "ordinary continuation", "F1"),
            (575, 310, "E" if labels else "x", "F1"),
            (590, 330, "Life and livelihood", "F1"),
            (520, 330, "continues below" if labels else "parallel continuation", "F1"),
            (470, 125, "THRASYLLUS" if labels else "Third section", "F1"),
            (455, 125, "Alexandria" if labels else "ordinary continuation", "F1"),
            (470, 330, "Fortune and death", "F1"),
            (350, 125, "VALENS" if labels else "Fourth section", "F1"),
            (335, 125, "Antioch" if labels else "ordinary continuation", "F1"),
            (350, 330, "Benefits and lawsuits", "F1"),
        )
    operations = [f"q\n{width} 0 0 {height} 0 0 cm\n/Im1 Do\nQ"]
    for y, x, value, font in text_cells:
        operations.append(f"BT\n/{font} 11 Tf\n{x} {y} Td\n({value}) Tj\nET")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] "
            b"/Resources << /XObject << /Im1 4 0 R >> "
            b"/Font << /F1 5 0 R /F2 6 0 R >> >> /Contents 7 0 R >>"
        ),
        image,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
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
