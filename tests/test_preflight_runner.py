from __future__ import annotations

from pathlib import Path
from threading import Event

from PySide6.QtCore import QTimer

import parsezen.presentation.preflight_runner as runner_module
from parsezen.application.run_preparation import PreparedQueueRun
from parsezen.application.scheduler import QueueRunPlan, RunMode
from parsezen.cancellation import CancellationToken
from parsezen.domain.jobs import DocumentJob, DocumentSource, JobConfiguration
from parsezen.presentation.preflight_runner import ForecastBatch, PreflightRunner


def test_queue_preparation_keeps_the_qt_event_loop_responsive(
    qtbot,
    monkeypatch,
) -> None:
    entered = Event()
    release = Event()
    plan = QueueRunPlan(RunMode.NONE, ())
    prepared = PreparedQueueRun(plan, (), ())

    def prepare(*_args, **_kwargs):
        entered.set()
        assert release.wait(timeout=2)
        return prepared

    monkeypatch.setattr(runner_module, "prepare_queue_run", prepare)
    runner = PreflightRunner()
    completed: list[object] = []
    runner.preparation_succeeded.connect(completed.append)

    assert runner.prepare(
        (),
        timeout_seconds=30,
        checkpoint_retention_days=30,
        metrics=(),
        plan=plan,
    )
    qtbot.waitUntil(entered.is_set, timeout=2_000)
    assert runner.preparing
    assert not runner.prepare(
        (),
        timeout_seconds=30,
        checkpoint_retention_days=30,
        metrics=(),
        plan=plan,
    )
    event_loop_tick: list[bool] = []
    QTimer.singleShot(0, lambda: event_loop_tick.append(True))
    qtbot.waitUntil(lambda: bool(event_loop_tick), timeout=1_000)

    release.set()
    qtbot.waitUntil(lambda: not runner.preparing, timeout=2_000)
    assert completed == [prepared]


def test_queue_preparation_reports_expected_failures(qtbot, monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        raise ValueError("Configuraci\u00f3n inv\u00e1lida")

    monkeypatch.setattr(runner_module, "prepare_queue_run", fail)
    runner = PreflightRunner()
    failures: list[str] = []
    runner.preparation_failed.connect(failures.append)

    runner.prepare(
        (),
        timeout_seconds=30,
        checkpoint_retention_days=30,
        metrics=(),
        plan=QueueRunPlan(RunMode.NONE, ()),
    )

    qtbot.waitUntil(lambda: not runner.preparing, timeout=2_000)
    assert failures == ["Configuraci\u00f3n inv\u00e1lida"]


def test_queue_preparation_can_be_cancelled_at_a_safe_boundary(qtbot, monkeypatch) -> None:
    entered = Event()

    def wait_for_cancellation(*_args, cancellation, **_kwargs):
        entered.set()
        assert cancellation._event.wait(timeout=2)  # noqa: SLF001
        cancellation.check()

    monkeypatch.setattr(runner_module, "prepare_queue_run", wait_for_cancellation)
    runner = PreflightRunner()
    cancelled: list[bool] = []
    runner.preparation_cancelled.connect(lambda: cancelled.append(True))
    runner.prepare(
        (),
        timeout_seconds=30,
        checkpoint_retention_days=30,
        metrics=(),
        plan=QueueRunPlan(RunMode.NONE, ()),
    )
    qtbot.waitUntil(entered.is_set, timeout=2_000)

    assert runner.cancel_preparation()
    qtbot.waitUntil(lambda: not runner.preparing, timeout=2_000)
    assert cancelled == [True]
    assert not runner.cancel_preparation()


def test_forecasts_are_computed_in_background_and_pdf_is_deferred(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    text_path = tmp_path / "ready.txt"
    failing_path = tmp_path / "failing.txt"
    pdf_path = tmp_path / "deferred.pdf"
    text_path.write_text("Ready", encoding="utf-8")
    failing_path.write_text("Failing", encoding="utf-8")
    pdf_path.write_bytes(b"%PDF")
    jobs = tuple(
        DocumentJob.create(
            DocumentSource.inspect(path),
            JobConfiguration(),
            order=index,
            job_id=job_id,
        )
        for index, (job_id, path) in enumerate(
            (("ready", text_path), ("failing", failing_path), ("pdf", pdf_path))
        )
    )
    original_mapping = runner_module.request_and_settings_from_job

    def map_job(job, **kwargs):
        if job.id == "failing":
            raise ValueError("invalid")
        return original_mapping(job, **kwargs)

    monkeypatch.setattr(runner_module, "request_and_settings_from_job", map_job)
    runner = PreflightRunner()
    batches: list[ForecastBatch] = []
    runner.forecasts_succeeded.connect(batches.append)

    assert runner.forecast(
        jobs,
        frozenset(job.id for job in jobs),
        timeout_seconds=30,
        checkpoint_retention_days=30,
        metrics=(),
        metrics_generation=4,
        excluded_keys=frozenset(),
    )

    qtbot.waitUntil(lambda: not runner.forecasting, timeout=2_000)
    assert len(batches) == 1
    assert batches[0].jobs == jobs
    assert batches[0].metrics_generation == 4
    assert [(result.job_id, result.forecast is None) for result in batches[0].results] == [
        ("ready", False),
        ("failing", True),
    ]


def test_preparation_worker_emits_the_prepared_value_directly(monkeypatch) -> None:
    plan = QueueRunPlan(RunMode.NONE, ())
    prepared = PreparedQueueRun(plan, (), ())
    monkeypatch.setattr(runner_module, "prepare_queue_run", lambda *_args, **_kwargs: prepared)
    worker = runner_module._PreparationWorker(  # noqa: SLF001
        (),
        timeout_seconds=30,
        checkpoint_retention_days=30,
        metrics=(),
        plan=plan,
        cancellation=CancellationToken(),
    )
    succeeded: list[object] = []
    finished: list[bool] = []
    worker.signals.succeeded.connect(succeeded.append)
    worker.signals.finished.connect(lambda: finished.append(True))

    worker.run()

    assert succeeded == [prepared]
    assert finished == [True]


def test_preparation_worker_uses_an_explicit_cancellation_signal(monkeypatch) -> None:
    def cancel(*_args, **_kwargs):
        raise runner_module.ProcessingCancelledError("cancelled")

    monkeypatch.setattr(runner_module, "prepare_queue_run", cancel)
    worker = runner_module._PreparationWorker(  # noqa: SLF001
        (),
        timeout_seconds=30,
        checkpoint_retention_days=30,
        metrics=(),
        plan=QueueRunPlan(RunMode.NONE, ()),
        cancellation=CancellationToken(),
    )
    cancelled: list[bool] = []
    failures: list[str] = []
    worker.signals.cancelled.connect(lambda: cancelled.append(True))
    worker.signals.failed.connect(failures.append)

    worker.run()

    assert cancelled == [True]
    assert failures == []


def test_forecast_worker_covers_success_failure_and_deferred_pdf_directly(
    tmp_path: Path,
    monkeypatch,
) -> None:
    text_path = tmp_path / "ready.txt"
    failing_path = tmp_path / "failing.txt"
    pdf_path = tmp_path / "deferred.pdf"
    text_path.write_text("Ready", encoding="utf-8")
    failing_path.write_text("Failing", encoding="utf-8")
    pdf_path.write_bytes(b"%PDF")
    jobs = tuple(
        DocumentJob.create(
            DocumentSource.inspect(path),
            JobConfiguration(),
            order=index,
            job_id=job_id,
        )
        for index, (job_id, path) in enumerate(
            (("ready", text_path), ("failing", failing_path), ("pdf", pdf_path))
        )
    )
    original_mapping = runner_module.request_and_settings_from_job

    def map_job(job, **kwargs):
        if job.id == "failing":
            raise ValueError("invalid")
        return original_mapping(job, **kwargs)

    monkeypatch.setattr(runner_module, "request_and_settings_from_job", map_job)
    worker = runner_module._ForecastWorker(  # noqa: SLF001
        jobs,
        frozenset(job.id for job in jobs),
        timeout_seconds=30,
        checkpoint_retention_days=30,
        metrics=(),
        metrics_generation=7,
        excluded_keys=frozenset(),
    )
    batches: list[ForecastBatch] = []
    worker.signals.succeeded.connect(batches.append)

    worker.run()

    assert [(result.job_id, result.forecast is None) for result in batches[0].results] == [
        ("ready", False),
        ("failing", True),
    ]


def test_forecast_worker_recovers_from_an_unexpected_mapping_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    text_path = tmp_path / "failing.txt"
    text_path.write_text("Failing", encoding="utf-8")
    job = DocumentJob.create(
        DocumentSource.inspect(text_path),
        JobConfiguration(),
        order=0,
        job_id="failing",
    )

    def fail_mapping(*_args, **_kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(runner_module, "request_and_settings_from_job", fail_mapping)
    worker = runner_module._ForecastWorker(  # noqa: SLF001
        (job,),
        frozenset({job.id}),
        timeout_seconds=30,
        checkpoint_retention_days=30,
        metrics=(),
        metrics_generation=1,
        excluded_keys=frozenset(),
    )
    batches: list[ForecastBatch] = []
    finished: list[bool] = []
    worker.signals.succeeded.connect(batches.append)
    worker.signals.finished.connect(lambda: finished.append(True))

    worker.run()

    assert batches[0].results[0].forecast is None
    assert finished == [True]
