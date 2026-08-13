from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from parsezen.domain.attempt_activity import (
    AttemptEvent,
    AttemptEventStatus,
    AttemptPhase,
    AttemptTimeline,
    FailureSnapshot,
    ReusableWork,
    durable_failure_message,
)
from parsezen.domain.outcomes import OutcomeSummary
from parsezen.recent_activity import (
    MAX_RECENT_JOBS,
    RecentJob,
    RecentJobStatus,
    append_recent_jobs,
    clear_recent_jobs,
    load_recent_jobs,
)


def test_recent_activity_is_bounded_deduplicated_and_clearable(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    now = datetime.now(UTC)
    jobs = tuple(
        RecentJob(
            tmp_path / f"document-{index}.pdf",
            RecentJobStatus.COMPLETED,
            now - timedelta(minutes=index),
            tmp_path / f"document-{index}.md",
        )
        for index in range(MAX_RECENT_JOBS + 5)
    )

    append_recent_jobs(jobs, path=path)
    append_recent_jobs((jobs[0],), path=path)

    restored = load_recent_jobs(path=path)
    assert len(restored) == MAX_RECENT_JOBS
    assert restored.count(jobs[0]) == 1
    clear_recent_jobs(path=path)
    assert load_recent_jobs(path=path) == ()


def test_recent_activity_round_trips_content_free_summary(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    job = RecentJob(
        tmp_path / "book.pdf",
        RecentJobStatus.COMPLETED,
        datetime.now(UTC),
        tmp_path / "book.epub",
        OutcomeSummary(
            output_format="EPUB",
            operations=("Conversión", "Edición EPUB"),
            processed_pages=50,
            preserved_images=3,
            integrity_verified=True,
            integrity_checks=3,
            duration_seconds=600,
            ai_review_recommended=True,
            ai_review_blocks=2,
            ai_review_signals=3,
            linguistic_review_mode="independent_bilingual",
            translation_checked_blocks=14,
            translation_reviewed_blocks=12,
            translation_independent_blocks=12,
            translation_unreviewed_blocks=2,
        ),
    )

    append_recent_jobs((job,), path=path)

    assert load_recent_jobs(path=path) == (job,)
    assert "processed_pages" in path.read_text(encoding="utf-8")


def test_recent_failure_sanitizes_document_text_from_the_trace(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    timeline = AttemptTimeline.from_events(
        (
            AttemptEvent(AttemptPhase.PREPARATION, AttemptEventStatus.STARTED),
            AttemptEvent(AttemptPhase.FAILURE, AttemptEventStatus.FAILED),
        )
    )
    job = RecentJob(
        tmp_path / "book.pdf",
        RecentJobStatus.FAILED,
        datetime.now(UTC),
        attempt_id="a" * 32,
        timeline=timeline,
        failure=FailureSnapshot(
            AttemptPhase.TRANSLATION,
            "transformation",
            'El término confidencial "PROYECTO-ALFA" provocó el error.',
            "deadbeef",
            ReusableWork.PREVIOUS_PHASES,
        ),
    )

    append_recent_jobs((job,), path=path)

    restored = load_recent_jobs(path=path)
    assert restored[0].failure is not None
    assert restored[0].failure.message == durable_failure_message("transformation")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 3
    assert "PROYECTO-ALFA" not in path.read_text(encoding="utf-8")
