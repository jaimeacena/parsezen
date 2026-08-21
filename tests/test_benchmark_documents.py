from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import scripts.benchmark_documents as benchmark_module

from parsezen.pdf_conversion import PdfProgressPhase, PdfQualityReport


def test_private_pdf_benchmark_records_no_document_text_and_detects_regressions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "private-book.pdf"
    source.write_bytes(b"%PDF-private-source-content")
    manifest = tmp_path / "local-benchmark.json"
    markdown = "# Secret heading\n\nPrivate converted paragraph with [link](<https://example.com>)."

    def convert(_source: Path, *, on_quality_report, **_kwargs) -> str:
        on_quality_report(PdfQualityReport((1, 2), (2,), ()))
        return markdown

    monkeypatch.setattr(benchmark_module, "convert_pdf", convert)
    monkeypatch.setattr(benchmark_module, "_working_set_bytes", lambda: 100 * 1024 * 1024)

    recorded = benchmark_module.record_manifest(manifest, (source,))

    manifest_text = manifest.read_text(encoding="utf-8")
    assert markdown not in manifest_text
    assert "private-source-content" not in manifest_text
    assert recorded[0].expected.headings == 1
    assert recorded[0].expected.links == 1
    assert recorded[0].expected.processed_pages == 2
    assert recorded[0].expected.ocr_pages == 1
    assert benchmark_module.check_manifest(manifest)[0].passed

    monkeypatch.setattr(
        benchmark_module,
        "convert_pdf",
        lambda _source, **_kwargs: "# Changed result",
    )
    changed = benchmark_module.check_manifest(manifest)[0]

    assert not changed.passed
    assert any("huella Markdown" in problem for problem in changed.problems)


def test_private_pdf_benchmark_requires_a_new_baseline_when_the_source_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF-first")
    manifest = tmp_path / "benchmark.json"
    monkeypatch.setattr(benchmark_module, "convert_pdf", lambda *_args, **_kwargs: "Result")
    monkeypatch.setattr(benchmark_module, "_working_set_bytes", lambda: None)
    benchmark_module.record_manifest(manifest, (source,))
    source.write_bytes(b"%PDF-second")

    result = benchmark_module.check_manifest(manifest)[0]

    assert not result.passed
    assert result.metrics is None
    assert result.problems == ("El documento cambió; vuelve a registrar su referencia.",)


def test_private_pdf_benchmark_uses_worst_repeated_resource_sample(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF")
    samples = iter(
        (
            benchmark_module.PdfMetrics("a" * 64, 10, 1, 0, 0, 50, 4, 60.0, 512.0),
            benchmark_module.PdfMetrics("a" * 64, 10, 1, 0, 0, 50, 4, 90.0, 768.0),
        )
    )
    monkeypatch.setattr(benchmark_module, "measure_pdf", lambda *_args, **_kwargs: next(samples))

    recorded = benchmark_module.record_manifest(
        tmp_path / "benchmark.json",
        (source,),
        runs=2,
    )[0]

    assert recorded.expected.elapsed_seconds == 90.0
    assert recorded.expected.peak_incremental_mib == 768.0
    assert recorded.max_elapsed_seconds == pytest.approx(121.5)
    assert recorded.max_peak_mib == pytest.approx(1_036.8)


def test_private_pdf_benchmark_rejects_unstable_repeated_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF")
    samples = iter(
        (
            benchmark_module.PdfMetrics("a" * 64, 10, 1, 0, 0, 50, 4, 60.0, 512.0),
            benchmark_module.PdfMetrics("b" * 64, 10, 1, 0, 0, 50, 4, 61.0, 513.0),
        )
    )
    monkeypatch.setattr(benchmark_module, "measure_pdf", lambda *_args, **_kwargs: next(samples))

    with pytest.raises(RuntimeError, match="salida estructural estable"):
        benchmark_module.record_manifest(
            tmp_path / "benchmark.json",
            (source,),
            runs=2,
        )


@pytest.mark.parametrize("runs", [0, 11, True])
def test_private_pdf_benchmark_rejects_invalid_recording_runs(
    tmp_path: Path,
    runs: object,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF")

    with pytest.raises(ValueError, match="repeticiones"):
        benchmark_module.record_manifest(
            tmp_path / "benchmark.json",
            (source,),
            runs=runs,  # type: ignore[arg-type]
        )


def test_private_pdf_benchmark_rejects_an_untrusted_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "benchmark.json"
    manifest.write_text('{"schema_version": 1, "documents": []}', encoding="utf-8")

    with pytest.raises(ValueError, match="formato compatible"):
        benchmark_module.load_manifest(manifest)


def test_private_pdf_benchmark_counts_descendant_working_sets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        def __init__(self, rss: int, children: tuple[Process, ...] = ()) -> None:
            self.rss = rss
            self.descendants = children

        def children(self, *, recursive: bool) -> tuple[Process, ...]:
            assert recursive
            return self.descendants

        def memory_info(self) -> SimpleNamespace:
            return SimpleNamespace(rss=self.rss)

    root = Process(100, (Process(200), Process(300)))
    fake_psutil = SimpleNamespace(
        Process=lambda _pid: root,
        Error=RuntimeError,
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    assert benchmark_module._working_set_bytes() == 600


def test_pdf_profile_matrix_measures_requested_long_document_prefixes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF")
    calls: list[object] = []

    def measure(_source: Path, *, page_range, **_kwargs):
        calls.append(page_range)
        return benchmark_module.PdfMetrics(
            "a" * 64,
            page_range.last_page,
            0,
            0,
            0,
            page_range.last_page,
            0,
            1.0,
            2.0,
        )

    monkeypatch.setattr(benchmark_module, "measure_pdf", measure)

    matrix = benchmark_module.measure_pdf_matrix(source, (100, 500, 1000, 500))

    assert tuple(matrix) == (100, 500, 1000)
    assert [item.last_page for item in calls] == [100, 500, 1000]


def test_pdf_profile_measures_page_rate_ocr_time_and_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF")
    clock = iter((0.0, 1.0, 2.0, 5.0, 10.0))
    monkeypatch.setattr(benchmark_module, "monotonic", clock.__next__)
    monkeypatch.setattr(benchmark_module, "_working_set_bytes", lambda: 100 * 1024 * 1024)

    def convert(_source: Path, *, on_quality_report, on_progress, **_kwargs):
        on_quality_report(PdfQualityReport((1, 2), (2,), ()))
        on_progress(PdfProgressPhase.EXTRACTING, 1, 2)
        on_progress(PdfProgressPhase.OCR, 1, 1)
        on_progress(PdfProgressPhase.STRUCTURING, 2, 2)
        return SimpleNamespace(
            markdown="# Result\n",
            resources=(SimpleNamespace(content=b"image"),),
        )

    monkeypatch.setattr(benchmark_module, "convert_pdf_document", convert)

    metrics = benchmark_module.measure_pdf(source, include_images=True)

    assert metrics.elapsed_seconds == 10.0
    assert metrics.elapsed_per_page_seconds == 5.0
    assert metrics.ocr_elapsed_seconds == 3.0
    assert metrics.resources == 1
    assert metrics.resource_bytes == 5
