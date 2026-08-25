import logging
from base64 import b64decode
from io import BytesIO
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

import parsezen.output as output_module
import parsezen.pipeline.prepare as prepare_module
import parsezen.pipeline.transform as transform_module
import parsezen.processing as processing_module
from parsezen.cancellation import CancellationToken
from parsezen.document_model import ConvertedDocument, ConvertedResource
from parsezen.domain.jobs import ReviewRecommendation, ReviewSignal
from parsezen.epub_builder import EPUB_CHAPTER_MARKER, EpubBookMetadata, build_epub
from parsezen.errors import (
    ConversionError,
    ImprovementError,
    OutputWriteError,
    ProcessingCancelledError,
    RequestValidationError,
    SettingsError,
    UnexpectedProcessingError,
)
from parsezen.glossary import GlossaryEntry, glossary_fingerprint
from parsezen.improvement import ImprovementMode
from parsezen.pdf_conversion import (
    PdfPageRange,
    PdfProgressPhase,
    PdfQualityReport,
    PdfReviewIssue,
)
from parsezen.pipeline.transform import review_scope_fingerprint
from parsezen.processing import (
    OutputFormat,
    ProcessRequest,
    ProcessResult,
    ProcessStage,
    apply_reviewed_revision,
    process_document,
    review_completed_result,
    validate_process_request,
)
from parsezen.revision import RevisionDecision
from parsezen.semantic_blocks import (
    DocumentTerm,
    SemanticBlock,
    SemanticDocument,
    SemanticRole,
    terminology_fingerprint,
)
from parsezen.settings import AppSettings
from parsezen.translation_quality import (
    LinguisticReviewMode,
    TranslationIssueKind,
    TranslationQualityReport,
)

LOCAL_SETTINGS = AppSettings(model="local-model", context_window=4_096)

_TRANSFORM_DEPENDENCIES = {
    "improve_markdown": "improve_markdown",
    "translate_markdown_offline": "translate_markdown_offline",
    "review_translation_markdown": "review_translation_markdown",
    "_repair_translation_warnings": "repair_translation_warnings",
    "_translation_quality_report": "translation_quality_report",
    "_improve_with_checkpoints": "improve_with_checkpoints",
    "_improve_selected_content": "improve_selected_content",
    "_linguistic_review_coverage": "linguistic_review_coverage",
}


def _patch_transform_dependency(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: object,
) -> None:
    if hasattr(processing_module, name):
        monkeypatch.setattr(processing_module, name, value)
    monkeypatch.setattr(transform_module, _TRANSFORM_DEPENDENCIES[name], value)


def test_linguistic_coverage_keeps_unaligned_blocks_out_of_automatic_checks() -> None:
    report = TranslationQualityReport(
        None,
        "es",
        "es",
        checked_segments=2,
        source_characters=100,
        translated_characters=120,
        total_issues=1,
        issues=(),
        source_blocks=2,
        translated_blocks=3,
    )

    coverage = transform_module.linguistic_review_coverage(
        report,
        mode=LinguisticReviewMode.NOT_REVIEWED,
    )

    assert coverage is not None
    assert coverage.translated_blocks == 3
    assert coverage.automatically_checked_blocks == 2
    assert coverage.semantically_unreviewed_blocks == 3
    assert coverage.remaining_issues == 1


def test_direct_pdf_checkpoint_cleanup_hashes_the_source_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF-local")
    hash_calls: list[Path] = []

    def digest(path: Path) -> str:
        hash_calls.append(path)
        return "0" * 64

    monkeypatch.setattr(processing_module, "sha256_file", digest)

    processing_module.clear_document_work_checkpoints(
        ProcessRequest(source, convert_to_markdown=True),
        None,
        root=tmp_path / "cache",
    )

    assert hash_calls == [source]


@pytest.mark.parametrize(
    ("identity", "message"),
    (
        ({"source_content_sha256": "invalid"}, "identidad del documento original no es válida"),
        ({"source_size_bytes": 1}, "identidad del documento original está incompleta"),
        ({"source_identity_verified": True}, "identidad verificada.*incompleta"),
    ),
)
def test_source_identity_validation_fails_closed_for_incomplete_metadata(
    tmp_path: Path,
    identity: dict[str, object],
    message: str,
) -> None:
    source = tmp_path / "book.txt"
    source.write_text("local", encoding="utf-8")

    with pytest.raises(RequestValidationError, match=message):
        validate_process_request(ProcessRequest(source, True, **identity), None)


def test_source_identity_validation_reports_an_unreadable_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.txt"
    source.write_text("local", encoding="utf-8")
    statistics = source.stat()
    monkeypatch.setattr(
        processing_module,
        "sha256_file",
        lambda _path: (_ for _ in ()).throw(OSError("blocked")),
    )
    request = ProcessRequest(
        source,
        True,
        source_size_bytes=statistics.st_size,
        source_modified_ns=statistics.st_mtime_ns,
        source_content_sha256="0" * 64,
    )

    with pytest.raises(RequestValidationError, match="No se pudo comprobar"):
        validate_process_request(request, None)


def test_targeted_review_sends_only_signalled_blocks_to_local_ai(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.md"
    source.write_text("Primer bloque.\n\nSegundo eror.\n\nTercer bloque.\n", encoding="utf-8")
    final_path = tmp_path / "result.md"
    final_path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    reviewed: list[str] = []

    def improve(markdown: str, *_args, **_kwargs) -> str:
        reviewed.append(markdown)
        return markdown.replace("eror", "error")

    _patch_transform_dependency(monkeypatch, "_improve_with_checkpoints", improve)
    result = review_completed_result(
        ProcessRequest(source, convert_to_markdown=False),
        base_result=ProcessResult(
            final_path,
            review_original_path=source,
            review_markdown=source.read_text(encoding="utf-8"),
        ),
        recommendation=ReviewRecommendation(
            ((ReviewSignal.CONVERSION_DAMAGE, 1),),
            (1,),
        ),
        settings=LOCAL_SETTINGS,
        work_checkpoint_root=tmp_path / "checkpoints",
    )

    assert reviewed == ["Segundo eror.\n\n"]
    assert result.revision_draft is not None
    assert "Segundo error." in result.revision_draft.proposed_markdown
    assert final_path.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")


def test_targeted_review_rejects_a_result_changed_after_the_recommendation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.md"
    source.write_text("Original block.\n", encoding="utf-8")
    changed = "Changed block.\n"
    final_path = tmp_path / "result.md"
    final_path.write_text(changed, encoding="utf-8")
    _patch_transform_dependency(
        monkeypatch,
        "_improve_with_checkpoints",
        lambda *_args, **_kwargs: pytest.fail("A stale scope must not reach the model."),
    )

    with pytest.raises(RequestValidationError, match="ha cambiado"):
        review_completed_result(
            ProcessRequest(source, convert_to_markdown=False),
            base_result=ProcessResult(final_path, review_markdown=changed),
            recommendation=ReviewRecommendation(
                ((ReviewSignal.CONVERSION_DAMAGE, 1),),
                (0,),
                review_scope_fingerprint("Original block.\n"),
            ),
            settings=LOCAL_SETTINGS,
            work_checkpoint_root=tmp_path / "checkpoints",
        )


@pytest.mark.parametrize("offline", [False, True])
def test_targeted_translation_review_keeps_source_alignment_for_both_routes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    offline: bool,
) -> None:
    source = tmp_path / "book.md"
    source.write_text("Source one.\n\nSource two.\n", encoding="utf-8")
    final_path = tmp_path / "translated.md"
    translated = "Destino uno.\n\nDestino dos eror.\n"
    final_path.write_text(translated, encoding="utf-8")
    reviewed: list[tuple[str, str, str | None]] = []

    def review_translation(
        source_markdown: str,
        target_markdown: str,
        _settings: AppSettings,
        request: ProcessRequest,
        *_args,
    ) -> str:
        reviewed.append(
            (
                source_markdown,
                target_markdown,
                request.offline_translation_language or request.target_language,
            )
        )
        return target_markdown.replace("eror", "error")

    monkeypatch.setattr(
        processing_module,
        "_review_translation_with_checkpoints",
        review_translation,
    )
    request = ProcessRequest(
        source,
        convert_to_markdown=False,
        improvement_mode=None if offline else ImprovementMode.TRANSLATE,
        target_language=None if offline else "Español",
        offline_translation_language="Español" if offline else None,
    )

    result = review_completed_result(
        request,
        base_result=ProcessResult(
            final_path,
            review_original_path=source,
            review_markdown=translated,
        ),
        recommendation=ReviewRecommendation(
            ((ReviewSignal.TRANSLATION_INCONSISTENCY, 1),),
            (1,),
        ),
        settings=LOCAL_SETTINGS,
        work_checkpoint_root=tmp_path / "checkpoints",
    )

    assert reviewed == [("Source two.\n", "Destino dos eror.\n", "Español")]
    assert result.revision_draft is not None
    assert "Destino dos error." in result.revision_draft.proposed_markdown
    assert final_path.read_text(encoding="utf-8") == translated


def test_epub_translation_checkpoint_key_includes_independent_content_review() -> None:
    source = Path("book.epub")
    translation = ProcessRequest(
        source,
        convert_to_markdown=False,
        output_format=OutputFormat.EPUB,
        improvement_mode=ImprovementMode.TRANSLATE,
        target_language="es",
    )
    translated_and_corrected = ProcessRequest(
        source,
        convert_to_markdown=False,
        output_format=OutputFormat.EPUB,
        improvement_mode=ImprovementMode.TRANSLATE,
        target_language="es",
        review_content=True,
    )

    assert transform_module.epub_translation_resume_key(
        translation,
        LOCAL_SETTINGS,
        "es",
    ) != transform_module.epub_translation_resume_key(
        translated_and_corrected,
        LOCAL_SETTINGS,
        "es",
    )


def test_checkpoint_key_changes_when_only_the_review_model_changes() -> None:
    request = ProcessRequest(
        Path("book.epub"),
        convert_to_markdown=False,
        output_format=OutputFormat.EPUB,
        improvement_mode=ImprovementMode.TRANSLATE,
        target_language="es",
        review_content=True,
    )
    translation_and_first_review = AppSettings(
        model="legacy:7b",
        translation_model="translator:7b",
        review_model="reviewer-a:7b",
    )
    second_review = AppSettings(
        model="legacy:7b",
        translation_model="translator:7b",
        review_model="reviewer-b:7b",
    )

    assert transform_module.epub_translation_resume_key(
        request, translation_and_first_review, "es"
    ) != transform_module.epub_translation_resume_key(request, second_review, "es")


def test_epub_checkpoint_key_keeps_the_legacy_identity_without_specialized_profiles() -> None:
    request = ProcessRequest(
        Path("book.epub"),
        convert_to_markdown=False,
        output_format=OutputFormat.EPUB,
        improvement_mode=ImprovementMode.TRANSLATE,
        target_language="es",
    )
    settings = AppSettings(model="legacy:7b", context_window=4_096)

    key = transform_module.epub_translation_resume_key(request, settings, "es")

    assert key == repr(
        (
            "epub-translation-v11",
            "es",
            "translate",
            False,
            False,
            "legacy:7b",
            4_096,
            glossary_fingerprint(()),
            terminology_fingerprint(()),
        )
    )


def test_epub_checkpoint_key_marks_specialized_identity_explicitly() -> None:
    request = ProcessRequest(
        Path("book.epub"),
        convert_to_markdown=False,
        output_format=OutputFormat.EPUB,
        improvement_mode=ImprovementMode.TRANSLATE,
        target_language="es",
    )
    settings = AppSettings(model="legacy:7b", translation_model="translator:7b")

    key = transform_module.epub_translation_resume_key(request, settings, "es")

    assert "epub-translation-v12-specialized" in key
    assert "translator:7b" in key


def test_pdf_page_checkpoints_are_shared_across_sample_and_full_ranges(
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"stable local source")
    sample = processing_module._open_pdf_conversion_checkpoints(  # noqa: SLF001
        ProcessRequest(
            source,
            convert_to_markdown=True,
            pdf_page_range=PdfPageRange(50, 50),
        ),
        root=tmp_path / "checkpoints",
    )
    full = processing_module._open_pdf_conversion_checkpoints(  # noqa: SLF001
        ProcessRequest(
            source,
            convert_to_markdown=True,
            pdf_page_range=PdfPageRange(1, 100),
        ),
        root=tmp_path / "checkpoints",
    )
    forced_ocr = processing_module._open_pdf_conversion_checkpoints(  # noqa: SLF001
        ProcessRequest(
            source,
            convert_to_markdown=True,
            pdf_page_range=PdfPageRange(1, 100),
            force_pdf_ocr=True,
        ),
        root=tmp_path / "checkpoints",
    )

    assert sample is not None and full is not None and forced_ocr is not None
    assert sample.directory == full.directory
    assert forced_ocr.directory != full.directory


def test_epub_can_open_final_personalization_without_another_transformation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.epub"
    source.write_bytes(build_epub("# Chapter\n\nBody.", (), EpubBookMetadata("Book", "en")).content)
    (tmp_path / "results").mkdir()

    result = process_document(
        ProcessRequest(
            source,
            convert_to_markdown=False,
            output_directory=tmp_path / "results",
            output_format=OutputFormat.EPUB,
        )
    )

    assert result.final_path.exists()
    assert result.final_path != source
    assert result.review_required
    assert result.revision_epub_metadata is not None
    assert result.revision_epub_metadata.title == "Book"
    assert result.revision_epub_metadata.language == "en"
    assert len(result.revision_epub_metadata.identifiers) == 1
    assert "# Chapter" in (result.review_markdown or "")


def test_each_processing_stage_is_logged_only_once(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Local notes.", encoding="utf-8")
    stages: list[ProcessStage] = []

    with caplog.at_level(logging.INFO, logger="parsezen.processing"):
        process_document(
            ProcessRequest(
                source,
                convert_to_markdown=True,
                output_format=OutputFormat.MARKDOWN,
            ),
            on_stage=stages.append,
        )

    stage_records = [
        record for record in caplog.records if record.message.startswith("processing_stage ")
    ]
    assert len(stage_records) == len(stages)
    assert [record.message.rsplit("=", 1)[-1] for record in stage_records] == [
        stage.value for stage in stages
    ]


def test_process_document_propagates_one_opaque_attempt_id_to_lifecycle_logs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Local notes.", encoding="utf-8")
    attempt_id = "c" * 32

    with caplog.at_level(logging.INFO, logger="parsezen.processing"):
        process_document(
            ProcessRequest(source, convert_to_markdown=True),
            attempt_id=attempt_id,
        )

    lifecycle = [
        record.message
        for record in caplog.records
        if record.message.startswith(
            ("processing_started", "processing_stage", "processing_completed")
        )
    ]
    assert lifecycle
    assert all(f"attempt_id={attempt_id}" in message for message in lifecycle)
    assert any(message.startswith("processing_stage") for message in lifecycle)


def test_completed_result_contains_privacy_safe_stage_telemetry(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Local notes.", encoding="utf-8")

    result = process_document(
        ProcessRequest(
            source,
            convert_to_markdown=True,
            output_format=OutputFormat.MARKDOWN,
        )
    )

    assert result.telemetry is not None
    assert result.telemetry.total_duration_ms >= 0
    assert [stage.stage for stage in result.telemetry.stages] == [
        ProcessStage.VALIDATING,
        ProcessStage.READING,
        ProcessStage.WRITING,
        ProcessStage.COMPLETED,
    ]
    assert all(stage.duration_ms >= 0 for stage in result.telemetry.stages)
    assert all(stage.visits == 1 for stage in result.telemetry.stages)


def test_reviewed_markdown_can_be_saved_even_without_an_ai_revision(tmp_path: Path) -> None:
    destination = tmp_path / "converted.md"
    destination.write_text("# Converted\n", encoding="utf-8")
    result = processing_module.ProcessResult(
        destination,
        review_markdown="<!-- PZDOC PDF PAGE 1 -->\n\n# Converted\n",
    )

    updated = apply_reviewed_revision(
        result,
        "<!-- PZDOC PDF PAGE 1 -->\n\n# Corrected by the user\n",
    )

    assert "PZDOC PDF PAGE" not in destination.read_text(encoding="utf-8")
    assert "# Corrected by the user" in destination.read_text(encoding="utf-8")
    assert updated.review_markdown is not None
    assert "PZDOC PDF PAGE" not in updated.review_markdown
    assert updated.revision_approved
    assert not updated.review_required


def test_reviewed_epub_uses_the_exact_chapter_split_chosen_by_the_user(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "book.epub"
    destination.write_bytes(b"draft")
    result = processing_module.ProcessResult(
        destination,
        review_markdown="# One\n\nFirst.\n\n# Two\n\nSecond.\n",
        revision_epub_metadata=EpubBookMetadata("Book", "en"),
        review_required=True,
    )
    reviewed = f"# One\n\nFirst.\n\n{EPUB_CHAPTER_MARKER}\n\n# Renamed two\n\nSecond.\n"

    updated = apply_reviewed_revision(result, reviewed)

    assert updated.epub_chapters == 2
    assert updated.revision_approved
    with ZipFile(destination) as archive:
        navigation = archive.read("EPUB/nav.xhtml").decode("utf-8")
    assert "Renamed two" in navigation
    assert "PZDOC EPUB CHAPTER" not in navigation


def test_unchanged_headless_epub_review_preserves_existing_package_bytes(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "book.epub"
    original = build_epub(
        "# One\n\nFirst.\n",
        (),
        EpubBookMetadata("Book", "en"),
    ).content
    destination.write_bytes(original)
    result = ProcessResult(
        destination,
        review_markdown="# One\n\nFirst.\n",
        revision_epub_metadata=EpubBookMetadata("Book", "en"),
        review_required=True,
        preserve_epub_package_on_unchanged_review=True,
    )

    updated = apply_reviewed_revision(result, result.review_markdown or "")

    assert destination.read_bytes() == original
    assert updated.revision_approved is True


def test_preflight_resolves_a_selected_pdf_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"placeholder")
    selected = PdfPageRange(80, 100)
    calls: list[tuple[Path, PdfPageRange]] = []

    def reject_range(path: Path, requested: PdfPageRange) -> PdfPageRange:
        calls.append((path, requested))
        raise ConversionError("El PDF tiene 47 páginas; el rango empieza en la página 80.")

    monkeypatch.setattr(processing_module, "resolve_pdf_page_range", reject_range)

    with pytest.raises(ConversionError, match="47 páginas"):
        validate_process_request(
            ProcessRequest(
                source,
                convert_to_markdown=True,
                pdf_page_range=selected,
            ),
            None,
        )

    assert calls == [(source, selected)]


def test_unexpected_failures_get_a_private_diagnostic_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_sentinel = "PRIVATE-DOCUMENT-CONTENT"
    source = tmp_path / "private-client-name.txt"
    source.write_text("safe", encoding="utf-8")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(private_sentinel)

    monkeypatch.setattr(processing_module, "_process_document", fail)
    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(
            UnexpectedProcessingError,
            match=r"Referencia local: [0-9a-f]{8}",
        ),
    ):
        process_document(ProcessRequest(source, convert_to_markdown=True))

    logged = caplog.text
    assert "incident=" in logged
    assert "module=parsezen.processing" in logged
    assert "function=process_document" in logged
    assert "line=" in logged
    assert private_sentinel not in logged
    assert source.name not in logged


def test_processes_txt_and_reports_real_stages(tmp_path: Path) -> None:
    source = tmp_path / "document.txt"
    source.write_text("Parsezen content", encoding="utf-8")
    stages: list[ProcessStage] = []

    result = process_document(
        ProcessRequest(source_path=source, convert_to_markdown=True),
        on_stage=stages.append,
    )

    assert result.final_path == tmp_path / "document.md"
    assert result.final_path.read_text(encoding="utf-8") == "Parsezen content"
    assert result.raw_markdown_path is None
    assert result.review_original_path is None
    assert result.final_integrity_report is not None
    assert result.final_integrity_report.verified
    assert stages == [
        ProcessStage.VALIDATING,
        ProcessStage.READING,
        ProcessStage.WRITING,
        ProcessStage.COMPLETED,
    ]


def test_chained_ai_translation_and_correction_keep_an_independent_bilingual_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "notes.md"
    source.write_text("# Title\n\nOriginal text.\n", encoding="utf-8")
    calls: list[ImprovementMode] = []
    stages: list[ProcessStage] = []

    def improve(text: str, mode: ImprovementMode, *_args: object, **_kwargs: object) -> str:
        calls.append(mode)
        assert mode is ImprovementMode.TRANSLATE
        assert text == "# Title\n\nOriginal text.\n"
        return "# Título\n\nTexto traducido y corregido.\n"

    _patch_transform_dependency(monkeypatch, "improve_markdown", improve)
    _patch_transform_dependency(
        monkeypatch,
        "review_translation_markdown",
        lambda _source, current, *_args, **_kwargs: current,
    )
    _patch_transform_dependency(
        monkeypatch,
        "_repair_translation_warnings",
        lambda _request, _source, translated, **_kwargs: translated,
    )
    _patch_transform_dependency(monkeypatch, "_translation_quality_report", lambda *_args: None)

    result = process_document(
        ProcessRequest(
            source,
            convert_to_markdown=True,
            improvement_mode=ImprovementMode.TRANSLATE,
            target_language="Español",
            review_content=True,
        ),
        settings=LOCAL_SETTINGS,
        on_stage=stages.append,
    )

    assert calls == [ImprovementMode.TRANSLATE]
    assert stages.index(ProcessStage.TRANSLATING) < stages.index(ProcessStage.REVIEWING_CONTENT)
    assert result.final_path.read_text(encoding="utf-8") == (
        "# Título\n\nTexto traducido y corregido.\n"
    )
    assert result.revision_draft is None


def test_translation_quality_is_measured_before_review_changes_block_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "notes.md"
    source.write_text("# Title\n\nOriginal text.\n", encoding="utf-8")
    translated = "# Título\n\nTexto traducido.\n"
    quality_calls: list[tuple[str | None, str]] = []

    def improve(text: str, mode: ImprovementMode, *_args: object, **_kwargs: object) -> str:
        assert mode is ImprovementMode.TRANSLATE
        return translated

    def quality_report(
        _request: ProcessRequest,
        translation_source: str | None,
        translated_markdown: str,
    ) -> None:
        quality_calls.append((translation_source, translated_markdown))

    _patch_transform_dependency(monkeypatch, "improve_markdown", improve)
    _patch_transform_dependency(
        monkeypatch,
        "review_translation_markdown",
        lambda _source, current, *_args, **_kwargs: current,
    )
    _patch_transform_dependency(
        monkeypatch,
        "_repair_translation_warnings",
        lambda _request, _source, translated_markdown, **_kwargs: translated_markdown,
    )
    _patch_transform_dependency(monkeypatch, "_translation_quality_report", quality_report)

    process_document(
        ProcessRequest(
            source,
            convert_to_markdown=True,
            improvement_mode=ImprovementMode.TRANSLATE,
            target_language="Español",
            review_content=True,
        ),
        settings=LOCAL_SETTINGS,
    )

    assert quality_calls == [("# Title\n\nOriginal text.\n", translated)]


def test_aligned_translation_corrects_only_pdf_pages_with_quality_signals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"placeholder")
    extracted = (
        "<!-- PZDOC PDF PAGE 1 -->\n\n"
        "# One\n\nFirst clean page.\n\n"
        "<!-- PZDOC PDF PAGE 2 -->\n\n"
        "# Two\n\nSecond damaged page.\n"
    )
    translated = (
        "<!-- PZDOC PDF PAGE 1 -->\n\n"
        "# Uno\n\nPrimera página correcta.\n\n"
        "<!-- PZDOC PDF PAGE 2 -->\n\n"
        "# Dos\n\nSegunda página dañada.\n"
    )
    report = PdfQualityReport(
        processed_pages=(1, 2),
        ocr_pages=(2,),
        issues=(
            PdfReviewIssue(
                2,
                "La página necesita revisión.",
                "Segunda página dañada.",
                blocking=True,
            ),
        ),
    )
    reviewed_payloads: list[str] = []
    bilingual_review_calls: list[tuple[str, str]] = []

    def convert(*_args: object, **kwargs: object) -> ConvertedDocument:
        callback = kwargs.get("on_pdf_quality_report")
        assert callable(callback)
        callback(report)
        return ConvertedDocument(extracted)

    def improve(
        text: str,
        mode: ImprovementMode,
        *_args: object,
        **_kwargs: object,
    ) -> str:
        if mode is ImprovementMode.TRANSLATE:
            return translated
        assert mode is ImprovementMode.REVIEW_CONTENT
        reviewed_payloads.append(text)
        return text.replace("dañada", "corregida")

    monkeypatch.setattr(processing_module, "convert_document", convert)
    _patch_transform_dependency(monkeypatch, "improve_markdown", improve)
    _patch_transform_dependency(
        monkeypatch,
        "review_translation_markdown",
        lambda original, current, *_args, **_kwargs: (
            bilingual_review_calls.append((original, current)) or current
        ),
    )
    _patch_transform_dependency(
        monkeypatch,
        "_repair_translation_warnings",
        lambda _request, _source, value, **_kwargs: value,
    )
    quality_report = TranslationQualityReport(None, "es", "es", 2, 100, 100, 1, ())
    _patch_transform_dependency(
        monkeypatch,
        "_translation_quality_report",
        lambda *_args: quality_report,
    )

    result = process_document(
        ProcessRequest(
            source,
            convert_to_markdown=True,
            improvement_mode=ImprovementMode.TRANSLATE,
            target_language="Español",
            review_content=True,
        ),
        settings=LOCAL_SETTINGS,
        work_checkpoint_root=tmp_path / "checkpoints",
    )

    assert reviewed_payloads
    assert all("Primera página correcta" not in payload for payload in reviewed_payloads)
    assert any("Segunda página dañada" in payload for payload in reviewed_payloads)
    assert bilingual_review_calls == [(extracted, translated)]
    assert result.revision_draft is not None
    assert "Segunda página corregida" in result.revision_draft.proposed_markdown
    assert result.linguistic_review_coverage is not None
    assert result.linguistic_review_coverage.mode is LinguisticReviewMode.INDEPENDENT_BILINGUAL
    assert result.linguistic_review_coverage.semantically_reviewed_blocks == 2
    assert result.linguistic_review_coverage.independently_verified_blocks == 2
    assert result.linguistic_review_coverage.remaining_issues == 1


def test_redundant_pdf_translation_reviews_only_pages_with_quality_signals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"placeholder")
    extracted = (
        "<!-- PZDOC PDF PAGE 1 -->\n\n"
        "# Uno\n\nPrimera página correcta.\n\n"
        "<!-- PZDOC PDF PAGE 2 -->\n\n"
        "# Dos\n\nSegunda página dañada.\n"
    )
    report = PdfQualityReport(
        processed_pages=(1, 2),
        ocr_pages=(2,),
        issues=(
            PdfReviewIssue(
                2,
                "La página necesita revisión.",
                "Segunda página dañada.",
                blocking=True,
            ),
        ),
    )
    reviewed_payloads: list[str] = []

    def convert(*_args: object, **kwargs: object) -> ConvertedDocument:
        callback = kwargs.get("on_pdf_quality_report")
        assert callable(callback)
        callback(report)
        return ConvertedDocument(extracted)

    def improve(
        text: str,
        mode: ImprovementMode,
        *_args: object,
        **_kwargs: object,
    ) -> str:
        assert mode is ImprovementMode.REVIEW_CONTENT
        reviewed_payloads.append(text)
        return text.replace("dañada", "corregida")

    monkeypatch.setattr(processing_module, "convert_document", convert)
    _patch_transform_dependency(monkeypatch, "improve_markdown", improve)
    monkeypatch.setattr(transform_module, "detect_language_code", lambda *_args: "es")

    result = process_document(
        ProcessRequest(
            source,
            convert_to_markdown=True,
            improvement_mode=ImprovementMode.TRANSLATE,
            target_language="Español",
            review_content=True,
        ),
        settings=LOCAL_SETTINGS,
        work_checkpoint_root=tmp_path / "checkpoints",
    )

    assert reviewed_payloads
    assert all("Primera página correcta" not in payload for payload in reviewed_payloads)
    assert any("Segunda página dañada" in payload for payload in reviewed_payloads)
    assert result.revision_draft is not None
    assert "Segunda página corregida" in result.revision_draft.proposed_markdown


def test_fused_translation_skips_visual_blocks_without_reviewable_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visual = SemanticBlock(
        identifier="visual",
        markdown='<div class="page-art"></div>\n\n',
        position=0,
        role=SemanticRole.BODY,
        page_number=2,
        confidence=0.7,
    )
    text = SemanticBlock(
        identifier="text",
        markdown="Texto dañado.\n",
        position=1,
        role=SemanticRole.BODY,
        page_number=2,
        confidence=0.9,
    )
    document = SemanticDocument((visual, text), ())
    report = PdfQualityReport(
        processed_pages=(2,),
        ocr_pages=(2,),
        issues=(
            PdfReviewIssue(
                2,
                "La página necesita revisión.",
                "Texto dudoso.",
                blocking=True,
            ),
        ),
    )
    reviewed_payloads: list[str] = []

    def improve(value: str, *_args: object, **_kwargs: object) -> str:
        reviewed_payloads.append(value)
        return value.replace("dañado", "corregido")

    monkeypatch.setattr(transform_module, "improve_with_checkpoints", improve)

    result = transform_module.improve_selected_content(
        visual.markdown + text.markdown,
        LOCAL_SETTINGS,
        ProcessRequest(tmp_path / "book.pdf", convert_to_markdown=True),
        None,
        None,
        None,
        semantic_document=document,
        pdf_quality_report=report,
    )

    assert reviewed_payloads == [text.markdown]
    assert result == visual.markdown + "Texto corregido.\n"


def test_fused_translation_keeps_tables_and_toc_out_of_generic_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = SemanticBlock(
        identifier="table",
        markdown="| Etiqueta | Valor |\n| --- | --- |\n| Casa | Texto |\n\n",
        position=0,
        role=SemanticRole.TABLE,
        page_number=2,
        confidence=0.9,
    )
    toc = SemanticBlock(
        identifier="toc",
        markdown="Capítulo — 12\n\n",
        position=1,
        role=SemanticRole.TOC,
        page_number=2,
        confidence=0.9,
    )
    body = SemanticBlock(
        identifier="body",
        markdown="Texto dañado.\n",
        position=2,
        role=SemanticRole.BODY,
        page_number=2,
        confidence=0.9,
    )
    document = SemanticDocument((table, toc, body), ())
    report = PdfQualityReport(
        processed_pages=(2,),
        ocr_pages=(2,),
        issues=(PdfReviewIssue(2, "Revisar.", "Texto dudoso.", blocking=False),),
    )
    reviewed_payloads: list[str] = []

    def improve(value: str, *_args: object, **_kwargs: object) -> str:
        reviewed_payloads.append(value)
        return value.replace("dañado", "corregido")

    monkeypatch.setattr(transform_module, "improve_with_checkpoints", improve)

    result = transform_module.improve_selected_content(
        table.markdown + toc.markdown + body.markdown,
        LOCAL_SETTINGS,
        ProcessRequest(tmp_path / "book.pdf", convert_to_markdown=True),
        None,
        None,
        None,
        semantic_document=document,
        pdf_quality_report=report,
    )

    assert reviewed_payloads == [body.markdown]
    assert result == table.markdown + toc.markdown + "Texto corregido.\n"


def test_selected_content_review_batches_blocks_from_the_same_problem_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocks = tuple(
        SemanticBlock(
            identifier=f"block-{index}",
            markdown=f"Texto dañado {index}.\n\n",
            position=index,
            role=SemanticRole.BODY,
            page_number=3,
            confidence=0.8,
        )
        for index in range(4)
    )
    document = SemanticDocument(blocks, ())
    report = PdfQualityReport(
        processed_pages=(3,),
        ocr_pages=(3,),
        issues=(PdfReviewIssue(3, "Revisar.", "Daño.", blocking=False),),
    )
    reviewed_payloads: list[str] = []

    def improve(value: str, *_args: object, **_kwargs: object) -> str:
        reviewed_payloads.append(value)
        return value.replace("dañado", "corregido")

    monkeypatch.setattr(transform_module, "improve_with_checkpoints", improve)

    result = transform_module.improve_selected_content(
        "".join(block.markdown for block in blocks),
        LOCAL_SETTINGS,
        ProcessRequest(tmp_path / "book.pdf", convert_to_markdown=True),
        None,
        None,
        None,
        semantic_document=document,
        pdf_quality_report=report,
    )

    assert reviewed_payloads == ["".join(block.markdown for block in blocks)]
    assert result.count("corregido") == len(blocks)


def test_offline_translation_applies_and_restores_the_glossary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Parsezen", encoding="utf-8")
    captured: list[str] = []

    def translate(text: str, *_args: object, **_kwargs: object) -> str:
        captured.append(text)
        return text

    _patch_transform_dependency(monkeypatch, "translate_markdown_offline", translate)
    _patch_transform_dependency(
        monkeypatch, "_repair_translation_warnings", lambda *args, **kwargs: args[2]
    )
    _patch_transform_dependency(monkeypatch, "_translation_quality_report", lambda *_args: None)

    result = process_document(
        ProcessRequest(
            source,
            convert_to_markdown=True,
            offline_translation_language="Español",
            glossary=(GlossaryEntry("Parsezen", "Parsezen revisado"),),
        )
    )

    assert "PZDOCGLOSSARY" in captured[0]
    assert result.final_path.read_text(encoding="utf-8") == "Parsezen revisado"


def test_offline_translation_content_review_compares_source_and_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.md"
    source_markdown = "# The History, Astrology and Magic of the Decans\n\nBy Austin Coppock.\n"
    translated = (
        "# Historia del Ajedrez; Astrología y Magia de los Decanos\n\nKor Austin Coppock.\n"
    )
    corrected = "# Historia, Astrología y Magia de los Decanos\n\nPor Austin Coppock.\n"
    source.write_text(source_markdown, encoding="utf-8")
    review_calls: list[tuple[str, str, str]] = []
    quality_calls: list[tuple[str | None, str]] = []
    quality_report = TranslationQualityReport(None, "es", "es", 2, 100, 95, 1, ())

    _patch_transform_dependency(
        monkeypatch,
        "translate_markdown_offline",
        lambda *_args, **_kwargs: translated,
    )
    _patch_transform_dependency(
        monkeypatch,
        "_repair_translation_warnings",
        lambda _request, _source, value, **_kwargs: value,
    )

    def report_quality(
        _request: ProcessRequest,
        original: str | None,
        current: str,
    ) -> TranslationQualityReport:
        quality_calls.append((original, current))
        return quality_report

    _patch_transform_dependency(monkeypatch, "_translation_quality_report", report_quality)

    def review(
        original: str,
        current: str,
        _settings: AppSettings,
        target_language: str,
        **_kwargs: object,
    ) -> str:
        review_calls.append((original, current, target_language))
        return corrected

    _patch_transform_dependency(monkeypatch, "review_translation_markdown", review)

    result = process_document(
        ProcessRequest(
            source,
            convert_to_markdown=True,
            offline_translation_language="es",
            review_content=True,
        ),
        settings=LOCAL_SETTINGS,
    )

    assert review_calls == [(source_markdown, translated, "es")]
    assert quality_calls == [
        (source_markdown, translated),
        (source_markdown, corrected),
    ]
    assert result.translation_quality_report is quality_report
    assert result.review_translation_quality_report is quality_report
    assert result.revision_draft is not None
    assert result.revision_draft.render() == corrected
    assert all(
        change.recommended_decision is RevisionDecision.ACCEPTED
        for change in result.revision_draft.changes
    )
    assert result.linguistic_review_coverage is not None
    assert result.linguistic_review_coverage.mode is LinguisticReviewMode.INDEPENDENT_BILINGUAL
    assert result.linguistic_review_coverage.automatically_checked_blocks == 2
    assert result.linguistic_review_coverage.semantically_unreviewed_blocks == 0
    assert result.linguistic_review_coverage.independently_verified_blocks == 2


def test_repeated_document_terms_are_protected_without_a_manual_glossary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "notes.md"
    source.write_text(
        "Written by Austin Coppock.\n\nLater Austin Coppock returns to it.\n",
        encoding="utf-8",
    )
    captured: list[str] = []

    def improve(text: str, *_args: object, **_kwargs: object) -> str:
        captured.append(text)
        return text

    _patch_transform_dependency(monkeypatch, "improve_markdown", improve)
    _patch_transform_dependency(
        monkeypatch,
        "_repair_translation_warnings",
        lambda _request, _source, translated, **_kwargs: translated,
    )
    _patch_transform_dependency(
        monkeypatch,
        "_translation_quality_report",
        lambda *_args: None,
    )

    result = process_document(
        ProcessRequest(
            source,
            convert_to_markdown=True,
            improvement_mode=ImprovementMode.TRANSLATE,
            target_language="Español",
        ),
        settings=LOCAL_SETTINGS,
    )

    assert captured and captured[0].count("PZDOCGLOSSARY") == 2
    assert result.final_path.read_text(encoding="utf-8").count("Austin Coppock") == 2
    assert result.terminology_terms == 1


def test_inferred_terminology_has_a_bounded_occurrence_budget() -> None:
    terminology = (
        DocumentTerm("Overused Proper Name", 65),
        DocumentTerm("Austin Coppock", 2),
    )

    assert prepare_module.combined_translation_glossary((), terminology) == (
        GlossaryEntry("Austin Coppock", "Austin Coppock"),
    )


def test_ai_translation_repair_reuses_encrypted_work_checkpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = SimpleNamespace(
        load=lambda _key: None,
        save=lambda _key, _payload: True,
    )
    callbacks: list[tuple[object, object, object]] = []
    repair_results: list[tuple[int, int]] = []

    def improve(_text: str, *_args: object, **kwargs: object) -> str:
        callbacks.append(
            (
                kwargs.get("load_checkpoint"),
                kwargs.get("save_checkpoint"),
                kwargs.get("focused_source_repair"),
            )
        )
        return "Texto reparado."

    def repair(*_args: object, **kwargs: object) -> SimpleNamespace:
        translated = kwargs["translate_segment"]("Source text.", "Current text.")
        return SimpleNamespace(translated=translated, attempted_segments=1, repaired_segments=1)

    _patch_transform_dependency(monkeypatch, "improve_markdown", improve)
    monkeypatch.setattr(transform_module, "repair_untranslated_source_text", repair)
    monkeypatch.setattr(
        transform_module,
        "restore_changed_third_language_headings",
        lambda _source, translated, **_kwargs: translated,
    )

    result = transform_module.repair_translation_warnings(
        ProcessRequest(
            Path("book.epub"),
            convert_to_markdown=False,
            improvement_mode=ImprovementMode.TRANSLATE,
            target_language="Español",
            output_format=OutputFormat.EPUB,
        ),
        "Source text.",
        "Current text.",
        settings=LOCAL_SETTINGS,
        cancellation=None,
        source_language_code="en",
        work_checkpoints=checkpoint,
        on_result=lambda attempted, accepted: repair_results.append((attempted, accepted)),
    )

    assert result == "Texto reparado."
    assert callbacks == [(checkpoint.load, checkpoint.save, True)]
    assert repair_results == [(1, 1)]


def test_translation_repair_rejects_numeric_damage_from_heading_restoration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        transform_module,
        "repair_untranslated_source_text",
        lambda *_args, **_kwargs: SimpleNamespace(
            translated="Referencia 202.\n",
            attempted_segments=1,
            repaired_segments=1,
        ),
    )
    monkeypatch.setattr(
        transform_module,
        "restore_changed_third_language_headings",
        lambda *_args, **_kwargs: "Referencia 260.\n",
    )

    result = transform_module.repair_translation_warnings(
        ProcessRequest(
            Path("book.pdf"),
            convert_to_markdown=True,
            offline_translation_language="Español",
            output_format=OutputFormat.EPUB,
        ),
        "Reference 202.\n",
        "Referencia 202.\n",
        settings=None,
        cancellation=None,
        source_language_code="en",
    )

    assert result == "Referencia 202.\n"


def test_preserved_translation_is_resolved_only_by_complete_verified_repair() -> None:
    clean_report = TranslationQualityReport(
        "es",
        "en",
        "en",
        2,
        120,
        130,
        0,
        (),
    )
    residual_report = TranslationQualityReport(
        "es",
        "en",
        "en",
        2,
        120,
        130,
        1,
        (),
        ((TranslationIssueKind.SOURCE_TEXT, 1),),
    )

    assert transform_module._preserved_translation_was_fully_repaired(
        2,
        attempted=2,
        accepted=2,
        report=clean_report,
    )
    assert not transform_module._preserved_translation_was_fully_repaired(
        2,
        attempted=2,
        accepted=1,
        report=clean_report,
    )
    assert not transform_module._preserved_translation_was_fully_repaired(
        2,
        attempted=2,
        accepted=2,
        report=residual_report,
    )


def test_avoids_overwriting_and_keeps_existing_output(tmp_path: Path) -> None:
    source = tmp_path / "document.txt"
    source.write_text("New content", encoding="utf-8")
    existing = tmp_path / "document.md"
    existing.write_text("Existing content", encoding="utf-8")

    result = process_document(ProcessRequest(source, convert_to_markdown=True))

    assert result.final_path == tmp_path / "document-2.md"
    assert existing.read_text(encoding="utf-8") == "Existing content"
    assert result.final_path.read_text(encoding="utf-8") == "New content"
    assert list(tmp_path.glob(".parsezen-*.tmp")) == []


def test_markdown_source_is_never_overwritten(tmp_path: Path) -> None:
    source = tmp_path / "document.md"
    source.write_text("# Original", encoding="utf-8")

    result = process_document(ProcessRequest(source, convert_to_markdown=True))

    assert result.final_path == tmp_path / "document-2.md"
    assert source.read_text(encoding="utf-8") == "# Original"


def test_writes_to_an_explicit_existing_directory(tmp_path: Path) -> None:
    source = tmp_path / "document.txt"
    source.write_text("Content", encoding="utf-8")
    output_directory = tmp_path / "output"
    output_directory.mkdir()

    result = process_document(ProcessRequest(source, True, output_directory))

    assert result.final_path == output_directory / "document.md"


def test_processes_a_resolved_pdf_range_and_names_the_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "long-book.pdf"
    source.write_bytes(b"placeholder")
    captured_ranges: list[PdfPageRange] = []

    monkeypatch.setattr(
        processing_module,
        "resolve_pdf_page_range",
        lambda _path, _requested: PdfPageRange(1, 7),
    )

    def convert(_path: Path, **kwargs) -> ConvertedDocument:
        captured_ranges.append(kwargs["pdf_page_range"])
        return ConvertedDocument("<!-- PZDOC PDF PAGE 1 -->\n\n# Partial book")

    monkeypatch.setattr(processing_module, "convert_document", convert)

    result = process_document(
        ProcessRequest(
            source,
            convert_to_markdown=True,
            pdf_page_range=PdfPageRange(1, 10),
        )
    )

    assert captured_ranges == [PdfPageRange(1, 7)]
    assert result.final_path == tmp_path / "long-book.pages-1-7.md"
    assert "PZDOC PDF PAGE" not in result.final_path.read_text(encoding="utf-8")
    assert "# Partial book" in result.final_path.read_text(encoding="utf-8")


def test_maps_real_pdf_progress_to_visible_processing_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"placeholder")
    stages: list[ProcessStage] = []
    progress: list[tuple[int, int]] = []

    def convert(_path: Path, **kwargs) -> ConvertedDocument:
        callback = kwargs["on_pdf_progress"]
        callback(PdfProgressPhase.EXTRACTING, 1, 2)
        callback(PdfProgressPhase.EXTRACTING, 2, 2)
        callback(PdfProgressPhase.STRUCTURING, 1, 2)
        callback(PdfProgressPhase.STRUCTURING, 2, 2)
        return ConvertedDocument("# Book")

    monkeypatch.setattr(processing_module, "convert_document", convert)

    process_document(
        ProcessRequest(source, convert_to_markdown=True),
        on_stage=stages.append,
        on_progress=lambda current, total: progress.append((current, total)),
    )

    assert stages == [
        ProcessStage.VALIDATING,
        ProcessStage.CONVERTING,
        ProcessStage.STRUCTURING,
        ProcessStage.WRITING,
        ProcessStage.COMPLETED,
    ]
    assert progress == [(1, 2), (2, 2), (1, 2), (2, 2)]


def test_pdf_can_produce_a_reflowable_epub_with_preserved_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "illustrated-book.pdf"
    source.write_bytes(b"placeholder")
    image = ConvertedResource(PurePosixPath("pdf/figure.jpg"), b"image", "image/jpeg")
    stages: list[ProcessStage] = []

    def convert(_path: Path, **kwargs) -> ConvertedDocument:
        assert kwargs["preserve_resources"] is True
        callback = kwargs["on_pdf_progress"]
        callback(PdfProgressPhase.IMAGES, 1, 1)
        return ConvertedDocument(
            "# Illustrated book\n\n![Figure](__parsezen_resources__/pdf/figure.jpg)",
            (image,),
        )

    monkeypatch.setattr(processing_module, "convert_document", convert)

    result = process_document(
        ProcessRequest(source, True, output_format=OutputFormat.EPUB),
        on_stage=stages.append,
    )

    assert result.final_path == tmp_path / "illustrated-book.epub"
    assert result.preserved_images == 1
    assert result.epub_chapters == 1
    assert ProcessStage.PRESERVING_IMAGES in stages
    assert ProcessStage.BUILDING_EPUB in stages
    with ZipFile(BytesIO(result.final_path.read_bytes())) as archive:
        assert archive.read("EPUB/images/pdf/figure.jpg") == b"image"


def test_markdown_can_produce_a_reflowable_epub(tmp_path: Path) -> None:
    source = tmp_path / "notes.md"
    source.write_text("# Notes", encoding="utf-8")

    result = process_document(
        ProcessRequest(source, True, output_format=OutputFormat.EPUB),
    )

    assert result.final_path == tmp_path / "notes.epub"
    assert result.epub_chapters == 1


def test_manual_epub_structure_review_is_kept_when_ai_changes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "notes.md"
    source.write_text("# Notes\n\nAlready well structured.", encoding="utf-8")
    _patch_transform_dependency(
        monkeypatch,
        "_improve_with_checkpoints",
        lambda markdown, *_args, **_kwargs: markdown,
    )

    result = process_document(
        ProcessRequest(
            source,
            True,
            output_format=OutputFormat.EPUB,
            review_structure=True,
        ),
        settings=LOCAL_SETTINGS,
    )

    assert result.revision_draft is None
    assert result.review_required
    assert result.review_markdown == "# Notes\n\nAlready well structured."
    assert result.revision_epub_metadata is not None


def test_generated_epub_accepts_local_title_author_and_cover(tmp_path: Path) -> None:
    source = tmp_path / "notes.md"
    source.write_text("# Notes", encoding="utf-8")
    cover = tmp_path / "front.jpg"
    cover.write_bytes(b"local-cover")

    result = process_document(
        ProcessRequest(
            source,
            True,
            output_format=OutputFormat.EPUB,
            epub_title="Mi libro",
            epub_author="Autora local",
            epub_cover_path=cover,
        ),
    )

    with ZipFile(result.final_path) as archive:
        package = archive.read("EPUB/package.opf").decode("utf-8")
        assert "<dc:title>Mi libro</dc:title>" in package
        assert "<dc:creator>Autora local</dc:creator>" in package
        assert 'properties="cover-image"' in package
        assert archive.read("EPUB/images/cover/cover.jpg") == b"local-cover"


def test_epub_metadata_is_rejected_for_a_markdown_result(tmp_path: Path) -> None:
    source = tmp_path / "notes.md"
    source.write_text("# Notes", encoding="utf-8")

    with pytest.raises(RequestValidationError, match="resultado es EPUB"):
        process_document(
            ProcessRequest(source, True, epub_title="No corresponde"),
        )


def test_markdown_epub_packages_its_local_images(tmp_path: Path) -> None:
    image = tmp_path / "cover.png"
    image.write_bytes(
        b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
    )
    source = tmp_path / "notes.md"
    source.write_text("# Notes\n\n![Cover](cover.png)\n", encoding="utf-8")

    result = process_document(
        ProcessRequest(source, True, output_format=OutputFormat.EPUB),
    )

    assert result.preserved_images == 1
    with ZipFile(result.final_path) as archive:
        assert archive.read("EPUB/images/markdown/image-001.png") == image.read_bytes()


def test_markdown_output_keeps_remote_image_links_without_packaging_them(
    tmp_path: Path,
) -> None:
    source = tmp_path / "notes.md"
    original = "# Notes\n\n![Remote](https://example.com/cover.png)\n"
    source.write_text(original, encoding="utf-8")

    result = process_document(ProcessRequest(source, True))

    assert result.final_path.read_text(encoding="utf-8") == original
    assert result.preserved_images == 0


def test_markdown_can_exclude_images_without_writing_asset_files(tmp_path: Path) -> None:
    source_directory = tmp_path / "source"
    output_directory = tmp_path / "output"
    source_directory.mkdir()
    output_directory.mkdir()
    image = source_directory / "figure.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\ncontent")
    source = source_directory / "notes.md"
    source.write_text("# Notes\n\n![Useful description](figure.png)\n", encoding="utf-8")

    result = process_document(
        ProcessRequest(
            source,
            True,
            output_directory=output_directory,
            include_images=False,
        )
    )

    assert result.preserved_images == 0
    assert "![" not in result.final_path.read_text(encoding="utf-8")
    assert "Useful description" in result.final_path.read_text(encoding="utf-8")
    assert not list(output_directory.rglob("*.png"))


def test_epub_can_exclude_body_images_and_cover(tmp_path: Path) -> None:
    image = tmp_path / "figure.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\ncontent")
    source = tmp_path / "notes.md"
    source.write_text("# Notes\n\n![Figure](figure.png)\n", encoding="utf-8")

    result = process_document(
        ProcessRequest(
            source,
            True,
            output_format=OutputFormat.EPUB,
            include_images=False,
        )
    )

    assert result.preserved_images == 0
    with ZipFile(result.final_path) as archive:
        assert not any(name.startswith("EPUB/images/") for name in archive.namelist())
        assert "<img" not in archive.read("EPUB/text/chapter-0001.xhtml").decode("utf-8")


def test_pdf_first_selected_page_can_become_the_epub_cover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"placeholder")
    selected = PdfPageRange(7, 12)
    rendered_pages: list[int] = []
    monkeypatch.setattr(
        processing_module,
        "resolve_pdf_page_range",
        lambda _path, requested: requested,
    )
    monkeypatch.setattr(
        processing_module,
        "convert_document",
        lambda *_args, **_kwargs: ConvertedDocument(
            "# Book\n\n"
            "![](<__parsezen_resources__/pdf/page-0007-image-01.jpg>)\n\n"
            "Content\n\n"
            "![](<__parsezen_resources__/pdf/page-0008-image-01.jpg>)\n",
            (
                ConvertedResource(
                    PurePosixPath("pdf/page-0007-image-01.jpg"),
                    b"duplicated-cover",
                    "image/jpeg",
                ),
                ConvertedResource(
                    PurePosixPath("pdf/page-0008-image-01.jpg"),
                    b"next-page-image",
                    "image/jpeg",
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        processing_module,
        "render_pdf_page_cover",
        lambda _path, page: rendered_pages.append(page) or b"jpeg-cover",
    )

    result = process_document(
        ProcessRequest(
            source,
            True,
            pdf_page_range=selected,
            output_format=OutputFormat.EPUB,
            epub_first_page_cover=True,
        )
    )

    assert rendered_pages == [7]
    with ZipFile(result.final_path) as archive:
        package = archive.read("EPUB/package.opf").decode("utf-8")
        assert 'properties="cover-image"' in package
        assert archive.read("EPUB/images/cover/first-page.jpg") == b"jpeg-cover"
        assert "first-page.jpg" in archive.read("EPUB/text/cover.xhtml").decode("utf-8")
        assert "EPUB/images/pdf/page-0007-image-01.jpg" not in archive.namelist()
        assert archive.read("EPUB/images/pdf/page-0008-image-01.jpg") == b"next-page-image"
        chapters = "".join(
            archive.read(name).decode("utf-8")
            for name in archive.namelist()
            if name.startswith("EPUB/text/chapter-")
        )
        assert "page-0007-image-01.jpg" not in chapters
        assert "page-0008-image-01.jpg" in chapters


def test_pdf_first_page_cover_remains_removed_after_applying_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"placeholder")
    selected = PdfPageRange(7, 12)
    monkeypatch.setattr(
        processing_module,
        "resolve_pdf_page_range",
        lambda _path, requested: requested,
    )
    monkeypatch.setattr(
        processing_module,
        "convert_document",
        lambda *_args, **_kwargs: ConvertedDocument(
            "# Book\n\n"
            "![](<__parsezen_resources__/pdf/page-0007-image-01.jpg>)\n\n"
            "Content\n\n"
            "![](<__parsezen_resources__/pdf/page-0008-image-01.jpg>)\n",
            (
                ConvertedResource(
                    PurePosixPath("pdf/page-0007-image-01.jpg"),
                    b"duplicated-cover",
                    "image/jpeg",
                ),
                ConvertedResource(
                    PurePosixPath("pdf/page-0008-image-01.jpg"),
                    b"next-page-image",
                    "image/jpeg",
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        processing_module,
        "render_pdf_page_cover",
        lambda _path, _page: b"jpeg-cover",
    )
    _patch_transform_dependency(
        monkeypatch,
        "_improve_with_checkpoints",
        lambda markdown, *_args, **_kwargs: markdown.replace("Content", "Reviewed content"),
    )

    result = process_document(
        ProcessRequest(
            source,
            True,
            pdf_page_range=selected,
            output_format=OutputFormat.EPUB,
            epub_first_page_cover=True,
            review_content=True,
        ),
        settings=LOCAL_SETTINGS,
    )

    assert result.revision_draft is not None
    assert "page-0007-image-01.jpg" not in result.revision_draft.original_markdown
    assert "page-0007-image-01.jpg" not in result.revision_draft.proposed_markdown
    updated = apply_reviewed_revision(result, result.revision_draft.render())

    with ZipFile(updated.final_path) as archive:
        assert "EPUB/images/pdf/page-0007-image-01.jpg" not in archive.namelist()
        assert archive.read("EPUB/images/pdf/page-0008-image-01.jpg") == b"next-page-image"
        chapters = "".join(
            archive.read(name).decode("utf-8")
            for name in archive.namelist()
            if name.startswith("EPUB/text/chapter-")
        )
    assert "page-0007-image-01.jpg" not in chapters
    assert "page-0008-image-01.jpg" in chapters
    assert "Reviewed content" in chapters


def test_epub_cover_is_rejected_when_images_are_disabled(tmp_path: Path) -> None:
    source = tmp_path / "notes.md"
    source.write_text("# Notes", encoding="utf-8")
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"png")

    with pytest.raises(RequestValidationError, match="Incluir imágenes"):
        process_document(
            ProcessRequest(
                source,
                True,
                output_format=OutputFormat.EPUB,
                include_images=False,
                epub_cover_path=cover,
            )
        )


def test_first_page_cover_is_rejected_for_unpaginated_input(tmp_path: Path) -> None:
    source = tmp_path / "notes.md"
    source.write_text("# Notes", encoding="utf-8")

    with pytest.raises(RequestValidationError, match="solo puede usarse.*PDF"):
        process_document(
            ProcessRequest(
                source,
                True,
                output_format=OutputFormat.EPUB,
                epub_first_page_cover=True,
            )
        )


def test_image_location_requires_image_inclusion(tmp_path: Path) -> None:
    source = tmp_path / "notes.md"
    source.write_text("# Notes", encoding="utf-8")

    with pytest.raises(RequestValidationError, match="Activa Incluir imágenes"):
        process_document(
            ProcessRequest(
                source,
                True,
                image_output_directory=tmp_path,
                include_images=False,
            )
        )


def test_markdown_epub_rejects_an_image_that_cannot_be_packaged(tmp_path: Path) -> None:
    source = tmp_path / "notes.md"
    source.write_text("![Remote](https://example.com/cover.png)", encoding="utf-8")

    with pytest.raises(ConversionError, match="archivos locales relativos"):
        process_document(ProcessRequest(source, True, output_format=OutputFormat.EPUB))

    assert not (tmp_path / "notes.epub").exists()


@pytest.mark.parametrize("extension", [".txt", ".docx"])
def test_other_textual_inputs_can_produce_epub(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extension: str,
) -> None:
    source = tmp_path / f"book{extension}"
    source.write_bytes(b"placeholder")
    monkeypatch.setattr(
        processing_module,
        "convert_document",
        lambda *_args, **_kwargs: ConvertedDocument("# Book\n\nContent"),
    )

    result = process_document(
        ProcessRequest(source, True, output_format=OutputFormat.EPUB),
    )

    assert result.final_path == tmp_path / "book.epub"
    assert result.epub_chapters == 1


def test_generated_markdown_can_link_images_in_a_stable_selected_folder(
    tmp_path: Path,
) -> None:
    source_directory = tmp_path / "vault"
    output_directory = source_directory / "notes"
    image_output_directory = source_directory / "attachments"
    output_directory.mkdir(parents=True)
    image_output_directory.mkdir()
    image = source_directory / "figure.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\ncontent")
    source = source_directory / "book.md"
    source.write_text("# Book\n\n![Figure](figure.png)\n", encoding="utf-8")

    result = process_document(
        ProcessRequest(
            source,
            True,
            output_directory=output_directory,
            image_output_directory=image_output_directory,
        )
    )

    assert result.final_path == output_directory / "book.md"
    assert result.preserved_images == 1
    assert "../attachments/book.assets/markdown/image-001.png" in result.final_path.read_text(
        encoding="utf-8"
    )
    assert (
        image_output_directory / "book.assets" / "markdown" / "image-001.png"
    ).read_bytes() == image.read_bytes()


def test_epub_input_cannot_be_rebuilt_as_a_new_generic_epub(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    source.write_bytes(b"placeholder")

    with pytest.raises(RequestValidationError, match="activa Traducir"):
        process_document(
            ProcessRequest(source, True, output_format=OutputFormat.EPUB),
        )


def test_pdf_translation_is_applied_before_building_the_epub(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"placeholder")
    monkeypatch.setattr(
        processing_module,
        "convert_document",
        lambda *_args, **_kwargs: ConvertedDocument("# Original\n\nEnglish content."),
    )
    _patch_transform_dependency(
        monkeypatch,
        "translate_markdown_offline",
        lambda *_args, **_kwargs: "# Traducido\n\nContenido en español.",
    )
    _patch_transform_dependency(
        monkeypatch,
        "_repair_translation_warnings",
        lambda _request, _source, translated, **_kwargs: translated,
    )
    _patch_transform_dependency(monkeypatch, "_translation_quality_report", lambda *_args: None)

    result = process_document(
        ProcessRequest(
            source,
            True,
            offline_translation_language="Español",
            output_format=OutputFormat.EPUB,
        )
    )

    assert result.final_path == tmp_path / "book.es.epub"
    with ZipFile(result.final_path) as archive:
        chapter = archive.read("EPUB/text/chapter-0001.xhtml").decode("utf-8")
    assert "Traducido" in chapter
    assert "Contenido en español" in chapter


def test_completed_direct_pdf_opens_only_reusable_pdf_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"placeholder")
    opened: list[tuple[str, SimpleNamespace]] = []

    def open_checkpoints(_source: Path, resume_key: str, **_kwargs):
        checkpoint = SimpleNamespace(
            load=lambda _key: None,
            save=lambda _key, _value: True,
            clear_calls=0,
        )

        def clear() -> None:
            checkpoint.clear_calls += 1

        checkpoint.clear = clear
        opened.append((resume_key, checkpoint))
        return checkpoint

    monkeypatch.setattr(processing_module, "open_work_checkpoints", open_checkpoints)
    monkeypatch.setattr(
        processing_module,
        "convert_document",
        lambda *_args, **_kwargs: ConvertedDocument("# Book"),
    )

    process_document(ProcessRequest(source, True))

    pdf = next(item for key, item in opened if "pdf-conversion" in key)
    assert all("general-work" not in key for key, _item in opened)
    assert pdf.clear_calls == 0


def test_zero_checkpoint_retention_clears_completed_pdf_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"placeholder")
    opened: list[tuple[str, SimpleNamespace]] = []

    def open_checkpoints(_source: Path, resume_key: str, **_kwargs):
        checkpoint = SimpleNamespace(
            load=lambda _key: None,
            save=lambda _key, _value: True,
            clear_calls=0,
        )

        def clear() -> None:
            checkpoint.clear_calls += 1

        checkpoint.clear = clear
        opened.append((resume_key, checkpoint))
        return checkpoint

    monkeypatch.setattr(processing_module, "open_work_checkpoints", open_checkpoints)
    monkeypatch.setattr(
        processing_module,
        "convert_document",
        lambda *_args, **_kwargs: ConvertedDocument("# Book"),
    )

    process_document(
        ProcessRequest(source, True),
        settings=AppSettings(checkpoint_retention_days=0),
    )

    pdf = next(item for key, item in opened if "pdf-conversion" in key)
    assert all("general-work" not in key for key, _item in opened)
    assert pdf.clear_calls == 1


def test_general_chunk_checkpoints_are_shared_across_glossary_variants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"placeholder")
    keys: list[str] = []

    def capture_key(_source: Path, resume_key: str, **_kwargs):
        keys.append(resume_key)
        return SimpleNamespace()

    monkeypatch.setattr(processing_module, "open_work_checkpoints", capture_key)
    for glossary in (
        (GlossaryEntry("First term", "Primer término"),),
        (GlossaryEntry("Second term", "Segundo término"),),
    ):
        processing_module._open_general_work_checkpoints(
            ProcessRequest(
                source,
                convert_to_markdown=True,
                improvement_mode=ImprovementMode.TRANSLATE,
                target_language="Español",
                glossary=glossary,
            ),
            LOCAL_SETTINGS,
            root=tmp_path / "checkpoints",
        )

    assert keys[0] == keys[1]
    assert "general-work-v3" in keys[0]


def test_general_checkpoint_key_keeps_the_legacy_identity_without_specialized_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"placeholder")
    keys: list[str] = []

    monkeypatch.setattr(
        processing_module,
        "open_work_checkpoints",
        lambda _source, resume_key, **_kwargs: keys.append(resume_key) or SimpleNamespace(),
    )

    request = ProcessRequest(source, convert_to_markdown=True)
    settings = AppSettings(model="legacy:7b", context_window=4_096)
    processing_module._open_general_work_checkpoints(
        request,
        settings,
        root=tmp_path / "checkpoints",
    )

    assert keys == []

    request = ProcessRequest(
        source,
        convert_to_markdown=True,
        improvement_mode=ImprovementMode.TRANSLATE,
        target_language="Español",
    )
    processing_module._open_general_work_checkpoints(
        request,
        settings,
        root=tmp_path / "checkpoints",
    )

    assert keys == [
        repr(
            (
                "general-work-v3",
                True,
                "translate",
                False,
                False,
                "Español",
                None,
                None,
                False,
                "markdown",
                "legacy:7b",
                4_096,
            )
        )
    ]


def test_general_checkpoint_key_marks_specialized_identity_explicitly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"placeholder")
    keys: list[str] = []
    monkeypatch.setattr(
        processing_module,
        "open_work_checkpoints",
        lambda _source, resume_key, **_kwargs: keys.append(resume_key) or SimpleNamespace(),
    )

    processing_module._open_general_work_checkpoints(
        ProcessRequest(
            source,
            convert_to_markdown=True,
            improvement_mode=ImprovementMode.TRANSLATE,
            target_language="Español",
        ),
        AppSettings(
            model="legacy:7b",
            context_window=4_096,
            translation_model="translator:7b",
            review_model="reviewer:7b",
        ),
        root=tmp_path / "checkpoints",
    )

    assert len(keys) == 1
    assert "general-work-v4-specialized" in keys[0]
    assert "translator:7b" in keys[0]
    assert "reviewer:7b" in keys[0]


def test_partial_pdf_improvement_uses_the_range_for_both_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "long-book.pdf"
    source.write_bytes(b"placeholder")
    monkeypatch.setattr(
        processing_module,
        "resolve_pdf_page_range",
        lambda _path, _requested: PdfPageRange(20, 30),
    )
    monkeypatch.setattr(
        processing_module,
        "convert_document",
        lambda _path, **_kwargs: ConvertedDocument("Raw 10"),
    )
    _patch_transform_dependency(
        monkeypatch,
        "improve_markdown",
        lambda *_args, **_kwargs: "Mended 10",
    )

    result = process_document(
        ProcessRequest(
            source,
            convert_to_markdown=True,
            improvement_mode=ImprovementMode.CLEAN,
            pdf_page_range=PdfPageRange(20, 30),
        ),
        settings=LOCAL_SETTINGS,
    )

    assert result.final_path == tmp_path / "long-book.pages-20-30.mended.md"
    assert result.raw_markdown_path == tmp_path / "long-book.pages-20-30.raw.md"


def test_rejects_page_selection_for_non_pdf_inputs(tmp_path: Path) -> None:
    source = tmp_path / "notes.md"
    source.write_text("# Notes", encoding="utf-8")

    with pytest.raises(RequestValidationError, match="solo se puede usar con PDF"):
        process_document(
            ProcessRequest(
                source,
                convert_to_markdown=True,
                pdf_page_range=PdfPageRange(1, 10),
            )
        )


def test_rejects_forced_pdf_ocr_for_a_non_pdf_input(tmp_path: Path) -> None:
    source = tmp_path / "notes.md"
    source.write_text("# Notes", encoding="utf-8")

    with pytest.raises(RequestValidationError, match="solo se puede usar al convertir un PDF"):
        process_document(
            ProcessRequest(
                source,
                convert_to_markdown=True,
                force_pdf_ocr=True,
            )
        )


def test_rejects_an_invalid_forced_ocr_flag(tmp_path: Path) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"placeholder")

    with pytest.raises(RequestValidationError, match="OCR exhaustivo no es válida"):
        process_document(
            ProcessRequest(
                source,
                convert_to_markdown=True,
                force_pdf_ocr="yes",  # type: ignore[arg-type]
            )
        )


@pytest.mark.parametrize(
    ("improvement_mode", "offline_language", "settings"),
    [
        (ImprovementMode.TRANSLATE, None, LOCAL_SETTINGS),
        (None, "Klingon", None),
    ],
)
def test_rejects_an_unsupported_translation_language_during_preflight(
    tmp_path: Path,
    improvement_mode: ImprovementMode | None,
    offline_language: str | None,
    settings: AppSettings | None,
) -> None:
    source = tmp_path / "document.txt"
    source.write_text("Local content.", encoding="utf-8")

    with pytest.raises(RequestValidationError, match="idioma de destino no est\u00e1 soportado"):
        process_document(
            ProcessRequest(
                source,
                convert_to_markdown=True,
                improvement_mode=improvement_mode,
                target_language="Klingon" if improvement_mode is not None else None,
                offline_translation_language=offline_language,
            ),
            settings=settings,
        )


@pytest.mark.parametrize("filename", ["document.rtf", "document", "document.exe"])
def test_rejects_unsupported_inputs(tmp_path: Path, filename: str) -> None:
    source = tmp_path / filename
    source.write_text("Unsupported", encoding="utf-8")

    with pytest.raises(RequestValidationError, match="no está soportado"):
        process_document(ProcessRequest(source, convert_to_markdown=True))


def test_rejects_a_missing_output_directory(tmp_path: Path) -> None:
    source = tmp_path / "document.txt"
    source.write_text("Content", encoding="utf-8")

    with pytest.raises(RequestValidationError, match="carpeta de salida no existe"):
        process_document(ProcessRequest(source, True, tmp_path / "missing"))


def test_rejects_pdf_processing_when_the_temporary_drive_is_too_full(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "document.pdf"
    source.write_bytes(b"placeholder")
    temporary_directory = tmp_path / "system-temp"
    temporary_directory.mkdir()

    class DiskUsage:
        def __init__(self, free: int) -> None:
            self.free = free

    monkeypatch.setattr(
        processing_module,
        "gettempdir",
        lambda: str(temporary_directory),
    )
    monkeypatch.setattr(
        processing_module.shutil,
        "disk_usage",
        lambda path: DiskUsage(1 if Path(path) == temporary_directory else 1024**4),
    )

    with pytest.raises(RequestValidationError, match="espacio temporal suficiente"):
        process_document(ProcessRequest(source, convert_to_markdown=True))


def test_rejects_an_output_directory_that_cannot_be_written_before_conversion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "document.txt"
    source.write_text("Content", encoding="utf-8")

    def fail_probe(**_kwargs):
        raise OSError("read only")

    monkeypatch.setattr(processing_module, "mkstemp", fail_probe)

    with pytest.raises(RequestValidationError, match="No se puede escribir"):
        process_document(ProcessRequest(source, convert_to_markdown=True))


def test_rejects_a_request_without_conversion(tmp_path: Path) -> None:
    source = tmp_path / "document.md"
    source.write_text("# Content", encoding="utf-8")

    with pytest.raises(RequestValidationError, match="conversión o la mejora"):
        process_document(ProcessRequest(source, convert_to_markdown=False))


def test_rejects_an_invalid_improvement_mode_as_a_validation_error(tmp_path: Path) -> None:
    source = tmp_path / "document.md"
    source.write_text("# Content", encoding="utf-8")

    with pytest.raises(RequestValidationError, match="modo de mejora"):
        process_document(
            ProcessRequest(
                source,
                convert_to_markdown=False,
                improvement_mode="invalid",  # type: ignore[arg-type]
            ),
            settings=LOCAL_SETTINGS,
        )


def test_improves_markdown_without_creating_a_redundant_raw_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "document.md"
    source.write_text("# Original 10", encoding="utf-8")
    calls: list[tuple[ImprovementMode, str | None]] = []

    def improve(
        markdown: str,
        mode: ImprovementMode,
        _settings: AppSettings,
        target_language: str | None,
        *,
        on_progress=None,
    ) -> str:
        assert on_progress is None
        calls.append((mode, target_language))
        return markdown.replace("Original", "Clean")

    _patch_transform_dependency(monkeypatch, "improve_markdown", improve)

    result = process_document(
        ProcessRequest(
            source,
            convert_to_markdown=False,
            improvement_mode=ImprovementMode.CLEAN,
        ),
        settings=LOCAL_SETTINGS,
    )

    assert calls == [(ImprovementMode.CLEAN, None)]
    assert result.final_path == tmp_path / "document.mended.md"
    assert result.final_path.read_text(encoding="utf-8") == "# Clean 10"
    assert result.raw_markdown_path is None
    assert result.review_original_path == source
    assert source.read_text(encoding="utf-8") == "# Original 10"


@pytest.mark.parametrize("extension", [".docx", ".pdf"])
def test_combined_converted_document_improvement_keeps_paired_raw_and_reports_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extension: str,
) -> None:
    source = tmp_path / f"document{extension}"
    source.write_bytes(b"conversion is replaced in this orchestration test")
    stages: list[ProcessStage] = []
    calls: list[tuple[str, ImprovementMode, str | None]] = []

    monkeypatch.setattr(
        processing_module,
        "convert_document",
        lambda _path, **_kwargs: ConvertedDocument("# Raw 10"),
    )

    def improve(
        markdown: str,
        mode: ImprovementMode,
        _settings: AppSettings,
        target_language: str | None,
        *,
        on_progress=None,
    ) -> str:
        assert on_progress is None
        calls.append((markdown, mode, target_language))
        return "# Mejorado 10"

    _patch_transform_dependency(monkeypatch, "improve_markdown", improve)

    request = ProcessRequest(
        source,
        convert_to_markdown=True,
        improvement_mode=ImprovementMode.CLEAN_AND_TRANSLATE,
        target_language="Español",
    )
    result = process_document(request, on_stage=stages.append, settings=LOCAL_SETTINGS)

    assert calls == [("# Raw 10", ImprovementMode.CLEAN_AND_TRANSLATE, "Español")]
    assert stages == [
        ProcessStage.VALIDATING,
        ProcessStage.CONVERTING,
        ProcessStage.TRANSLATING,
        ProcessStage.WRITING,
        ProcessStage.COMPLETED,
    ]
    assert result.final_path == tmp_path / "document.mended.md"
    assert result.raw_markdown_path == tmp_path / "document.raw.md"
    assert result.review_original_path == result.raw_markdown_path
    assert result.final_path.read_text(encoding="utf-8") == "# Mejorado 10"
    assert result.raw_markdown_path.read_text(encoding="utf-8") == "# Raw 10"


def test_translates_markdown_offline_without_requiring_a_local_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "document.md"
    source.write_text("# Original 10", encoding="utf-8")
    stages: list[ProcessStage] = []
    calls: list[tuple[str, str]] = []

    def translate(
        markdown: str,
        language: str,
        *,
        on_progress=None,
        on_engine_ready=None,
    ) -> str:
        assert on_progress is None
        assert on_engine_ready is not None
        on_engine_ready()
        calls.append((markdown, language))
        return "# Traducido 10"

    _patch_transform_dependency(monkeypatch, "translate_markdown_offline", translate)

    result = process_document(
        ProcessRequest(
            source,
            convert_to_markdown=False,
            offline_translation_language="Español",
        ),
        on_stage=stages.append,
    )

    assert calls == [("# Original 10", "Español")]
    assert stages == [
        ProcessStage.VALIDATING,
        ProcessStage.READING,
        ProcessStage.PREPARING_TRANSLATION,
        ProcessStage.TRANSLATING,
        ProcessStage.WRITING,
        ProcessStage.COMPLETED,
    ]
    assert result.final_path == tmp_path / "document.mended.md"
    assert result.review_original_path == source
    assert result.final_path.read_text(encoding="utf-8") == "# Traducido 10"
    assert result.translation_quality_report is not None
    assert result.translation_quality_report.target_language == "es"
    assert result.linguistic_review_coverage is not None
    assert result.linguistic_review_coverage.mode is LinguisticReviewMode.NOT_REVIEWED
    assert (
        result.linguistic_review_coverage.semantically_unreviewed_blocks
        == result.translation_quality_report.checked_segments
    )


def test_ai_translation_is_skipped_when_the_text_is_already_in_the_target_language(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "document.md"
    original = (
        "# Capítulo\n\n"
        "Este documento ya está escrito completamente en español y no necesita "
        "una traducción adicional para conservar su contenido."
    )
    source.write_text(original, encoding="utf-8")
    stages: list[ProcessStage] = []

    monkeypatch.setattr(processing_module, "detect_language_code", lambda *_args, **_kwargs: "es")
    _patch_transform_dependency(
        monkeypatch,
        "improve_markdown",
        lambda *_args, **_kwargs: pytest.fail("No debe invocarse la traducción redundante."),
    )

    result = process_document(
        ProcessRequest(
            source,
            convert_to_markdown=False,
            improvement_mode=ImprovementMode.TRANSLATE,
            target_language="Español",
        ),
        on_stage=stages.append,
        settings=LOCAL_SETTINGS,
    )

    assert ProcessStage.PREPARING_TRANSLATION not in stages
    assert ProcessStage.TRANSLATING not in stages
    assert result.final_path.read_text(encoding="utf-8") == original
    assert result.translation_quality_report is None


def test_offline_translation_is_skipped_when_the_text_is_already_in_target_language(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "document.md"
    original = (
        "# Capítulo\n\n"
        "Este documento ya está escrito completamente en español y no necesita "
        "una traducción adicional para conservar su contenido."
    )
    source.write_text(original, encoding="utf-8")
    stages: list[ProcessStage] = []

    monkeypatch.setattr(processing_module, "detect_language_code", lambda *_args, **_kwargs: "es")
    _patch_transform_dependency(
        monkeypatch,
        "translate_markdown_offline",
        lambda *_args, **_kwargs: pytest.fail("No debe invocarse la traducción redundante."),
    )

    result = process_document(
        ProcessRequest(
            source,
            convert_to_markdown=False,
            offline_translation_language="Español",
        ),
        on_stage=stages.append,
    )

    assert ProcessStage.PREPARING_TRANSLATION not in stages
    assert ProcessStage.TRANSLATING not in stages
    assert result.final_path.read_text(encoding="utf-8") == original
    assert result.translation_quality_report is None


def test_direct_epub_translation_is_skipped_when_declared_language_matches_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.epub"
    source.write_bytes(
        build_epub(
            "# Capítulo\n\nContenido ya escrito en español.",
            (),
            EpubBookMetadata("Libro", "es", "Autora"),
        ).content
    )
    output_directory = tmp_path / "results"
    output_directory.mkdir()
    stages: list[ProcessStage] = []

    monkeypatch.setattr(
        processing_module,
        "translate_epub",
        lambda *_args, **_kwargs: pytest.fail("No debe invocarse la traducción redundante."),
    )

    result = process_document(
        ProcessRequest(
            source,
            convert_to_markdown=False,
            output_directory=output_directory,
            offline_translation_language="Español",
            output_format=OutputFormat.EPUB,
        ),
        on_stage=stages.append,
    )

    assert ProcessStage.PREPARING_TRANSLATION not in stages
    assert ProcessStage.TRANSLATING not in stages
    assert result.review_required
    assert result.translation_quality_report is None
    assert result.revision_epub_metadata is not None
    assert result.revision_epub_metadata.title == "Libro"
    assert result.revision_epub_metadata.language == "es"
    assert result.revision_epub_metadata.author == "Autora"
    assert len(result.revision_epub_metadata.identifiers) == 1


def test_direct_epub_translation_does_not_trust_incorrect_declared_language(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.epub"
    source.write_bytes(
        build_epub(
            "# Chapter\n\n"
            "This complete English paragraph contains enough natural language to detect that "
            "the publication metadata is incorrect and translation is still required.",
            (),
            EpubBookMetadata("Book", "es", "Author"),
        ).content
    )
    translated = SimpleNamespace(
        content=source.read_bytes(),
        language_code="es",
        translation_parts=1,
        resumed_parts=0,
        checkpoint_degraded=False,
        quality_report=None,
    )
    calls: list[bool] = []

    def translate(*_args, **_kwargs):
        calls.append(True)
        return translated

    monkeypatch.setattr(processing_module, "translate_epub", translate)

    result = process_document(
        ProcessRequest(
            source,
            convert_to_markdown=False,
            offline_translation_language="es",
            output_format=OutputFormat.EPUB,
        )
    )

    assert calls == [True]
    assert result.epub_translation_parts == 1


def test_direct_epub_translation_propagates_checkpoint_degradation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.epub"
    source.write_bytes(b"synthetic epub handled by orchestration doubles")
    output = tmp_path / "book.es.epub"
    cleared: list[bool] = []
    checkpoints = SimpleNamespace(
        load=lambda _key: None,
        save=lambda _key, _payload: False,
        clear=lambda: cleared.append(True),
    )
    translated = SimpleNamespace(
        content=b"complete epub",
        language_code="es",
        translation_parts=2,
        resumed_parts=0,
        checkpoint_degraded=True,
        quality_report=None,
    )

    monkeypatch.setattr(
        processing_module,
        "open_epub_translation_checkpoints",
        lambda *_args, **_kwargs: checkpoints,
    )
    monkeypatch.setattr(processing_module, "translate_epub", lambda *_args, **_kwargs: translated)
    monkeypatch.setattr(processing_module, "validate_epub_file", lambda _path: None)

    def write_output(*_args, **_kwargs) -> Path:
        output.write_bytes(_args[1])
        return output

    monkeypatch.setattr(processing_module, "write_epub_translation_output", write_output)
    monkeypatch.setattr(
        processing_module,
        "convert_epub",
        lambda *_args, **_kwargs: ConvertedDocument("# Book\n"),
    )
    monkeypatch.setattr(
        processing_module,
        "inspect_epub_package",
        lambda path, **_kwargs: SimpleNamespace(
            title="Book",
            language="en" if Path(path) == source else "es",
            authors=(),
            cover_path=None,
            identifiers=(),
            publisher=None,
            publication_date=None,
        ),
    )

    result = process_document(
        ProcessRequest(
            source,
            convert_to_markdown=False,
            offline_translation_language="Español",
            output_format=OutputFormat.EPUB,
        ),
        settings=AppSettings(checkpoint_retention_days=0),
    )

    assert result.final_path.read_bytes() == b"complete epub"
    assert result.epub_checkpoint_degraded is True
    assert result.epub_translation_parts == 2
    assert result.preserved_images == 0
    assert cleared == [True]


def test_direct_epub_translation_does_not_publish_before_review_preparation_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.epub"
    source.write_bytes(b"synthetic epub handled by orchestration doubles")
    translated = SimpleNamespace(
        content=b"translated epub",
        language_code="es",
        translation_parts=1,
        resumed_parts=0,
        checkpoint_degraded=False,
        quality_report=None,
    )
    writer_calls = 0

    monkeypatch.setattr(
        processing_module,
        "open_epub_translation_checkpoints",
        lambda *_args, **_kwargs: SimpleNamespace(
            load=lambda _key: None,
            save=lambda _key, _payload: True,
            clear=lambda: None,
        ),
    )
    monkeypatch.setattr(processing_module, "translate_epub", lambda *_args, **_kwargs: translated)
    monkeypatch.setattr(
        processing_module,
        "inspect_epub_package",
        lambda path, **_kwargs: SimpleNamespace(
            title="Book",
            language="en" if Path(path) == source else "es",
            authors=(),
            cover_path=None,
            identifiers=(),
            publisher=None,
            publication_date=None,
        ),
    )

    def convert(path: Path, **_kwargs) -> ConvertedDocument:
        if Path(path) != source:
            raise ConversionError("staged review failed")
        return ConvertedDocument(
            "This sufficiently long English source paragraph establishes the source language "
            "before the direct package translation begins."
        )

    def write_output(*_args, **_kwargs) -> Path:
        nonlocal writer_calls
        writer_calls += 1
        return tmp_path / "book.es.epub"

    monkeypatch.setattr(processing_module, "convert_epub", convert)
    monkeypatch.setattr(processing_module, "write_epub_translation_output", write_output)

    with pytest.raises(ConversionError, match="staged review failed"):
        process_document(
            ProcessRequest(
                source,
                convert_to_markdown=False,
                offline_translation_language="Español",
                output_format=OutputFormat.EPUB,
            )
        )

    assert writer_calls == 0
    assert not (tmp_path / "book.es.epub").exists()
    assert not tuple(tmp_path.glob(".parsezen-epub-prepare-*.epub"))


def test_direct_epub_translation_can_remove_content_images_but_keep_the_cover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.epub"
    source.write_bytes(b"synthetic epub handled by orchestration doubles")
    output = tmp_path / "book.es.epub"
    resource = ConvertedResource(
        PurePosixPath("images/figure.png"),
        b"image",
        "image/png",
    )
    translated = SimpleNamespace(
        content=b"translated epub",
        language_code="es",
        translation_parts=1,
        resumed_parts=0,
        checkpoint_degraded=False,
        quality_report=None,
    )
    built_arguments: list[tuple[str, tuple[ConvertedResource, ...], EpubBookMetadata]] = []

    monkeypatch.setattr(
        processing_module,
        "open_epub_translation_checkpoints",
        lambda *_args, **_kwargs: SimpleNamespace(
            load=lambda _key: None,
            save=lambda _key, _payload: True,
            clear=lambda: None,
        ),
    )
    monkeypatch.setattr(processing_module, "translate_epub", lambda *_args, **_kwargs: translated)
    monkeypatch.setattr(processing_module, "validate_epub_file", lambda _path: None)

    def write_output(*_args, **_kwargs) -> Path:
        output.write_bytes(_args[1])
        return output

    monkeypatch.setattr(processing_module, "write_epub_translation_output", write_output)
    monkeypatch.setattr(
        processing_module,
        "convert_epub",
        lambda *_args, **_kwargs: ConvertedDocument(
            "# Libro\n\n![Figura](__parsezen_resources__/images/figure.png)\n",
            (resource,),
        ),
    )
    monkeypatch.setattr(
        processing_module,
        "inspect_epub_package",
        lambda path: SimpleNamespace(
            title="Libro",
            language="en" if Path(path) == source else "es",
            authors=("Autora",),
            cover_path=resource.relative_path,
            identifiers=(),
            publisher=None,
            publication_date=None,
        ),
    )

    def build(markdown, resources, metadata, **_kwargs):
        built_arguments.append((markdown, resources, metadata))
        return SimpleNamespace(
            content=b"normalized without images",
            chapter_count=1,
            resource_count=1,
            integrity_report=None,
        )

    monkeypatch.setattr(processing_module, "build_epub", build)

    result = process_document(
        ProcessRequest(
            source,
            convert_to_markdown=False,
            offline_translation_language="Español",
            output_format=OutputFormat.EPUB,
            include_images=False,
        )
    )

    markdown, resources, metadata = built_arguments[0]
    assert "![Figura]" not in markdown
    assert resources == (resource,)
    assert metadata.cover_resource == resource.relative_path
    assert result.final_path.read_bytes() == b"normalized without images"
    assert result.preserved_images == 1


def test_direct_epub_translation_can_normalize_styles_while_retaining_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.epub"
    source.write_bytes(b"synthetic epub handled by orchestration doubles")
    output = tmp_path / "book.es.epub"
    resource = ConvertedResource(PurePosixPath("images/figure.png"), b"image", "image/png")
    translated = SimpleNamespace(
        content=b"translated epub",
        language_code="es",
        translation_parts=1,
        resumed_parts=0,
        checkpoint_degraded=False,
        quality_report=None,
    )
    built_resources: list[tuple[ConvertedResource, ...]] = []

    monkeypatch.setattr(
        processing_module,
        "open_epub_translation_checkpoints",
        lambda *_args, **_kwargs: SimpleNamespace(
            load=lambda _key: None,
            save=lambda _key, _payload: True,
            clear=lambda: None,
        ),
    )
    monkeypatch.setattr(processing_module, "translate_epub", lambda *_args, **_kwargs: translated)
    monkeypatch.setattr(processing_module, "validate_epub_file", lambda _path: None)

    def write_output(*_args, **_kwargs) -> Path:
        output.write_bytes(_args[1])
        return output

    monkeypatch.setattr(processing_module, "write_epub_translation_output", write_output)
    monkeypatch.setattr(
        processing_module,
        "convert_epub",
        lambda *_args, **_kwargs: ConvertedDocument(
            "# Libro\n\n![Figura](__parsezen_resources__/images/figure.png)\n",
            (resource,),
        ),
    )
    monkeypatch.setattr(
        processing_module,
        "inspect_epub_package",
        lambda path: SimpleNamespace(
            title="Libro",
            language="en" if Path(path) == source else "es",
            authors=(),
            cover_path=None,
            identifiers=(),
            publisher=None,
            publication_date=None,
        ),
    )

    def build(_markdown, resources, _metadata, **_kwargs):
        built_resources.append(resources)
        return SimpleNamespace(
            content=b"normalized with image",
            chapter_count=1,
            resource_count=1,
            integrity_report=None,
        )

    monkeypatch.setattr(processing_module, "build_epub", build)

    result = process_document(
        ProcessRequest(
            source,
            convert_to_markdown=False,
            offline_translation_language="Español",
            output_format=OutputFormat.EPUB,
            preserve_styles=False,
        )
    )

    assert built_resources == [(resource,)]
    assert result.final_path.read_bytes() == b"normalized with image"
    assert result.preserved_images == 1


def test_direct_epub_translation_requires_epub_as_the_selected_output(
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.epub"
    source.write_bytes(b"placeholder")

    with pytest.raises(RequestValidationError, match="necesita convertirse"):
        process_document(
            ProcessRequest(
                source,
                convert_to_markdown=False,
                offline_translation_language="Español",
                output_format=OutputFormat.MARKDOWN,
            )
        )


def test_direct_epub_translation_rejects_cleaning_that_could_break_layout(
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.epub"
    source.write_bytes(b"placeholder")

    with pytest.raises(RequestValidationError, match="configuración antigua"):
        process_document(
            ProcessRequest(
                source,
                convert_to_markdown=False,
                improvement_mode=ImprovementMode.CLEAN_AND_TRANSLATE,
                target_language="Español",
                output_format=OutputFormat.EPUB,
            ),
            settings=LOCAL_SETTINGS,
        )


def test_direct_epub_offline_translation_also_rejects_ai_cleaning(
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.epub"
    source.write_bytes(b"placeholder")

    with pytest.raises(RequestValidationError, match="configuración antigua"):
        process_document(
            ProcessRequest(
                source,
                convert_to_markdown=False,
                improvement_mode=ImprovementMode.CLEAN,
                offline_translation_language="Español",
                output_format=OutputFormat.EPUB,
            ),
            settings=LOCAL_SETTINGS,
        )


def test_offline_translation_repairs_one_residual_source_block_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "document.md"
    original = (
        "The first complete paragraph explains the purpose of the document clearly.\n\n"
        "The second complete paragraph remains in English and needs one focused retry."
    )
    source.write_text(original, encoding="utf-8")
    first_translation = (
        "El primer párrafo completo explica claramente el propósito del documento.\n\n"
        "The second complete paragraph remains in English and needs one focused retry."
    )
    calls: list[str] = []

    def translate(markdown: str, _language: str, **kwargs) -> str:
        calls.append(markdown)
        if len(calls) == 1:
            assert "source_language_code" not in kwargs
            return first_translation
        assert kwargs["source_language_code"] == "en"
        return "El segundo párrafo completo necesitaba un único reintento específico."

    _patch_transform_dependency(monkeypatch, "translate_markdown_offline", translate)

    result = process_document(
        ProcessRequest(
            source,
            convert_to_markdown=False,
            offline_translation_language="Español",
        )
    )

    assert len(calls) == 2
    output = result.final_path.read_text(encoding="utf-8")
    assert "El segundo párrafo" in output
    assert "The second complete" not in output
    assert result.translation_quality_report is not None
    assert result.translation_quality_report.total_issues == 0


def test_ai_translation_repairs_one_residual_source_block_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "document.md"
    original = (
        "The first complete paragraph explains the purpose of the document clearly.\n\n"
        "The second complete paragraph remains in English and needs one focused retry."
    )
    source.write_text(original, encoding="utf-8")
    first_translation = (
        "El primer párrafo completo explica claramente el propósito del documento.\n\n"
        "The second complete paragraph remains in English and needs one focused retry."
    )
    calls: list[tuple[str, ImprovementMode]] = []

    def improve(markdown: str, mode: ImprovementMode, *_args, **_kwargs) -> str:
        calls.append((markdown, mode))
        if len(calls) == 1:
            return first_translation
        return "El segundo párrafo completo necesitaba un único reintento específico."

    _patch_transform_dependency(monkeypatch, "improve_markdown", improve)

    result = process_document(
        ProcessRequest(
            source,
            convert_to_markdown=False,
            improvement_mode=ImprovementMode.TRANSLATE,
            target_language="Español",
        ),
        settings=LOCAL_SETTINGS,
    )

    assert [mode for _markdown, mode in calls] == [
        ImprovementMode.TRANSLATE,
        ImprovementMode.TRANSLATE,
    ]
    assert calls[1][0].startswith("The second")
    assert result.translation_quality_report is not None
    assert result.translation_quality_report.total_issues == 0


def test_local_cleanup_runs_before_offline_translation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "document.md"
    source.write_text("Original", encoding="utf-8")
    settings = AppSettings(
        model="local-model",
    )
    order: list[str] = []

    def improve(*_args, **_kwargs) -> str:
        order.append("clean")
        return "Clean"

    def translate(markdown: str, *_args, **_kwargs) -> str:
        order.append(f"translate:{markdown}")
        return "Translated"

    _patch_transform_dependency(monkeypatch, "improve_markdown", improve)
    _patch_transform_dependency(monkeypatch, "translate_markdown_offline", translate)

    result = process_document(
        ProcessRequest(
            source,
            convert_to_markdown=False,
            improvement_mode=ImprovementMode.CLEAN,
            offline_translation_language="Inglés",
        ),
        settings=settings,
    )

    assert order == ["clean", "translate:Clean"]
    assert result.final_path.read_text(encoding="utf-8") == "Translated"


def test_rejects_two_translation_services_in_one_request(tmp_path: Path) -> None:
    source = tmp_path / "document.md"
    source.write_text("Original", encoding="utf-8")
    settings = AppSettings(
        model="local-model",
    )

    with pytest.raises(RequestValidationError, match="pero no ambas"):
        process_document(
            ProcessRequest(
                source,
                convert_to_markdown=False,
                improvement_mode=ImprovementMode.TRANSLATE,
                target_language="Español",
                offline_translation_language="Inglés",
            ),
            settings=settings,
        )


def test_reports_improvement_chunk_progress_to_the_caller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "document.md"
    source.write_text("# Original 10", encoding="utf-8")
    progress: list[tuple[int, int]] = []

    def improve(
        markdown: str,
        _mode: ImprovementMode,
        _settings: AppSettings,
        _target_language: str | None,
        *,
        on_progress,
    ) -> str:
        on_progress(1, 2)
        on_progress(2, 2)
        return markdown

    _patch_transform_dependency(monkeypatch, "improve_markdown", improve)

    process_document(
        ProcessRequest(
            source,
            convert_to_markdown=False,
            improvement_mode=ImprovementMode.CLEAN,
        ),
        on_progress=lambda current, total: progress.append((current, total)),
        settings=LOCAL_SETTINGS,
    )

    assert progress == [(1, 2), (2, 2)]


def test_improvement_outputs_use_one_shared_collision_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "document.docx"
    source.write_bytes(b"placeholder")
    (tmp_path / "document.raw.md").write_text("Existing raw", encoding="utf-8")
    monkeypatch.setattr(
        processing_module,
        "convert_document",
        lambda _path, **_kwargs: ConvertedDocument("Raw"),
    )
    _patch_transform_dependency(
        monkeypatch,
        "improve_markdown",
        lambda *_args, **_kwargs: "Mended",
    )

    result = process_document(
        ProcessRequest(
            source,
            True,
            improvement_mode=ImprovementMode.CLEAN,
        ),
        settings=LOCAL_SETTINGS,
    )

    assert result.final_path == tmp_path / "document-2.mended.md"
    assert result.raw_markdown_path == tmp_path / "document-2.raw.md"
    assert (tmp_path / "document.raw.md").read_text(encoding="utf-8") == "Existing raw"


def test_pdf_reports_ocr_only_when_selective_ocr_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"placeholder")
    stages: list[ProcessStage] = []

    def convert(_path: Path, on_ocr_start=None, **_kwargs) -> ConvertedDocument:
        assert on_ocr_start is not None
        on_ocr_start()
        return ConvertedDocument("# Recognized scan")

    monkeypatch.setattr(processing_module, "convert_document", convert)

    result = process_document(
        ProcessRequest(source, convert_to_markdown=True),
        on_stage=stages.append,
    )

    assert stages == [
        ProcessStage.VALIDATING,
        ProcessStage.CONVERTING,
        ProcessStage.OCR,
        ProcessStage.WRITING,
        ProcessStage.COMPLETED,
    ]
    assert result.final_path.read_text(encoding="utf-8") == "# Recognized scan"


def test_pdf_ocr_checkpoint_encoding_preserves_empty_results() -> None:
    encoded_empty = prepare_module.encode_pdf_ocr_checkpoint("")
    encoded_text = prepare_module.encode_pdf_ocr_checkpoint("Recognized")

    assert encoded_empty
    assert prepare_module.decode_pdf_ocr_checkpoint(encoded_empty) == ""
    assert prepare_module.decode_pdf_ocr_checkpoint(encoded_text) == "Recognized"
    assert prepare_module.decode_pdf_ocr_checkpoint("Legacy") == "Legacy"
    assert prepare_module.decode_pdf_ocr_checkpoint(None) is None


def test_pdf_ocr_checkpoint_rejects_a_stale_tagged_version() -> None:
    stale = "\x1eParsezen PDF OCR v2\x1fRecognized"

    assert prepare_module.decode_pdf_ocr_checkpoint(stale) is None


def test_pdf_result_reports_pages_marked_for_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"placeholder")
    report = PdfQualityReport(
        processed_pages=tuple(range(1, 13)),
        ocr_pages=(3, 12),
        issues=(
            PdfReviewIssue(3, "Revisa esta página.", "Texto de la página 3."),
            PdfReviewIssue(12, "Compara esta página.", "Texto de la página 12."),
        ),
    )

    def convert(_path: Path, **kwargs) -> ConvertedDocument:
        kwargs["on_pdf_quality_report"](report)
        return ConvertedDocument(
            "# Book\n\n"
            "> **Aviso OCR (página 12):** compara esta página con el original.\n\n"
            "> **Aviso de conversión (página 3):** revisa esta página."
        )

    monkeypatch.setattr(processing_module, "convert_document", convert)

    result = process_document(ProcessRequest(source, convert_to_markdown=True))

    assert result.problematic_pdf_pages == (3, 12)
    assert result.pdf_quality_report is report
    assert result.exhaustive_pdf_ocr_used is False


def test_forced_pdf_ocr_is_forwarded_only_for_the_focused_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"placeholder")
    calls: list[dict[str, object]] = []

    def convert(_path: Path, **kwargs) -> ConvertedDocument:
        calls.append(kwargs)
        return ConvertedDocument("# Focused page")

    monkeypatch.setattr(
        processing_module,
        "resolve_pdf_page_range",
        lambda _path, requested: requested,
    )
    monkeypatch.setattr(processing_module, "convert_document", convert)

    result = process_document(
        ProcessRequest(
            source,
            convert_to_markdown=True,
            pdf_page_range=PdfPageRange(7, 7),
            force_pdf_ocr=True,
        )
    )

    assert calls[0]["force_pdf_ocr"] is True
    assert calls[0]["pdf_page_range"] == PdfPageRange(7, 7)
    assert result.exhaustive_pdf_ocr_used is True


def test_improvement_requires_settings_before_reading(tmp_path: Path) -> None:
    source = tmp_path / "document.md"
    source.write_text("Content", encoding="utf-8")
    stages: list[ProcessStage] = []

    with pytest.raises(SettingsError, match="Elige un modelo"):
        process_document(
            ProcessRequest(
                source,
                False,
                improvement_mode=ImprovementMode.CLEAN,
            ),
            on_stage=stages.append,
        )

    assert stages == [ProcessStage.VALIDATING]


def test_reasoning_model_is_rejected_before_reading_document(tmp_path: Path) -> None:
    source = tmp_path / "document.md"
    source.write_text("Content", encoding="utf-8")
    stages: list[ProcessStage] = []

    with pytest.raises(SettingsError, match="Instruct"):
        process_document(
            ProcessRequest(
                source,
                False,
                improvement_mode=ImprovementMode.CLEAN,
            ),
            settings=AppSettings(model="qwen3:4b"),
            on_stage=stages.append,
        )

    assert stages == [ProcessStage.VALIDATING]


@pytest.mark.parametrize("extension, format_name", [(".docx", "DOCX"), (".pdf", "PDF")])
def test_converted_document_cannot_skip_conversion_even_when_improving(
    tmp_path: Path,
    extension: str,
    format_name: str,
) -> None:
    source = tmp_path / f"document{extension}"
    source.write_bytes(b"placeholder")

    with pytest.raises(RequestValidationError, match=rf"{format_name} necesita convertirse"):
        process_document(
            ProcessRequest(
                source,
                False,
                improvement_mode=ImprovementMode.CLEAN,
            ),
            settings=LOCAL_SETTINGS,
        )


def test_failed_improvement_does_not_publish_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "document.docx"
    source.write_bytes(b"placeholder")
    monkeypatch.setattr(
        processing_module,
        "convert_document",
        lambda _path, **_kwargs: ConvertedDocument("Raw"),
    )

    def fail(*_args, **_kwargs):
        raise ImprovementError("Unsafe response")

    _patch_transform_dependency(monkeypatch, "improve_markdown", fail)

    with pytest.raises(ImprovementError, match="Unsafe"):
        process_document(
            ProcessRequest(
                source,
                True,
                improvement_mode=ImprovementMode.CLEAN,
            ),
            settings=LOCAL_SETTINGS,
        )

    assert list(tmp_path.glob("*.raw.md")) == []
    assert list(tmp_path.glob("*.mended.md")) == []


def test_rejects_a_missing_source_before_other_stages(tmp_path: Path) -> None:
    stages: list[ProcessStage] = []

    with pytest.raises(RequestValidationError, match="No existe"):
        process_document(
            ProcessRequest(tmp_path / "missing.txt", convert_to_markdown=True),
            on_stage=stages.append,
        )

    assert stages == [ProcessStage.VALIDATING]


def test_cancellation_after_conversion_never_publishes_an_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "document.docx"
    source.write_bytes(b"placeholder")
    cancellation = CancellationToken()

    def convert(
        _path: Path,
        *,
        cancellation: CancellationToken,
        **_kwargs,
    ) -> ConvertedDocument:
        cancellation.cancel()
        return ConvertedDocument("# Converted but not published")

    monkeypatch.setattr(processing_module, "convert_document", convert)

    with pytest.raises(ProcessingCancelledError):
        process_document(
            ProcessRequest(source, convert_to_markdown=True),
            cancellation=cancellation,
        )

    assert list(tmp_path.glob("*.md")) == []
    assert list(tmp_path.glob(".parsezen-*.tmp")) == []


def test_cleans_temporary_output_when_atomic_publish_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "document.txt"
    source.write_text("Content", encoding="utf-8")

    def fail_publish(_source: Path, _destination: Path) -> None:
        raise PermissionError

    monkeypatch.setattr(output_module.os, "link", fail_publish)
    monkeypatch.setattr(output_module.os, "rename", fail_publish)

    with pytest.raises(OutputWriteError, match="document.md"):
        process_document(ProcessRequest(source, convert_to_markdown=True))

    assert not (tmp_path / "document.md").exists()
    assert list(tmp_path.glob(".parsezen-*.tmp")) == []


def test_atomic_publish_never_exposes_an_empty_reservation(tmp_path: Path) -> None:
    temporary = tmp_path / ".complete.tmp"
    destination = tmp_path / "result.md"
    temporary.write_text("Complete content", encoding="utf-8")

    output_module._publish_without_overwrite(temporary, destination)

    assert destination.read_text(encoding="utf-8") == "Complete content"
    assert destination.stat().st_size > 0
    assert not temporary.exists()
