from __future__ import annotations

from pathlib import Path

from parsezen.application.job_queue import JobQueue
from parsezen.application.queue_configuration import QueueConfigurationService
from parsezen.domain.jobs import (
    AIProfileConfiguration,
    DocumentFormat,
    DocumentJob,
    DocumentSource,
    JobConfiguration,
    OutputConfiguration,
)
from parsezen.domain.stages import StageKind, StageStatus


def _job(path: Path, job_id: str, order: int) -> DocumentJob:
    return DocumentJob.create(
        DocumentSource.inspect(path),
        JobConfiguration(
            output=OutputConfiguration(
                configured=True,
                format=DocumentFormat.MARKDOWN,
            ),
            ai=AIProfileConfiguration(model="old-model", context_window=4_096),
        ),
        order=order,
        job_id=job_id,
    )


def test_global_output_propagation_updates_every_editable_job(tmp_path: Path) -> None:
    inherited_path = tmp_path / "inherited.txt"
    custom_path = tmp_path / "custom.txt"
    inherited_path.write_text("One", encoding="utf-8")
    custom_path.write_text("Two", encoding="utf-8")
    inherited = _job(inherited_path, "inherited", 0)
    custom = _job(custom_path, "custom", 1)
    queue = JobQueue((inherited, custom))
    destination = tmp_path / "global-output"

    updated = QueueConfigurationService(queue).propagate_output_directory(None, destination)

    assert tuple(job.id for job in updated) == ("inherited", "custom")
    saved_inherited = queue.get("inherited")
    saved_custom = queue.get("custom")
    assert saved_inherited is not None
    assert saved_custom is not None
    assert saved_inherited.configuration.output.directory == destination
    assert saved_custom.configuration.output.directory == destination


def test_global_ai_propagation_updates_all_editable_profiles(tmp_path: Path) -> None:
    paths = tuple(tmp_path / name for name in ("inherited.txt", "custom.txt", "failed.txt"))
    for path in paths:
        path.write_text("Text", encoding="utf-8")
    inherited = _job(paths[0], "inherited", 0)
    custom = _job(paths[1], "custom", 1)
    failed = _job(paths[2], "failed", 2)
    failed = failed.replace_stage(
        failed.stage(StageKind.PREPARE)
        .transition(StageStatus.READY)
        .transition(StageStatus.RUNNING)
        .transition(StageStatus.FAILED, error_message="previous failure")
    )
    queue = JobQueue((inherited, custom, failed))
    current = AIProfileConfiguration(model="new-model", context_window=8_192)

    updated = QueueConfigurationService(queue).propagate_ai_profile(
        AIProfileConfiguration(model="old-model", context_window=4_096),
        current,
    )

    assert tuple(job.id for job in updated) == ("inherited", "custom", "failed")
    saved_inherited = queue.get("inherited")
    saved_custom = queue.get("custom")
    saved_failed = queue.get("failed")
    assert saved_inherited is not None
    assert saved_custom is not None
    assert saved_failed is not None
    assert saved_inherited.configuration.ai == current
    assert saved_custom.configuration.ai == current
    assert saved_failed.configuration.ai == current
