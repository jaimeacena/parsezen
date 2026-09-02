from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from parsezen.application.job_queue import JobQueue
from parsezen.application.queue_configuration import QueueConfigurationService
from parsezen.domain.jobs import (
    AIProfileConfiguration,
    CoverStrategy,
    DocumentFormat,
    DocumentJob,
    DocumentSource,
    JobConfiguration,
    OutputConfiguration,
    PageRangeConfiguration,
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


def test_compatible_pdf_batch_preserves_ranges_and_skips_other_formats(tmp_path: Path) -> None:
    paths = tuple(tmp_path / name for name in ("source.pdf", "second.pdf", "notes.txt"))
    for path in paths:
        path.write_text("Text", encoding="utf-8")
    source = _job(paths[0], "source", 0)
    second = _job(paths[1], "second", 1).with_configuration(
        replace(
            _job(paths[1], "unused", 1).configuration,
            page_range=PageRangeConfiguration(20, 40),
            force_pdf_ocr=True,
        )
    )
    notes = _job(paths[2], "notes", 2)
    queue = JobQueue((source, second, notes))
    shared = replace(
        source.configuration,
        page_range=PageRangeConfiguration(1, 5),
        force_pdf_ocr=False,
        ai=AIProfileConfiguration(model="shared:7b", context_window=8_192),
    )
    service = QueueConfigurationService(queue)

    assert service.compatible_job_count(source) == 1
    updates = service.apply_to_compatible_jobs(
        source,
        shared,
        timeout_seconds=30,
        checkpoint_retention_days=7,
    )

    assert tuple(update.job.id for update in updates) == ("second",)
    updated = queue.get("second")
    untouched = queue.get("notes")
    assert updated is not None
    assert untouched is not None
    assert updated.configuration.page_range == PageRangeConfiguration(20, 40)
    assert updated.configuration.force_pdf_ocr
    assert updated.configuration.ai == shared.ai
    assert untouched.configuration == notes.configuration


def test_compatible_epub_batch_preserves_each_title_author_and_cover(tmp_path: Path) -> None:
    source_path = tmp_path / "source.epub"
    candidate_path = tmp_path / "candidate.epub"
    cover = tmp_path / "candidate-cover.png"
    source_path.write_bytes(b"source")
    candidate_path.write_bytes(b"candidate")
    cover.write_bytes(b"cover")
    source = _job(source_path, "source", 0)
    candidate = _job(candidate_path, "candidate", 1).with_configuration(
        replace(
            _job(candidate_path, "unused", 1).configuration,
            output=OutputConfiguration(
                format=DocumentFormat.EPUB,
                title="Título propio",
                author="Autora propia",
                cover_strategy=CoverStrategy.CUSTOM,
                cover_path=cover,
            ),
        )
    )
    queue = JobQueue((source, candidate))
    shared = replace(
        source.configuration,
        output=OutputConfiguration(
            format=DocumentFormat.EPUB,
            title="Título de origen",
            author="Autor de origen",
            cover_strategy=CoverStrategy.FIRST_PAGE,
        ),
        ai=AIProfileConfiguration(model="shared:7b"),
    )

    QueueConfigurationService(queue).apply_to_compatible_jobs(
        source,
        shared,
        timeout_seconds=30,
        checkpoint_retention_days=7,
    )

    updated = queue.get("candidate")
    assert updated is not None
    assert updated.configuration.ai == shared.ai
    assert updated.configuration.output.title == "Título propio"
    assert updated.configuration.output.author == "Autora propia"
    assert updated.configuration.output.cover_strategy is CoverStrategy.CUSTOM
    assert updated.configuration.output.cover_path == cover


def test_compatible_batch_updates_failed_but_not_running_or_completed(tmp_path: Path) -> None:
    paths = tuple(tmp_path / f"{name}.txt" for name in ("source", "failed", "running", "done"))
    for path in paths:
        path.write_text("Text", encoding="utf-8")
    source, failed, running, completed = (
        _job(path, name, order)
        for order, (path, name) in enumerate(
            zip(paths, ("source", "failed", "running", "done"), strict=True)
        )
    )
    failed = failed.replace_stage(
        failed.stage(StageKind.PREPARE)
        .transition(StageStatus.READY)
        .transition(StageStatus.RUNNING)
        .transition(StageStatus.FAILED, error_message="failure")
    )
    running = running.replace_stage(
        running.stage(StageKind.PREPARE)
        .transition(StageStatus.READY)
        .transition(StageStatus.RUNNING)
    )
    for kind in (StageKind.PREPARE, StageKind.PUBLISH):
        completed = completed.replace_stage(
            completed.stage(kind)
            .transition(StageStatus.READY)
            .transition(StageStatus.RUNNING)
            .transition(StageStatus.COMPLETED)
        )
    queue = JobQueue((source, failed, running, completed))
    shared = replace(source.configuration, ai=AIProfileConfiguration(model="shared:7b"))
    service = QueueConfigurationService(queue)

    assert service.compatible_job_count(source) == 1
    updates = service.apply_to_compatible_jobs(
        source,
        shared,
        timeout_seconds=30,
        checkpoint_retention_days=7,
    )

    assert tuple(update.job.id for update in updates) == ("failed",)
    assert queue.get("failed").configuration.ai == shared.ai  # type: ignore[union-attr]
    assert queue.get("running") == running
    assert queue.get("done") == completed
