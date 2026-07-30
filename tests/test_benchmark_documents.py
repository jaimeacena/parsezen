from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import scripts.benchmark_documents as benchmark_module

from parsezen.pdf_conversion import PdfQualityReport


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
