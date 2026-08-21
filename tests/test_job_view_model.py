from dataclasses import replace
from pathlib import Path

from parsezen.application.planner import activate_next_stage
from parsezen.domain.jobs import (
    DocumentFormat,
    DocumentJob,
    DocumentSource,
    JobConfiguration,
    OutputConfiguration,
    ProcessingPlan,
    TranslationConfiguration,
)
from parsezen.domain.stages import StageKind, StageStatus
from parsezen.presentation.job_view_model import (
    JobAction,
    next_step_view,
    queue_header_view,
)


def make_job(
    identifier: str,
    *,
    configured: bool = True,
    output_format: DocumentFormat = DocumentFormat.EPUB,
) -> DocumentJob:
    job = DocumentJob.create(
        DocumentSource(Path(f"{identifier}.pdf"), DocumentFormat.PDF, 1_000, 2),
        JobConfiguration(
            output=OutputConfiguration(
                format=output_format,
                configured=configured,
            )
        ),
        order=0,
        job_id=identifier,
    )
    return activate_next_stage(job) if configured else job


def make_review_job(
    identifier: str,
    *,
    output_format: DocumentFormat = DocumentFormat.EPUB,
) -> DocumentJob:
    job = make_job(identifier, output_format=output_format)
    return job.replace_stage(
        job.stage(StageKind.PREPARE)
        .transition(StageStatus.RUNNING)
        .transition(StageStatus.BLOCKED_FOR_REVIEW, review_id="review")
    )


def make_completed_job(identifier: str) -> DocumentJob:
    job = make_job(identifier)
    for stage in job.stages:
        if not stage.participates:
            continue
        current = job.stage(stage.kind)
        if current.status is StageStatus.PENDING:
            current = current.transition(StageStatus.READY)
        current = current.transition(StageStatus.RUNNING).transition(StageStatus.COMPLETED)
        job = job.replace_stage(current)
    return replace(job, result_path=Path(f"{identifier}.epub"))


def test_next_step_keeps_missing_configuration_distinct_from_review() -> None:
    missing = next_step_view(make_job("missing", configured=False))
    review = next_step_view(make_review_job("review"))

    assert (missing.label, missing.action) == ("Pendiente", JobAction.CONFIGURE)
    assert (review.label, review.action) == (
        "Necesita tu revisión",
        JobAction.REVIEW,
    )
    assert review.action_label == "Revisar y publicar"


def test_review_action_names_the_human_decision_for_editable_output() -> None:
    review = next_step_view(
        make_review_job("review-markdown", output_format=DocumentFormat.MARKDOWN)
    )

    assert review.action_label == "Revisar cambios"


def test_next_step_exposes_ready_result() -> None:
    presentation = next_step_view(make_completed_job("done"))

    assert presentation.label == "Listo"
    assert presentation.action is JobAction.OPEN_RESULT
    assert presentation.action_label == "Abrir resultado"


def test_header_pluralization_and_review_priority() -> None:
    view = queue_header_view((make_review_job("one"), make_review_job("two"), make_job("ready")))

    assert view.summary == "3 documentos · 2 por revisar"
    assert view.review_count == 2
    assert view.primary_mode == "review"
    assert view.primary_label == "Revisar 2 pendientes"


def test_header_processes_only_configured_documents() -> None:
    view = queue_header_view((make_job("ready"), make_job("missing", configured=False)))

    assert view.summary == "2 documentos"
    assert view.primary_mode == "process"
    assert view.primary_label == "Procesar 1 documento"


def test_current_activity_follows_the_canonical_flow_order() -> None:
    job = DocumentJob.create(
        DocumentSource(Path("flow.pdf"), DocumentFormat.PDF, 1_000, 2),
        JobConfiguration(
            output=OutputConfiguration(format=DocumentFormat.EPUB),
            translation=TranslationConfiguration(enabled=True, target_language="es"),
            plan=ProcessingPlan.LOCAL_AI_REVIEWED,
        ),
        order=0,
        job_id="flow",
    )

    labels: list[str] = []
    for stage_kind, expected_label in (
        (StageKind.PREPARE, "Preparando"),
        (StageKind.TRANSLATE, "Traduciendo"),
        (StageKind.REFINE, "Corrigiendo"),
        (StageKind.STRUCTURE, "Personalizando"),
        (StageKind.PUBLISH, "Publicando"),
    ):
        stage = job.stage(stage_kind)
        if stage.status is StageStatus.PENDING:
            stage = stage.transition(StageStatus.READY)
        stage = stage.transition(StageStatus.RUNNING)
        job = job.replace_stage(stage)

        presentation = next_step_view(job)
        labels.append(presentation.label)
        assert presentation.stage is stage_kind
        assert presentation.label == expected_label

        job = job.replace_stage(stage.transition(StageStatus.COMPLETED))

    assert labels == [
        "Preparando",
        "Traduciendo",
        "Corrigiendo",
        "Personalizando",
        "Publicando",
    ]


def test_prepare_activity_never_exposes_an_internal_progress_message() -> None:
    job = make_job("preparing")
    stage = replace(
        job.stage(StageKind.PREPARE).transition(StageStatus.RUNNING),
        progress_message="Extrayendo páginas",
    )

    presentation = next_step_view(job.replace_stage(stage))

    assert presentation.label == "Preparando"
