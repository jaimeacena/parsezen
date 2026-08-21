"""Fast coverage for every combination in the unified document workflow."""

from __future__ import annotations

from itertools import product
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile

import pytest

import parsezen.pipeline.transform as transform_module
import parsezen.processing as processing_module
from parsezen.document_model import ConvertedDocument
from parsezen.improvement import ImprovementMode
from parsezen.processing import (
    OutputFormat,
    ProcessRequest,
    ProcessStage,
    apply_reviewed_revision,
    process_document,
)
from parsezen.settings import AppSettings

pytestmark = pytest.mark.acceptance

_SOURCE_MARKDOWN = (
    "**PRACTICAL GUIDE**\n\n"
    "This complete paragraph contains a minor conversion error and keeps its meaning."
)
_TRANSLATED_MARKDOWN = (
    "**GUÍA PRÁCTICA**\n\n"
    "Este párrafo completo contiene un error menor de conversión y conserva su significado."
)
_OPTION_COMBINATIONS = tuple(product((False, True), repeat=3))


@pytest.mark.parametrize("output_format", (OutputFormat.MARKDOWN, OutputFormat.EPUB))
@pytest.mark.parametrize(
    ("translate", "review_content", "review_structure"),
    _OPTION_COMBINATIONS,
)
def test_all_optional_improvement_combinations_complete_without_losing_the_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_format: OutputFormat,
    translate: bool,
    review_content: bool,
    review_structure: bool,
) -> None:
    source = tmp_path / "synthetic.pdf"
    source_bytes = b"%PDF-1.4\n% deterministic workflow source\n"
    source.write_bytes(source_bytes)
    output_directory = tmp_path / "outputs"
    output_directory.mkdir()
    calls: list[ImprovementMode] = []

    monkeypatch.setattr(
        processing_module,
        "convert_document",
        lambda *_args, **_kwargs: ConvertedDocument(_SOURCE_MARKDOWN),
    )

    def improve(
        markdown: str,
        mode: ImprovementMode,
        *_args: object,
        **_kwargs: object,
    ) -> str:
        calls.append(mode)
        if mode is ImprovementMode.TRANSLATE:
            assert markdown == _SOURCE_MARKDOWN
            return _TRANSLATED_MARKDOWN
        if mode is ImprovementMode.CLEAN_AND_TRANSLATE:
            assert markdown == _SOURCE_MARKDOWN
            return _TRANSLATED_MARKDOWN.replace(
                "error menor de conversión",
                "error de conversión corregido",
            )
        if mode is ImprovementMode.REVIEW_CONTENT:
            return markdown.replace("minor conversion error", "corrected conversion error").replace(
                "error menor de conversión",
                "error de conversión corregido",
            )
        if mode is ImprovementMode.REVIEW_STRUCTURE:
            return markdown.replace("**PRACTICAL GUIDE**", "# PRACTICAL GUIDE").replace(
                "**GUÍA PRÁCTICA**",
                "# GUÍA PRÁCTICA",
            )
        raise AssertionError(f"Unexpected mode: {mode}")

    monkeypatch.setattr(transform_module, "improve_markdown", improve)
    monkeypatch.setattr(
        transform_module,
        "review_translation_markdown",
        lambda _source, current, *_args, **_kwargs: current.replace(
            "error menor de conversión",
            "error de conversión corregido",
        ),
    )
    monkeypatch.setattr(
        transform_module,
        "repair_translation_warnings",
        lambda _request, _source, translated, **_kwargs: translated,
    )
    monkeypatch.setattr(transform_module, "translation_quality_report", lambda *_args: None)

    stages: list[ProcessStage] = []
    request = ProcessRequest(
        source,
        convert_to_markdown=True,
        output_directory=output_directory,
        output_format=output_format,
        improvement_mode=ImprovementMode.TRANSLATE if translate else None,
        target_language="Español" if translate else None,
        review_content=review_content,
        review_structure=review_structure,
    )
    needs_ai = translate or review_content or review_structure
    settings = AppSettings(model="deterministic-local-model") if needs_ai else None

    result = process_document(request, settings=settings, on_stage=stages.append)

    expected_calls = [
        mode
        for enabled, mode in (
            (translate, ImprovementMode.TRANSLATE),
            (review_content and not translate, ImprovementMode.REVIEW_CONTENT),
        )
        if enabled
    ]
    if review_structure:
        expected_calls.append(ImprovementMode.REVIEW_STRUCTURE)
    assert calls == expected_calls
    assert stages[-1] is ProcessStage.COMPLETED
    assert source.read_bytes() == source_bytes
    assert result.final_path.is_file()

    expects_revision = review_structure or review_content
    if expects_revision:
        assert result.revision_draft is not None
        result = apply_reviewed_revision(result, result.revision_draft.proposed_markdown)
        assert result.revision_approved
    else:
        assert result.revision_draft is None

    if output_format is OutputFormat.EPUB:
        _assert_valid_epub_container(result.final_path)
    else:
        output = result.final_path.read_text(encoding="utf-8")
        assert "PRACTICAL GUIDE" in output or "GUÍA PRÁCTICA" in output


def _assert_valid_epub_container(path: Path) -> None:
    with ZipFile(path) as archive:
        assert archive.testzip() is None
        assert archive.namelist()[0] == "mimetype"
        assert archive.getinfo("mimetype").compress_type == ZIP_STORED
        assert archive.read("mimetype") == b"application/epub+zip"
        assert "EPUB/package.opf" in archive.namelist()
        assert "EPUB/nav.xhtml" in archive.namelist()
