from pathlib import Path

import pytest

from parsezen.application.runtime_mapping import (
    configuration_from_request,
    request_and_settings_from_job,
    stage_kind_from_process_stage,
)
from parsezen.domain.jobs import (
    CoverStrategy,
    DocumentFormat,
    DocumentJob,
    DocumentSource,
    JobConfiguration,
    OutputConfiguration,
)
from parsezen.domain.stages import StageKind
from parsezen.processing import OutputFormat, ProcessRequest, ProcessStage
from parsezen.settings import AppSettings


def test_process_request_and_settings_round_trip_independent_configuration() -> None:
    request = ProcessRequest(
        source_path=Path("book.pdf"),
        convert_to_markdown=True,
        output_directory=Path("output"),
        target_language="Español",
        output_format=OutputFormat.EPUB,
        preserve_styles=False,
        review_content=True,
        review_structure=True,
    )
    settings = AppSettings(model="qwen3:4b", context_window=8192)
    configuration = configuration_from_request(request, settings)
    job = DocumentJob.create(
        DocumentSource(Path("book.pdf"), DocumentFormat.PDF, 100, 1),
        configuration,
        order=0,
    )

    restored_request, restored_settings = request_and_settings_from_job(job)

    assert restored_request.source_path == request.source_path
    assert restored_request.output_format is OutputFormat.EPUB
    assert not restored_request.preserve_styles
    assert restored_request.review_content
    assert restored_request.review_structure
    assert configuration.ai.model == "qwen3:4b"
    assert configuration.ai.context_window == 8192
    assert configuration.translation.model is None
    assert configuration.refinement.model is None
    assert restored_settings.model == "qwen3:4b"
    assert restored_settings.context_window == 8192


def test_epub_cover_removal_reaches_the_physical_request() -> None:
    job = DocumentJob.create(
        DocumentSource(Path("book.epub"), DocumentFormat.EPUB, 100, 1),
        JobConfiguration(
            output=OutputConfiguration(
                format=DocumentFormat.EPUB,
                cover_strategy=CoverStrategy.REMOVE,
            )
        ),
        order=0,
    )

    request, _settings = request_and_settings_from_job(job)

    assert request.epub_remove_cover
    assert request.epub_cover_path is None
    assert not request.epub_first_page_cover


def test_processor_stages_map_to_stable_domain_phases() -> None:
    for stage in (
        ProcessStage.VALIDATING,
        ProcessStage.READING,
        ProcessStage.CONVERTING,
        ProcessStage.OCR,
        ProcessStage.PRESERVING_IMAGES,
        ProcessStage.STRUCTURING,
    ):
        assert stage_kind_from_process_stage(stage) is StageKind.PREPARE
    assert stage_kind_from_process_stage(ProcessStage.TRANSLATING) is StageKind.TRANSLATE
    assert stage_kind_from_process_stage(ProcessStage.REVIEWING_CONTENT) is StageKind.REFINE
    assert stage_kind_from_process_stage(ProcessStage.ORGANIZING_STRUCTURE) is StageKind.STRUCTURE
    assert stage_kind_from_process_stage(ProcessStage.WRITING) is StageKind.PUBLISH


def test_translation_precedes_refinement_in_the_stable_pipeline() -> None:
    job = DocumentJob.create(
        DocumentSource(Path("book.pdf"), DocumentFormat.PDF, 100, 1),
        configuration_from_request(
            ProcessRequest(
                Path("book.pdf"),
                convert_to_markdown=True,
                target_language="Español",
                review_content=True,
            ),
            AppSettings(model="qwen3:4b"),
        ),
        order=0,
    )

    participating = tuple(stage.kind for stage in job.stages if stage.participates)

    assert participating.index(StageKind.TRANSLATE) < participating.index(StageKind.REFINE)


def test_runtime_mapping_rejects_a_job_without_deliberate_output() -> None:
    job = DocumentJob.create(
        DocumentSource(Path("book.pdf"), DocumentFormat.PDF, 100, 1),
        JobConfiguration(output=OutputConfiguration(configured=False)),
        order=0,
    )

    with pytest.raises(ValueError, match="Configura el resultado"):
        request_and_settings_from_job(job)
