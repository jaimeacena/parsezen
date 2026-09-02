from pathlib import Path

from parsezen.application.job_queue import JobQueue
from parsezen.application.queue_session import QueueSession
from parsezen.application.workspace_recovery_controller import WorkspaceRecoveryController
from parsezen.domain.jobs import DocumentJob, DocumentSource, JobConfiguration, JobStatus
from parsezen.domain.stages import StageKind, StageStatus
from parsezen.infrastructure.artifact_store import ArtifactStore
from parsezen.infrastructure.result_snapshots import ResultSnapshotStore
from parsezen.infrastructure.state_store import StateStore
from parsezen.settings import AppSettings


def test_workspace_recovery_controller_restores_and_pauses_interrupted_job(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source.txt"
    source_path.write_text("Text", encoding="utf-8")
    job = DocumentJob.create(
        DocumentSource.inspect(source_path, include_content_hash=False),
        JobConfiguration(),
        order=0,
        job_id="interrupted",
    )
    job = job.replace_stage(
        job.stage(StageKind.PREPARE).transition(StageStatus.READY).transition(StageStatus.RUNNING)
    )
    state = StateStore(tmp_path / "workspace.sqlite3")
    state.replace_jobs((job,))
    artifacts = ArtifactStore(tmp_path / "artifacts")
    queue = JobQueue()
    session = QueueSession(queue)
    controller = WorkspaceRecoveryController(
        state,
        artifacts,
        ResultSnapshotStore(state, artifacts),
        queue,
        session.execution,
        session,
        AppSettings(),
    )

    outcome = controller.restore()

    restored = queue.get(job.id)
    assert restored is not None
    assert restored.status is JobStatus.PAUSED
    assert session.runtime_for(job.id) is not None
    assert outcome.retained_artifact_job_ids == frozenset()
    assert not outcome.persistence_unavailable


def test_workspace_recovery_controller_prunes_only_unretained_artifacts(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "workspace.sqlite3")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    artifacts.put_text(job_id="retained", text="keep")
    artifacts.put_text(job_id="orphan", text="remove")
    queue = JobQueue()
    session = QueueSession(queue)
    controller = WorkspaceRecoveryController(
        state,
        artifacts,
        ResultSnapshotStore(state, artifacts),
        queue,
        session.execution,
        session,
        AppSettings(),
    )

    controller.prune_orphaned_review_artifacts(frozenset({"retained"}))

    assert (artifacts.root / "retained").is_dir()
    assert not (artifacts.root / "orphan").exists()
