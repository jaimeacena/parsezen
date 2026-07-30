from parsezen.application.recovery import (
    FailureKind,
    ProcessingFailure,
    RecoveryAction,
    recovery_plan,
)
from parsezen.domain.stages import StageKind
from parsezen.errors import (
    FinalIntegrityError,
    LocalModelUnavailableError,
    OutputWriteError,
)


def test_recovery_plan_routes_local_ai_failures_to_the_relevant_control() -> None:
    failure = ProcessingFailure.from_exception(
        LocalModelUnavailableError("Ollama no está disponible.")
    )

    plan = recovery_plan(failure, stage=StageKind.TRANSLATE)

    assert failure.kind is FailureKind.LOCAL_AI
    assert plan.primary_action is RecoveryAction.LOCAL_AI
    assert plan.secondary_action is RecoveryAction.RETRY
    assert "fases anteriores" in plan.preserved_work


def test_recovery_plan_keeps_integrity_and_output_failures_distinct() -> None:
    integrity = ProcessingFailure.from_exception(
        FinalIntegrityError("El contenido temporal cambió.")
    )
    output = ProcessingFailure.from_exception(OutputWriteError("El destino está bloqueado."))

    integrity_plan = recovery_plan(integrity, stage=StageKind.PUBLISH)
    output_plan = recovery_plan(output, stage=StageKind.PUBLISH)

    assert integrity.kind is FailureKind.INTEGRITY
    assert integrity_plan.primary_action is RecoveryAction.RETRY
    assert "No se sustituyó" in integrity_plan.preserved_work
    assert output.kind is FailureKind.OUTPUT
    assert output_plan.primary_action is RecoveryAction.CONFIGURE
    assert output_plan.primary_label == "Revisar destino"


def test_saved_failure_prefers_the_persisted_classification() -> None:
    failure = ProcessingFailure.from_saved(
        FailureKind.LOCAL_AI.value,
        "Mensaje sin palabras clave.",
        stage=StageKind.REFINE,
    )

    assert failure.kind is FailureKind.LOCAL_AI
