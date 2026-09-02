from dataclasses import replace

from parsezen.application.preflight import build_workload_profile
from parsezen.application.runtime_mapping import request_and_settings_from_job
from parsezen.domain.jobs import (
    AIPhase,
    AIProfileConfiguration,
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
    resolve_ai_profile,
)
from parsezen.pipeline.contracts import ProcessRequest
from parsezen.settings import AppSettings
from parsezen.workflow import OutputFormat


def _component(model: str, digest: str, context_window: int) -> LocalAIComponentSnapshot:
    return LocalAIComponentSnapshot("policy-v1", model, digest * 64, context_window)


def test_resolves_global_and_specialized_profiles_by_phase() -> None:
    global_only = AIProfileConfiguration(model="global:7b", context_window=4_096)
    assert resolve_ai_profile(global_only, AIPhase.TRANSLATION).identity[:3] == (
        "translation",
        "global:7b",
        4_096,
    )
    specialized = replace(
        global_only,
        translation_model="translator:7b",
        translation_context_window=8_192,
        review_model="reviewer:7b",
        review_context_window=16_384,
    )
    assert (
        resolve_ai_profile(specialized, AIPhase.TRANSLATION).model,
        resolve_ai_profile(specialized, AIPhase.TRANSLATION).context_window,
    ) == ("translator:7b", 8_192)
    assert (
        resolve_ai_profile(specialized, AIPhase.REVIEW).model,
        resolve_ai_profile(specialized, AIPhase.REVIEW).context_window,
    ) == ("reviewer:7b", 16_384)


def test_fixed_components_precede_specialized_and_global_profiles() -> None:
    configuration = AIProfileConfiguration(
        model="global:7b",
        context_window=4_096,
        translation_model="custom-translator:7b",
        review_model="custom-reviewer:7b",
        components=LocalAIPolicySnapshot(
            translation=_component("fixed-translator:7b", "a", 8_192),
            review=_component("fixed-reviewer:7b", "b", 16_384),
            visual_ocr=_component("fixed-visual:7b", "c", 32_768),
        ),
    )

    assert resolve_ai_profile(configuration, AIPhase.TRANSLATION).model == "fixed-translator:7b"
    assert resolve_ai_profile(configuration, AIPhase.REVIEW).model == "fixed-reviewer:7b"
    visual = resolve_ai_profile(configuration, AIPhase.VISUAL_OCR)
    assert (visual.model, visual.context_window) == ("fixed-visual:7b", 32_768)


def test_runtime_mapping_keeps_the_job_snapshot_for_every_ai_phase(tmp_path) -> None:
    source_path = tmp_path / "source.md"
    source_path.write_text("Text", encoding="utf-8")
    ai = AIProfileConfiguration(
        model="visual:7b",
        context_window=4_096,
        translation_model="translator:7b",
        translation_context_window=8_192,
        review_model="reviewer:7b",
        review_context_window=16_384,
    )
    job = DocumentJob.create(
        DocumentSource.inspect(source_path),
        JobConfiguration(
            output=OutputConfiguration(format=DocumentFormat.MARKDOWN),
            ai=ai,
            translation=TranslationConfiguration(
                enabled=True,
                method=TranslationMethod.LOCAL_AI,
                target_language="es",
            ),
            plan=ProcessingPlan.LOCAL_AI_REVIEWED,
        ),
        order=0,
    )

    _request, settings = request_and_settings_from_job(job)

    assert (settings.model, settings.context_window) == ("visual:7b", 4_096)
    assert (settings.translation_model, settings.translation_context_window) == (
        "translator:7b",
        8_192,
    )
    assert (settings.review_model, settings.review_context_window) == ("reviewer:7b", 16_384)


def test_eta_identity_uses_the_job_profiles_not_later_global_settings(tmp_path) -> None:
    source_path = tmp_path / "source.md"
    source_path.write_text("Text", encoding="utf-8")
    base = JobConfiguration(
        output=OutputConfiguration(format=DocumentFormat.MARKDOWN),
        ai=AIProfileConfiguration(
            translation_model="translator:7b",
            translation_context_window=8_192,
            review_model="reviewer:7b",
            review_context_window=16_384,
        ),
        translation=TranslationConfiguration(
            enabled=True,
            method=TranslationMethod.LOCAL_AI,
            target_language="es",
        ),
        plan=ProcessingPlan.LOCAL_AI_REVIEWED,
    )
    job = DocumentJob.create(DocumentSource.inspect(source_path), base, order=0)
    request = ProcessRequest(
        source_path,
        convert_to_markdown=False,
        output_format=OutputFormat.MARKDOWN,
    )

    old_profile, _label = build_workload_profile(job, request, AppSettings(model="old:7b"))
    changed_global, _label = build_workload_profile(job, request, AppSettings(model="new:7b"))
    changed_review_job = replace(
        job,
        configuration=replace(
            base,
            ai=replace(base.ai, review_context_window=32_768),
        ),
    )
    changed_review, _label = build_workload_profile(
        changed_review_job,
        request,
        AppSettings(model="old:7b"),
    )

    assert old_profile.model_key == changed_global.model_key
    assert old_profile.model_key != changed_review.model_key
