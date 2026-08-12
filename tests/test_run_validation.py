from __future__ import annotations

from pathlib import Path

import parsezen.application.run_validation as validation_module
from parsezen.application.run_validation import validate_batch_requests
from parsezen.processing import ProcessRequest
from parsezen.settings import AppSettings


def test_run_validation_reports_all_invalid_documents_before_work_starts(
    tmp_path: Path,
) -> None:
    missing_one = tmp_path / "one.txt"
    missing_two = tmp_path / "two.txt"

    issues = validate_batch_requests(
        (ProcessRequest(missing_one, True), ProcessRequest(missing_two, True)),
        AppSettings(),
    )

    assert [issue.document_number for issue in issues] == [1, 2]
    assert all("No existe" in issue.message for issue in issues)


def test_run_validation_checks_aggregate_output_space(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sources = (tmp_path / "one.txt", tmp_path / "two.txt")
    for source in sources:
        source.write_text("content", encoding="utf-8")
    monkeypatch.setattr(validation_module, "validate_process_request", lambda *_args: None)
    monkeypatch.setattr(
        validation_module.shutil,
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
