from dataclasses import replace
from pathlib import Path

import pytest

from parsezen.application.job_queue import JobQueue
from parsezen.application.planner import activate_next_stage
from parsezen.domain.jobs import (
    DocumentFormat,
    DocumentJob,
    DocumentSource,
    JobConfiguration,
    ProcessingPlan,
)
from parsezen.domain.stages import StageKind, StageStatus


def source(name: str, *, size: int = 100, modified: int = 1) -> DocumentSource:
    return DocumentSource(Path(name), DocumentFormat.PDF, size, modified)


def test_job_queue_owns_unique_identity_configuration_and_order() -> None:
    queue = JobQueue()
    first = queue.add(source("one.pdf"), JobConfiguration(), job_id="one")
    second = queue.add(source("two.pdf"), JobConfiguration(), job_id="two")

    configured = queue.configure(
        second.id,
        replace(
            second.configuration,
            plan=ProcessingPlan.LOCAL_AI_REVIEWED,
        ),
    )
    queue.move(second.id, 0)

    assert queue.jobs[0].id == second.id
    assert queue.jobs[1].id == first.id
    assert tuple(job.order for job in queue.jobs) == (0, 1)
    assert configured.configuration_revision == 2
    assert queue.get(second.id) is not None
    assert queue.get(second.id).configuration.plan is ProcessingPlan.LOCAL_AI_REVIEWED
    assert queue.for_source(Path("two.pdf")).id == second.id


def test_job_queue_rejects_duplicate_sources_and_identifiers() -> None:
    queue = JobQueue()
    first = queue.add(source("one.pdf"), JobConfiguration(), job_id="one")
    second = queue.add(source("two.pdf"), JobConfiguration(), job_id="two")

    with pytest.raises(ValueError):
        queue.add(source("one.pdf"), JobConfiguration(), job_id="other")
    with pytest.raises(ValueError):
        queue.replace(replace(second, order=first.order))
    with pytest.raises(ValueError):
        queue.restore(
            (
                DocumentJob.create(
                    source("one.pdf"),
                    JobConfiguration(),
                    order=0,
                    job_id="same",
                ),
                DocumentJob.create(
                    source("two.pdf"),
                    JobConfiguration(),
                    order=1,
                    job_id="same",
                ),
            )
        )


def test_job_queue_refreshes_only_safe_source_snapshots() -> None:
    queue = JobQueue()
    queued = queue.add(source("one.pdf"), JobConfiguration(), job_id="one")

    refreshed = queue.refresh_source(
        queued.id,
        source("one.pdf", size=200, modified=2),
    )

    assert refreshed.source.size_bytes == 200
    assert refreshed.id == queued.id
    assert refreshed.configuration_revision == queued.configuration_revision

    ready = activate_next_stage(refreshed)
    running = ready.replace_stage(ready.stage(StageKind.PREPARE).transition(StageStatus.RUNNING))
    queue.replace(running)

    with pytest.raises(ValueError):
        queue.refresh_source(
            running.id,
            source("one.pdf", size=300, modified=3),
        )


def test_job_queue_remove_returns_job_and_compacts_remaining_order() -> None:
    queue = JobQueue(
        (
            DocumentJob.create(
                source("one.pdf"),
                JobConfiguration(),
                order=4,
                job_id="one",
            ),
            DocumentJob.create(
                source("two.pdf"),
                JobConfiguration(),
                order=8,
                job_id="two",
            ),
        )
    )

    removed = queue.remove("one")

    assert removed.id == "one"
    assert tuple((job.id, job.order) for job in queue.jobs) == (("two", 0),)
    assert queue.for_source(Path("one.pdf")) is None


def test_job_queue_retains_only_sources_still_present_in_the_executor() -> None:
    queue = JobQueue()
    queue.add(source("one.pdf"), JobConfiguration(), job_id="one")
    queue.add(source("two.pdf"), JobConfiguration(), job_id="two")

    retained = queue.retain_sources((Path("two.pdf"),))

    assert tuple(job.id for job in retained) == ("two",)
    assert retained[0].order == 0
