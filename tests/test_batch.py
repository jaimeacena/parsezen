from __future__ import annotations

from pathlib import Path

import parsezen.batch as batch_module
from parsezen.batch import BatchEntry, BatchQueue, BatchStatus, validate_batch_requests
from parsezen.processing import ProcessRequest
from parsezen.settings import AppSettings


def test_batch_queue_keeps_order_and_state_outside_the_ui(tmp_path: Path) -> None:
    first = BatchEntry(tmp_path / "first.txt")
    second = BatchEntry(tmp_path / "second.txt")
    queue = BatchQueue((first, second))

    moved = queue.pop(0)
    queue.insert(1, moved)
    first.status = BatchStatus.COMPLETED

    assert queue.paths == (second.path, first.path)
    assert queue.find(first.path) is first
    assert queue.next_pending() is second

    third = BatchEntry(tmp_path / "third.txt")
    queue.append(third)
    queue.extend((BatchEntry(tmp_path / "fourth.txt"),))
    assert len(queue) == 4
    queue.remove(third)
    queue.clear()
    assert not queue


def test_preflight_reports_all_invalid_documents_before_work_starts(tmp_path: Path) -> None:
    missing_one = tmp_path / "one.txt"
    missing_two = tmp_path / "two.txt"

    issues = validate_batch_requests(
        (ProcessRequest(missing_one, True), ProcessRequest(missing_two, True)),
        AppSettings(),
    )

    assert [issue.document_number for issue in issues] == [1, 2]
    assert all("No existe" in issue.message for issue in issues)


def test_preflight_checks_aggregate_output_space(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sources = (tmp_path / "one.txt", tmp_path / "two.txt")
    for source in sources:
        source.write_text("content", encoding="utf-8")
    monkeypatch.setattr(batch_module, "validate_process_request", lambda *_args: None)
    monkeypatch.setattr(
        batch_module.shutil,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": 1})(),
    )

    issues = validate_batch_requests(
        tuple(ProcessRequest(source, True) for source in sources),
        AppSettings(),
    )

    assert len(issues) == 1
    assert issues[0].document_number == 0
    assert "todos los resultados" in issues[0].message
