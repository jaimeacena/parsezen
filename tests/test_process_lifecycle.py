from parsezen.domain.attempt_activity import AttemptPhase
from parsezen.domain.process_lifecycle import (
    PROCESS_STAGE_LIFECYCLE,
    ProcessStage,
    diagnostic_label_for_process_stage,
    phase_for_process_stage,
    stage_kind_from_process_stage,
)
from parsezen.domain.stages import StageKind
from parsezen.processing import ProcessStage as FacadeProcessStage


def test_every_physical_stage_has_one_complete_lifecycle_mapping() -> None:
    assert set(PROCESS_STAGE_LIFECYCLE) == set(ProcessStage)
    assert FacadeProcessStage is ProcessStage

    for stage, lifecycle in PROCESS_STAGE_LIFECYCLE.items():
        assert isinstance(lifecycle.domain_stage, StageKind)
        assert isinstance(lifecycle.attempt_phase, AttemptPhase)
        assert lifecycle.diagnostic_label.strip()
        assert stage_kind_from_process_stage(stage) is lifecycle.domain_stage
        assert phase_for_process_stage(stage) is lifecycle.attempt_phase
        assert diagnostic_label_for_process_stage(stage.value) == lifecycle.diagnostic_label


def test_lifecycle_mapping_uses_safe_defaults_for_absent_or_unknown_stages() -> None:
    assert stage_kind_from_process_stage(None) is StageKind.PREPARE
    assert phase_for_process_stage(None) is AttemptPhase.PREPARATION
    assert diagnostic_label_for_process_stage("not_started") == "inicio"
    assert diagnostic_label_for_process_stage("unknown") is None
