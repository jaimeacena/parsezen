from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from scripts.validate_epub import (
    EpubCheckReport,
    Finding,
    load_report,
    new_errors,
    run_epubcheck,
)

from parsezen.application.book_editor import BookEditor, create_book_from_markdown, publish_book
from parsezen.epub_builder import EpubBookMetadata
from parsezen.epub_conversion import translate_epub
from parsezen.infrastructure.artifact_store import ArtifactStore
from test_epub_conversion import _write_epub3


def test_load_report_expands_every_reported_location(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps(
            {
                "messages": [
                    {
                        "ID": "RSC-005",
                        "severity": "ERROR",
                        "message": "  Invalid   element ",
                        "locations": [
                            {"path": "chapter1.xhtml", "line": 2},
                            {"path": "chapter2.xhtml", "line": 8},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = load_report(report_path)

    assert report.findings == (
        Finding("ERROR", "RSC-005", "chapter1.xhtml", "Invalid element"),
        Finding("ERROR", "RSC-005", "chapter2.xhtml", "Invalid element"),
    )


def test_new_errors_ignores_baseline_errors_but_detects_additions() -> None:
    existing = Finding("ERROR", "RSC-005", "chapter.xhtml", "Invalid element")
    introduced = Finding("ERROR", "OPF-012", "content.opf", "Missing item")
    warning = Finding("WARNING", "ACC-001", "chapter.xhtml", "Consider a title")
    original = EpubCheckReport((existing,))
    generated = EpubCheckReport((existing, introduced, warning))

    assert new_errors(original, generated) == (introduced,)


def test_new_errors_respects_repeated_occurrences() -> None:
    repeated = Finding("ERROR", "RSC-005", "chapter.xhtml", "Invalid element")

    assert new_errors(EpubCheckReport((repeated,)), EpubCheckReport((repeated, repeated))) == (
        repeated,
    )


def test_load_report_rejects_an_invalid_payload(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text('{"messages": {}}', encoding="utf-8")

    with pytest.raises(ValueError, match="lista de mensajes"):
        load_report(report_path)


@pytest.mark.epubcheck
def test_epubcheck_finds_no_new_errors_in_a_rebuilt_epub(tmp_path: Path) -> None:
    java_value = os.environ.get("EPUBCHECK_JAVA")
    jar_value = os.environ.get("EPUBCHECK_JAR")
    if not java_value or not jar_value:
        pytest.skip("EPUBCheck es una herramienta externa disponible solo en validación interna.")

    source = tmp_path / "original.epub"
    generated = tmp_path / "generated.es.epub"
    _write_epub3(source)
    translated = translate_epub(source, "es", lambda text, _progress, _token: text)
    generated.write_bytes(translated.content)

    original_report = run_epubcheck(
        source,
        java_path=Path(java_value),
        jar_path=Path(jar_value),
    )
    generated_report = run_epubcheck(
        generated,
        java_path=Path(java_value),
        jar_path=Path(jar_value),
    )

    assert new_errors(original_report, generated_report) == ()


@pytest.mark.epubcheck
def test_epubcheck_accepts_the_normalized_editor_publication(tmp_path: Path) -> None:
    java_value = os.environ.get("EPUBCHECK_JAVA")
    jar_value = os.environ.get("EPUBCHECK_JAR")
    if not java_value or not jar_value:
        pytest.skip("EPUBCheck es una herramienta externa disponible solo en validación interna.")

    def reversible(payload: bytes) -> bytes:
        return bytes(value ^ 0x39 for value in payload)

    artifacts = ArtifactStore(
        tmp_path / "artifacts",
        protect=reversible,
        unprotect=reversible,
    )
    book = create_book_from_markdown(
        "# First\n\nBody.\n\n# Second\n\nMore body.",
        (),
        EpubBookMetadata("Validated Book", "en", "Author"),
        artifacts,
        job_id="job",
    )
    book = BookEditor(book, artifacts, job_id="job").update_metadata(
        title="Validated Book",
        author="Author",
        language="en",
    )
    destination = tmp_path / "edited.epub"
    destination.write_bytes(publish_book(book, artifacts, job_id="job"))

    report = run_epubcheck(
        destination,
        java_path=Path(java_value),
        jar_path=Path(jar_value),
    )

    assert tuple(finding for finding in report.findings if finding.severity == "ERROR") == ()
