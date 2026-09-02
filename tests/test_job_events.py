from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from parsezen.domain.job_events import (
    JOB_EVENT_PAYLOAD_VERSION,
    JobEvent,
    JobEventKind,
)
from parsezen.domain.jobs import DocumentJob, DocumentSource, JobConfiguration
from parsezen.domain.stages import StageKind, StageStatus
from parsezen.infrastructure.state_store import StateStore


def _job(tmp_path: Path) -> DocumentJob:
    source = tmp_path / "private-source.txt"
    source.write_text("content that must never enter job events", encoding="utf-8")
    return DocumentJob.create(
        DocumentSource.inspect(source),
        JobConfiguration(),
        order=0,
        job_id="audit-job",
    )


def test_state_store_round_trips_versioned_content_free_stage_events(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    store = StateStore(database)
    job = _job(tmp_path)
    event = JobEvent(
        job.id,
        JobEventKind.STAGE_TRANSITION,
        job.configuration_revision,
        stage=StageKind.TRANSLATE,
        from_status=StageStatus.RUNNING,
        to_status=StageStatus.FAILED,
        attempt_id="b" * 32,
        error_code="translation_failed",
    )

    store.replace_jobs((job,), events=(event,))

    assert store.load_job_events(job.id) == (event,)
    with sqlite3.connect(database) as connection:
        payload_text = connection.execute(
            "SELECT payload FROM job_events WHERE job_id = ?",
            (job.id,),
        ).fetchone()[0]
    payload = json.loads(payload_text)
    assert payload == {
        "attempt_id": "b" * 32,
        "configuration_revision": 1,
        "error_code": "translation_failed",
        "from_status": "running",
        "payload_version": JOB_EVENT_PAYLOAD_VERSION,
        "snapshot_generation": None,
        "stage": "translate",
        "to_status": "failed",
    }
    assert "content that must never enter" not in payload_text
    assert "private-source.txt" not in payload_text


def test_snapshot_pointer_records_its_generation_as_a_typed_event(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    job = _job(tmp_path)
    store.replace_jobs((job,))

    store.save_result_snapshot(job.id, "gen-0123456789abcdef")

    assert store.load_job_events(job.id) == (
        JobEvent(
            job.id,
            JobEventKind.SNAPSHOT_SAVED,
            job.configuration_revision,
            snapshot_generation="gen-0123456789abcdef",
        ),
    )


def test_job_events_reject_unversioned_or_incomplete_audit_payloads() -> None:
    with pytest.raises(ValueError, match="version"):
        JobEvent(
            "audit-job",
            JobEventKind.CONFIGURATION_CHANGED,
            2,
            payload_version=0,
        )
    with pytest.raises(ValueError, match="stage and both statuses"):
        JobEvent(
            "audit-job",
            JobEventKind.STAGE_TRANSITION,
            1,
            stage=StageKind.PREPARE,
        )
    with pytest.raises(ValueError, match="attempt id"):
        JobEvent(
            "audit-job",
            JobEventKind.CONFIGURATION_CHANGED,
            2,
            attempt_id="document text is not an opaque id",
        )
