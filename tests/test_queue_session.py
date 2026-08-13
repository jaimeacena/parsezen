from pathlib import Path

import pytest

from parsezen.application.job_queue import JobQueue
from parsezen.application.queue_session import QueueSession, SessionTermination
from parsezen.application.run_preparation import PreparedQueueRun
from parsezen.application.scheduler import QueueRunPlan, RunMode
from parsezen.domain.jobs import (
    DocumentFormat,
    DocumentSource,
    JobConfiguration,
    OutputConfiguration,
)


def _session_with_jobs(*identifiers: str) -> QueueSession:
    queue = JobQueue()
    for identifier in identifiers:
        queue.add(
            DocumentSource(Path(f"{identifier}.txt"), DocumentFormat.TEXT, 10, 1),
            JobConfiguration(
                output=OutputConfiguration(
                    format=DocumentFormat.MARKDOWN,
                    configured=True,
                )
            ),
            job_id=identifier,
        )
    session = QueueSession(queue)
    session.retain_runtime(identifiers)
    session.set_prepared_run(PreparedQueueRun(QueueRunPlan(RunMode.NEW, identifiers), (), ()))
    return session


def test_session_owns_run_start_selection_and_completion() -> None:
    session = _session_with_jobs("one", "two")

    assert session.begin_prepared_run() == ("one", "two")
    first = session.claim_next()
    assert first is not None
    assert first[0].id == "one"
    assert session.current_job is first[0]
    assert session.current_runtime is first[1]
    assert first[1].started_at is not None

    session.execution.complete("one", Path("one.md"))
    session.release_current()
    second = session.claim_next()
    assert second is not None and second[0].id == "two"
    session.execution.complete("two", Path("two.md"))

    assert session.finish() == ("one", "two")
    assert not session.running
    assert session.current_job is None
    assert session.prepared_run is None
    assert session.last_termination is SessionTermination.COMPLETED


def test_pause_is_a_session_termination_and_prevents_the_next_claim() -> None:
    session = _session_with_jobs("one", "two")
    session.begin_prepared_run()
    current = session.claim_next()
    assert current is not None

    assert session.request_pause()
    assert not session.has_next()
    assert session.claim_next() is None
    assert session.finish() == ("one", "two")
    assert session.last_termination is SessionTermination.PAUSED


def test_rejected_pause_restores_the_active_session() -> None:
    session = _session_with_jobs("one")
    session.begin_prepared_run()

    assert session.request_pause()
    session.reject_pause()

    assert not session.pause_requested
    assert session.claim_next() is not None


def test_session_rejects_invalid_or_concurrent_runs() -> None:
    session = QueueSession()
    with pytest.raises(ValueError, match="valid prepared"):
        session.begin_prepared_run()

    session = _session_with_jobs("one")
    session.begin_prepared_run()
    with pytest.raises(RuntimeError, match="already active"):
        session.begin_prepared_run()
    with pytest.raises(RuntimeError, match="session is active"):
        session.set_prepared_run(None)
