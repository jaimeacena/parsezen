from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import parsezen.ocr_conversion as ocr_module
import parsezen.ocr_executor as executor_module
from parsezen.cancellation import CancellationToken
from parsezen.errors import ConversionError, ProcessingCancelledError
from parsezen.ocr_conversion import (
    OCR_LANGUAGES,
    _clean_ocr_markdown,
    _convert_pdf_pages_in_process,
    _page_ranges,
    _worker_page_batches,
    convert_pdf_pages_with_ocr,
)


def test_ocr_covers_every_supported_latin_translation_language() -> None:
    assert set(OCR_LANGUAGES) == {"es", "en", "fr", "de", "it", "pt"}


def test_groups_only_contiguous_pages_into_ranges() -> None:
    assert _page_ranges({7, 2, 3, 4, 10, 11, 0}) == [(2, 3), (4, 4), (7, 7), (10, 11)]


def test_limits_ocr_ranges_to_safe_cancellation_batches() -> None:
    assert _page_ranges(set(range(1, 11))) == [
        (1, 2),
        (3, 4),
        (5, 6),
        (7, 8),
        (9, 10),
    ]


def test_groups_sparse_ranges_into_bounded_worker_lifetimes() -> None:
    assert _worker_page_batches({1, 3, 5, 7, 9, 11, 13, 15, 17}) == [
        {1, 3, 5, 7, 9, 11, 13, 15},
        {17},
    ]


def test_cleans_placeholders_page_numbers_and_shadowed_headings() -> None:
    markdown = """1

## CREATE YO' CREATE YO' CREATE YO' VISION VISION VISION

<!-- image -->

Useful text.
"""

    assert _clean_ocr_markdown(markdown) == "## CREATE YO' VISION\n\nUseful text."


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("# # CREATE YO' VISION", "## CREATE YO' VISION"),
        ("# # # LEVEL UP", "### LEVEL UP"),
    ],
)
def test_ocr_cleaning_repairs_spaced_atx_heading_markers(
    source: str,
    expected: str,
) -> None:
    assert _clean_ocr_markdown(source) == expected


def test_ocr_cleaning_replaces_xml_forbidden_controls_without_joining_words() -> None:
    assert _clean_ocr_markdown("First\x1fSecond\n\nThird\x01Fourth") == (
        "First Second\n\nThird Fourth"
    )


def test_ocr_cleaning_detaches_a_caption_merged_into_a_table_header() -> None:
    markdown = (
        "TABLE 11. Local comparison | First | Second | Third |\n"
        "| --- | --- | --- |\n"
        "| Row one | A | B |\n"
    )

    assert _clean_ocr_markdown(markdown) == (
        "TABLE 11. Local comparison\n\n"
        "| First | Second | Third |\n"
        "| --- | --- | --- |\n"
        "| Row one | A | B |"
    )


def test_full_page_recovery_drops_only_tiny_isolated_ocr_labels() -> None:
    def cell(text: str, height: float, width: float = 160.0):
        box = SimpleNamespace(height=height, width=width)
        return SimpleNamespace(
            text=text,
            from_ocr=True,
            to_bounding_box=lambda: box,
        )

    cells = [
        cell("Main title", 70.0, 500.0),
        cell("A complete descriptive subtitle", 48.0, 620.0),
        cell("Zqx", 12.0, 45.0),
        cell("Author Name", 44.0, 280.0),
        cell("Publisher", 40.0, 180.0),
    ]
    result = SimpleNamespace(
        pages=[
            SimpleNamespace(
                size=SimpleNamespace(width=1_000.0, height=1_400.0),
                assembled=SimpleNamespace(
                    elements=[SimpleNamespace(cluster=SimpleNamespace(cells=cells))]
                ),
            )
        ]
    )
    markdown = "# Main title\n\nA complete descriptive subtitle\n\nZqx\n\nAuthor Name\n"

    cleaned = ocr_module._strip_low_prominence_ocr_lines(markdown, result)

    assert "Zqx" not in cleaned
    assert "Main title" in cleaned
    assert "Author Name" in cleaned


def test_full_page_recovery_keeps_uniform_short_ocr_labels() -> None:
    def cell(text: str):
        box = SimpleNamespace(height=30.0, width=80.0)
        return SimpleNamespace(
            text=text,
            from_ocr=True,
            to_bounding_box=lambda: box,
        )

    cells = [cell("One"), cell("Two"), cell("Act"), cell("Name")]
    result = SimpleNamespace(
        pages=[
            SimpleNamespace(
                size=SimpleNamespace(width=1_000.0, height=1_400.0),
                assembled=SimpleNamespace(
                    elements=[SimpleNamespace(cluster=SimpleNamespace(cells=cells))]
                ),
            )
        ]
    )

    assert ocr_module._strip_low_prominence_ocr_lines("Act\n", result) == "Act\n"


def test_low_prominence_filter_uses_only_the_requested_batch_page() -> None:
    def page(label: str, label_height: float):
        def cell(text: str, height: float, width: float):
            box = SimpleNamespace(height=height, width=width)
            return SimpleNamespace(
                text=text,
                from_ocr=True,
                to_bounding_box=lambda: box,
            )

        cells = [
            cell("Main title", 60.0, 500.0),
            cell("Subtitle", 48.0, 300.0),
            cell(label, label_height, 45.0),
            cell("Author Name", 44.0, 260.0),
        ]
        return SimpleNamespace(
            size=SimpleNamespace(width=1_000.0, height=1_400.0),
            assembled=SimpleNamespace(
                elements=[SimpleNamespace(cluster=SimpleNamespace(cells=cells))]
            ),
        )

    result = SimpleNamespace(pages=[page("Act", 44.0), page("Zqx", 10.0)])

    first = ocr_module._strip_low_prominence_ocr_lines(
        "Act\n",
        result,
        page_number=1,
        fallback_index=0,
    )
    second = ocr_module._strip_low_prominence_ocr_lines(
        "Zqx\n",
        result,
        page_number=2,
        fallback_index=1,
    )

    assert first == "Act\n"
    assert second == ""


def test_public_ocr_boundary_delegates_to_the_private_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "scan.pdf"
    cancellation = CancellationToken()
    progress: list[tuple[int, int]] = []
    received: list[
        tuple[
            Path,
            set[int],
            CancellationToken | None,
            set[int] | None,
            object,
        ]
    ] = []

    def run_worker(
        received_source: Path,
        pages: set[int],
        received_cancellation: CancellationToken | None,
        *,
        force_full_page_numbers: set[int] | None,
        on_progress,
        on_page_result,
    ) -> dict[int, str]:
        received.append(
            (
                received_source,
                pages,
                received_cancellation,
                force_full_page_numbers,
                on_progress,
            )
        )
        if on_progress is not None:
            on_progress(1, 1)
        on_page_result(2, "Texto local.")
        return {2: "Texto local."}

    monkeypatch.setattr(executor_module, "run_ocr_worker", run_worker)

    assert convert_pdf_pages_with_ocr(
        source,
        {2},
        cancellation,
        force_full_page_numbers={2},
        on_progress=lambda current, total: progress.append((current, total)),
    ) == {2: "Texto local."}
    assert callable(received[0][4])
    assert received[0][:4] == (source, {2}, cancellation, {2})
    assert progress == [(1, 1)]


def test_public_ocr_restarts_failed_batches_and_degrades_to_single_pages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "scan.pdf"
    failures = {"batch": 2, 1: 0, 2: 2}
    calls: list[set[int]] = []

    def run_worker(_source, pages, _cancellation, **_kwargs):
        calls.append(set(pages))
        if len(pages) == 2:
            failures["batch"] -= 1
            raise ConversionError("batch failed")
        page = next(iter(pages))
        if failures[page]:
            failures[page] -= 1
            raise ConversionError("page failed")
        return {page: f"OCR {page}"}

    monkeypatch.setattr(executor_module, "run_ocr_worker", run_worker)

    result = convert_pdf_pages_with_ocr(source, {1, 2})

    assert result == {1: "OCR 1"}
    assert calls == [{1, 2}, {1, 2}, {1}, {2}, {2}]


def test_public_ocr_retries_only_unfinished_pages_after_a_partial_worker_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "scan.pdf"
    calls: list[set[int]] = []

    def run_worker(_source, pages, _cancellation, *, on_page_result, **_kwargs):
        calls.append(set(pages))
        if pages == {1, 3, 5}:
            on_page_result(1, "OCR 1")
            raise ConversionError("worker failed after one page")
        return {page: f"OCR {page}" for page in pages}

    monkeypatch.setattr(executor_module, "run_ocr_worker", run_worker)

    assert convert_pdf_pages_with_ocr(source, {1, 3, 5}) == {
        1: "OCR 1",
        3: "OCR 3",
        5: "OCR 5",
    }
    assert calls == [{1, 3, 5}, {3, 5}]


def test_translates_ocr_dependency_failures_to_an_application_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_to_start():
        raise RuntimeError("dependency detail")

    monkeypatch.setattr(ocr_module, "_document_converter", fail_to_start)

    with pytest.raises(ConversionError, match="modelos OCR"):
        _convert_pdf_pages_in_process(tmp_path / "scan.pdf", {1})


def test_ocr_cancellation_is_not_wrapped_as_a_conversion_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = CancellationToken()
    calls: list[tuple[int, int]] = []
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-local")

    class Converter:
        def convert(self, _source: Path, *, page_range: tuple[int, int]):
            calls.append(page_range)
            cancellation.cancel()
            return object()

    monkeypatch.setattr(ocr_module, "_document_converter", Converter)

    with pytest.raises(ProcessingCancelledError):
        _convert_pdf_pages_in_process(
            source,
            set(range(1, 9)),
            cancellation=cancellation,
        )

    assert calls == [(1, 2)]


def test_uses_an_ascii_file_path_for_unicode_pdf_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "Gestión — escaneada.pdf"
    source.write_bytes(b"%PDF-local-content")
    received_sources: list[tuple[str, bytes]] = []

    class Document:
        def export_to_markdown(self, **_kwargs: object) -> str:
            return "Texto reconocido."

    class Converter:
        def convert(self, received_source: object, *, page_range: tuple[int, int]):
            assert page_range == (1, 1)
            assert isinstance(received_source, Path)
            received_sources.append((received_source.name, received_source.read_bytes()))
            return SimpleNamespace(document=Document())

    monkeypatch.setattr(ocr_module, "_document_converter", Converter)

    result = _convert_pdf_pages_in_process(source, {1})

    assert result == {1: "Texto reconocido."}
    assert received_sources == [("parsezen.pdf", b"%PDF-local-content")]


def test_recovers_missing_ocr_pages_from_rendered_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "table.pdf"
    source.write_bytes(b"%PDF-placeholder")

    class EmptyDocument:
        def export_to_markdown(self, **_kwargs: object) -> str:
            return ""

    class Converter:
        def convert(self, _source: object, *, page_range: tuple[int, int]):
            assert page_range == (1, 1)
            return SimpleNamespace(document=EmptyDocument())

    monkeypatch.setattr(ocr_module, "_document_converter", Converter)
    monkeypatch.setattr(
        ocr_module,
        "_recover_pages_from_images",
        lambda _converter, _source, pages, _cancellation, **_kwargs: {
            page: "| Campo | Valor |\n| --- | --- |" for page in pages
        },
    )

    assert _convert_pdf_pages_in_process(source, {1}) == {1: "| Campo | Valor |\n| --- | --- |"}


def test_reports_a_successful_empty_ocr_result_for_negative_checkpointing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "empty.pdf"
    source.write_bytes(b"%PDF-placeholder")

    class EmptyDocument:
        def export_to_markdown(self, **_kwargs: object) -> str:
            return ""

    class Converter:
        def convert(self, _source: object, *, page_range: tuple[int, int]):
            assert page_range == (1, 1)
            return SimpleNamespace(document=EmptyDocument())

    monkeypatch.setattr(ocr_module, "_document_converter", Converter)
    monkeypatch.setattr(
        ocr_module,
        "_recover_pages_from_images",
        lambda _converter, _source, _pages, _cancellation, **_kwargs: {},
    )
    page_results: list[tuple[int, str]] = []

    result = _convert_pdf_pages_in_process(
        source,
        {1},
        on_page_result=lambda page, markdown: page_results.append((page, markdown)),
    )

    assert result == {}
    assert page_results == [(1, "")]


def test_does_not_negative_checkpoint_an_ocr_page_after_a_conversion_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "failed.pdf"
    source.write_bytes(b"%PDF-placeholder")

    class Converter:
        def convert(self, _source: object, *, page_range: tuple[int, int]):
            assert page_range == (1, 1)
            raise RuntimeError("temporary OCR failure")

    monkeypatch.setattr(ocr_module, "_document_converter", Converter)
    monkeypatch.setattr(
        ocr_module,
        "_recover_pages_from_images",
        lambda _converter, _source, _pages, _cancellation, **_kwargs: {},
    )
    page_results: list[tuple[int, str]] = []

    with pytest.raises(ConversionError, match="OCR"):
        _convert_pdf_pages_in_process(
            source,
            {1},
            on_page_result=lambda page, markdown: page_results.append((page, markdown)),
        )

    assert page_results == []


def test_prefers_a_rotated_crop_for_an_orientation_mismatched_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "rotated-image.pdf"
    source.write_bytes(b"placeholder")
    cropped_image = Image.new("RGB", (100, 220), "white")
    cropped_image.paste("black", (20, 20, 80, 200))
    full_image = Image.new("RGB", (500, 700), "white")
    full_image.paste("black", (100, 100, 300, 500))
    image_metadata = {
        "x0": 100,
        "x1": 300,
        "top": 100,
        "bottom": 500,
        "width": 200,
        "height": 400,
        "srcsize": (900, 400),
    }

    class Rendered:
        def __init__(self, image) -> None:
            self.original = image

    class CroppedPage:
        def to_image(self, **_kwargs):
            return Rendered(cropped_image)

    class Page:
        images = [image_metadata]

        def to_image(self, **_kwargs):
            return Rendered(full_image)

        def crop(self, bbox):
            assert bbox == (100.0, 100.0, 300.0, 500.0)
            return CroppedPage()

    class Pdf:
        pages = [Page()]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    converted_sizes: list[tuple[int, int]] = []

    class Document:
        def export_to_markdown(self, **_kwargs):
            return "Recovered text from the correctly rotated embedded image with enough letters."

    class Converter:
        def convert(self, source_stream):
            image = Image.open(source_stream.stream)
            converted_sizes.append(image.size)
            return SimpleNamespace(document=Document())

    monkeypatch.setattr(ocr_module.pdfplumber, "open", lambda _source: Pdf())

    result = ocr_module._recover_pages_from_images(Converter(), source, {1}, None)

    assert result[1].startswith("Recovered text")
    assert converted_sizes == [(220, 100)]
