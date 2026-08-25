from pathlib import Path

import pytest

from parsezen.application.runtime_mapping import (
    configuration_from_request,
    request_and_settings_from_job,
)
from parsezen.domain.jobs import (
    CoverStrategy,
    DocumentFormat,
    DocumentJob,
    DocumentSource,
    JobConfiguration,
    LocalAIComponentSnapshot,
    LocalAIPolicySnapshot,
    OutputConfiguration,
    ProcessingPlan,
    TranslationConfiguration,
    TranslationMethod,
)
from parsezen.domain.process_lifecycle import stage_kind_from_process_stage
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
    assert configuration.plan is ProcessingPlan.LOCAL_AI_REVIEWED
    assert configuration.translation.method is TranslationMethod.LOCAL_AI
    assert restored_request.target_language == "Español"
    assert restored_request.offline_translation_language is None
    assert restored_request.source_size_bytes == job.source.size_bytes
    assert restored_request.source_modified_ns == job.source.modified_ns
    assert restored_request.source_content_sha256 == job.source.content_sha256
    assert restored_settings.model == "qwen3:4b"
    assert restored_settings.context_window == 8192


def test_default_job_maps_to_direct_runtime_without_ai_review_flags() -> None:
    job = DocumentJob.create(
        DocumentSource(Path("book.txt"), DocumentFormat.TEXT, 100, 1),
        JobConfiguration(),
        order=0,
    )

    request, _settings = request_and_settings_from_job(job)

    assert job.configuration.plan is ProcessingPlan.STANDARD
    assert not request.review_content
    assert not request.review_structure


def test_configuration_mapping_preserves_optional_local_ai_policy_snapshot() -> None:
    policy = LocalAIPolicySnapshot(
        translation=LocalAIComponentSnapshot(
            "policy-v1", "translator:7b", "a" * 64, context_window=8_192
        ),
        review=LocalAIComponentSnapshot(
            "policy-v1", "reviewer:7b", "b" * 64, context_window=16_384
        ),
    )

    configuration = configuration_from_request(
        ProcessRequest(Path("book.pdf"), convert_to_markdown=True, output_format=OutputFormat.EPUB),
        AppSettings(model="general:7b", context_window=8192),
        local_ai_policy=policy,
    )

    assert configuration.ai.components == policy


def test_configuration_mapping_carries_specialized_settings_without_snapshot() -> None:
    configuration = configuration_from_request(
        ProcessRequest(Path("book.pdf"), convert_to_markdown=True),
        AppSettings(
            model="general:7b",
            context_window=4_096,
            translation_model="translator:7b",
            translation_context_window=8_192,
            review_model="reviewer:7b",
            review_context_window=16_384,
        ),
    )

    assert configuration.ai.translation_model == "translator:7b"
    assert configuration.ai.translation_context_window == 8_192
    assert configuration.ai.review_model == "reviewer:7b"
    assert configuration.ai.review_context_window == 16_384


def test_runtime_mapping_uses_snapshot_models_and_contexts_per_phase() -> None:
    from parsezen.component_catalog import REVIEW_COMPONENT_MANIFEST, TRANSLATION_COMPONENT_MANIFEST

    policy = LocalAIPolicySnapshot(
        translation=LocalAIComponentSnapshot(
            TRANSLATION_COMPONENT_MANIFEST.policy_version,
            TRANSLATION_COMPONENT_MANIFEST.model_name,
            TRANSLATION_COMPONENT_MANIFEST.ollama_digest,
            context_window=TRANSLATION_COMPONENT_MANIFEST.context_window,
        ),
        review=LocalAIComponentSnapshot(
            REVIEW_COMPONENT_MANIFEST.policy_version,
            REVIEW_COMPONENT_MANIFEST.model_name,
            REVIEW_COMPONENT_MANIFEST.ollama_digest,
            context_window=REVIEW_COMPONENT_MANIFEST.context_window,
        ),
    )
    request = ProcessRequest(
        Path("book.pdf"),
        convert_to_markdown=True,
        target_language="es",
        review_content=True,
    )

    configuration = configuration_from_request(
        request,
        AppSettings(model="legacy:7b", context_window=4_096),
        local_ai_policy=policy,
    )
    job = DocumentJob.create(
        DocumentSource(Path("book.pdf"), DocumentFormat.PDF, 100, 1),
        configuration,
        order=0,
    )

    _request, settings = request_and_settings_from_job(job)

    assert (settings.translation_model, settings.translation_context_window) == (
        TRANSLATION_COMPONENT_MANIFEST.model_name,
        8_192,
    )
    assert (settings.review_model, settings.review_context_window) == (
        REVIEW_COMPONENT_MANIFEST.model_name,
        REVIEW_COMPONENT_MANIFEST.context_window,
    )


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


def test_offline_translation_maps_only_to_argos_runtime() -> None:
    job = DocumentJob.create(
        DocumentSource(Path("book.pdf"), DocumentFormat.PDF, 100, 1),
        JobConfiguration(
            translation=TranslationConfiguration(
                enabled=True,
                method=TranslationMethod.OFFLINE,
                target_language="es",
            )
        ),
        order=0,
    )

    request, settings = request_and_settings_from_job(job)

    assert request.target_language is None
    assert request.offline_translation_language == "es"
    assert request.improvement_mode is None
    assert settings.model is None


def test_runtime_mapping_rejects_a_job_without_deliberate_output() -> None:
    job = DocumentJob.create(
        DocumentSource(Path("book.pdf"), DocumentFormat.PDF, 100, 1),
        JobConfiguration(output=OutputConfiguration(configured=False)),
        order=0,
    )

    with pytest.raises(ValueError, match="Configura el resultado"):
        request_and_settings_from_job(job)
