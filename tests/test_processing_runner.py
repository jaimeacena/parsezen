from __future__ import annotations

import logging
import re
from datetime import UTC
from pathlib import Path
from threading import Event

import pytest

from parsezen.cancellation import CancellationToken
from parsezen.domain.attempt_activity import (
    AttemptEventStatus,
    AttemptPhase,
    durable_failure_message,
)
from parsezen.domain.outcomes import EarlyCheckReport
from parsezen.errors import ConversionError, ProcessingCancelledError
from parsezen.failure_recovery import FailureKind, ProcessingFailure
from parsezen.presentation.processing_runner import ProcessingRunner, ProcessingWorker
from parsezen.processing import ProcessRequest, ProcessResult, ProcessStage
from parsezen.settings import AppSettings


def test_runner_forwards_events_and_releases_the_worker(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    destination = tmp_path / "notes.md"
    source.write_text("Notes", encoding="utf-8")
    runner = ProcessingRunner()
    stages: list[ProcessStage] = []
    progress: list[tuple[int, int]] = []
    results: list[ProcessResult] = []
    finished: list[bool] = []
    runner.stage_changed.connect(stages.append)
    runner.improvement_progress.connect(lambda current, total: progress.append((current, total)))
    runner.succeeded.connect(results.append)
    runner.finished.connect(lambda: finished.append(True))

    def processor(
        request,
        *,
        on_stage,
        on_progress,
        settings,
        cancellation,
    ) -> ProcessResult:
        assert request.source_path == source
        assert settings == AppSettings()
        assert cancellation is not None
        on_stage(ProcessStage.READING)
        on_progress(1, 2)
        destination.write_text("Converted", encoding="utf-8")
        return ProcessResult(destination)

    runner.start(
        ProcessRequest(source, convert_to_markdown=False),
        AppSettings(),
        processor=processor,
    )
    qtbot.waitUntil(lambda: not runner.is_active, timeout=2_000)

    assert stages == [ProcessStage.READING]
    assert progress == [(1, 2)]
    assert results == [ProcessResult(destination)]
    assert finished == [True]
    assert runner.worker is None
    assert runner.cancellation is None


def test_runner_ignores_an_invalid_cross_thread_stage_payload() -> None:
    runner = ProcessingRunner()
    stages: list[object] = []
    runner.stage_changed.connect(stages.append)

    runner._worker_stage_changed("reading")  # noqa: SLF001

    assert stages == []
    assert runner.timeline.events == ()


def test_runner_generates_one_opaque_attempt_id_and_retains_timeline(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    destination = tmp_path / "notes.md"
    source.write_text("Notes", encoding="utf-8")
    received_attempt_ids: list[str] = []
    runner = ProcessingRunner()

    def processor(
        request,
        *,
        on_stage,
        on_progress,
        settings,
        cancellation,
        attempt_id,
    ) -> ProcessResult:
        del request, on_progress, settings, cancellation
        received_attempt_ids.append(attempt_id)
        on_stage(ProcessStage.READING)
        destination.write_text("Converted", encoding="utf-8")
        return ProcessResult(destination)

    runner.start(
        ProcessRequest(source, convert_to_markdown=False),
        AppSettings(),
        processor=processor,
    )
    qtbot.waitUntil(lambda: not runner.is_active, timeout=2_000)

    assert runner.attempt_id is not None
    assert re.fullmatch(r"[0-9a-f]{32}", runner.attempt_id)
    assert received_attempt_ids == [runner.attempt_id]
    assert runner.timeline.events[0].phase is AttemptPhase.PREPARATION
    assert runner.timeline.events[0].status is AttemptEventStatus.STARTED
    assert runner.timeline.events[-1].phase is AttemptPhase.COMPLETION
    assert runner.timeline.events[-1].status is AttemptEventStatus.COMPLETED


def test_runner_deduplicates_physical_stages_and_completes_each_phase(
    qtbot,
    tmp_path: Path,
) -> None:
    source = tmp_path / "notes.txt"
    destination = tmp_path / "notes.md"
    source.write_text("Notes", encoding="utf-8")
    runner = ProcessingRunner()

    def processor(_request, *, on_stage, **_arguments) -> ProcessResult:
        for stage in (
            ProcessStage.READING,
            ProcessStage.CONVERTING,
            ProcessStage.TRANSLATING,
            ProcessStage.IMPROVING,
            ProcessStage.ORGANIZING_STRUCTURE,
            ProcessStage.WRITING,
            ProcessStage.COMPLETED,
        ):
            on_stage(stage)
        destination.write_text("Converted", encoding="utf-8")
        return ProcessResult(destination)

    runner.start(
        ProcessRequest(source, convert_to_markdown=False),
        AppSettings(),
        processor=processor,
    )
    qtbot.waitUntil(lambda: not runner.is_active, timeout=2_000)

    assert [(event.phase, event.status) for event in runner.timeline.events] == [
        (AttemptPhase.PREPARATION, AttemptEventStatus.STARTED),
        (AttemptPhase.PREPARATION, AttemptEventStatus.COMPLETED),
        (AttemptPhase.TRANSLATION, AttemptEventStatus.STARTED),
        (AttemptPhase.TRANSLATION, AttemptEventStatus.COMPLETED),
        (AttemptPhase.CORRECTION, AttemptEventStatus.STARTED),
        (AttemptPhase.CORRECTION, AttemptEventStatus.COMPLETED),
        (AttemptPhase.PERSONALIZATION, AttemptEventStatus.STARTED),
        (AttemptPhase.PERSONALIZATION, AttemptEventStatus.COMPLETED),
        (AttemptPhase.PUBLICATION, AttemptEventStatus.STARTED),
        (AttemptPhase.PUBLICATION, AttemptEventStatus.COMPLETED),
        (AttemptPhase.COMPLETION, AttemptEventStatus.STARTED),
        (AttemptPhase.COMPLETION, AttemptEventStatus.COMPLETED),
    ]
    assert all(event.timestamp.tzinfo is UTC for event in runner.timeline.events)


def test_runner_failure_retains_the_real_failed_phase(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Notes", encoding="utf-8")
    runner = ProcessingRunner()

    def processor(_request, *, on_stage, **_arguments) -> ProcessResult:
        on_stage(ProcessStage.TRANSLATING)
        raise ConversionError('No se pudo traducir el t\u00e9rmino confidencial "PROYECTO-ALFA".')

    runner.start(
        ProcessRequest(source, convert_to_markdown=False),
        AppSettings(),
        processor=processor,
    )
    qtbot.waitUntil(lambda: not runner.is_active, timeout=2_000)

    assert runner.failure_snapshot is not None
    assert runner.failure_snapshot.phase is AttemptPhase.TRANSLATION
    assert runner.failure_snapshot.message == durable_failure_message("source")
    assert "PROYECTO-ALFA" not in runner.failure_snapshot.message
    assert runner.timeline.events[-1].phase is AttemptPhase.TRANSLATION
    assert runner.timeline.events[-1].status is AttemptEventStatus.FAILED
    assert all(event.phase is not AttemptPhase.FAILURE for event in runner.timeline.events)


def test_runner_cancels_cooperatively_and_rejects_parallel_start(
    qtbot,
    tmp_path: Path,
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Notes", encoding="utf-8")
    started = Event()
    runner = ProcessingRunner()
    cancelled: list[bool] = []
    runner.cancelled.connect(lambda: cancelled.append(True))

    def processor(
        _request,
        *,
        on_stage,
        on_progress,
        settings,
        cancellation,
    ) -> ProcessResult:
        del on_stage, on_progress, settings
        started.set()
        assert cancellation._event.wait(timeout=2)  # noqa: SLF001
        cancellation.check()
        raise AssertionError("Cancellation must stop the worker.")

    request = ProcessRequest(source, convert_to_markdown=False)
    runner.start(request, AppSettings(), processor=processor)
    qtbot.waitUntil(started.is_set, timeout=2_000)

    with pytest.raises(RuntimeError, match="already active"):
        runner.start(request, AppSettings(), processor=processor)
    assert runner.cancel()
    assert not runner.cancel()
    qtbot.waitUntil(lambda: not runner.is_active, timeout=2_000)

    assert cancelled == [True]
    assert runner.timeline.events[-1].phase is AttemptPhase.PREPARATION
    assert runner.timeline.events[-1].status is AttemptEventStatus.CANCELLED


def test_runner_pause_terminates_the_active_phase_as_paused(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Notes", encoding="utf-8")
    started = Event()
    runner = ProcessingRunner()

    def processor(
        _request,
        *,
        on_stage,
        on_progress,
        settings,
        cancellation,
    ) -> ProcessResult:
        del on_progress, settings
        on_stage(ProcessStage.TRANSLATING)
        started.set()
        assert cancellation._event.wait(timeout=2)  # noqa: SLF001
        cancellation.check()
        raise AssertionError("Pause must stop the worker.")

    runner.start(
        ProcessRequest(source, convert_to_markdown=False),
        AppSettings(),
        processor=processor,
    )
    qtbot.waitUntil(started.is_set, timeout=2_000)

    assert runner.pause()
    qtbot.waitUntil(lambda: not runner.is_active, timeout=2_000)

    assert runner.timeline.events[-1].phase is AttemptPhase.TRANSLATION
    assert runner.timeline.events[-1].status is AttemptEventStatus.PAUSED


@pytest.mark.parametrize(
    ("error", "expected_message", "expected_log"),
    (
        (
            ConversionError("No se pudo convertir."),
            "No se pudo convertir.",
            "processing_attempt_task_failed error_type=ConversionError",
        ),
        (
            RuntimeError("C:/private/document.txt"),
            "Se produjo un error inesperado durante el procesamiento.",
            "unexpected_processing_attempt_failure error_type=RuntimeError",
        ),
    ),
)
def test_runner_classifies_failures_without_logging_private_details(
    qtbot,
    tmp_path: Path,
    caplog,
    error: Exception,
    expected_message: str,
    expected_log: str,
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Notes", encoding="utf-8")
    runner = ProcessingRunner()
    failures: list[ProcessingFailure] = []
    runner.failed.connect(failures.append)

    def processor(_request, **_arguments):
        raise error

    runner.start(
        ProcessRequest(source, convert_to_markdown=False),
        AppSettings(),
        processor=processor,
    )
    qtbot.waitUntil(lambda: not runner.is_active, timeout=2_000)

    assert len(failures) == 1
    assert failures[0].message == expected_message
    assert failures[0].error_type == type(error).__name__
    assert expected_log in caplog.text
    assert "C:/private/document.txt" not in caplog.text


def test_blocking_early_check_prevents_the_full_processor(
    qtbot,
    tmp_path: Path,
    caplog,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"pdf")
    runner = ProcessingRunner()
    processor_called: list[bool] = []
    started: list[bool] = []
    completed: list[EarlyCheckReport] = []
    failures: list[ProcessingFailure] = []
    runner.early_check_started.connect(lambda: started.append(True))
    runner.early_check_completed.connect(completed.append)
    runner.failed.connect(failures.append)

    def early_check(_request, _settings, **_arguments):
        return EarlyCheckReport(
            (1, 50, 100),
            blocking_reasons=("Revisa el OCR antes de continuar.",),
        )

    def processor(_request, **_arguments):
        processor_called.append(True)
        return ProcessResult(tmp_path / "unexpected.md")

    with caplog.at_level(logging.INFO, logger="parsezen.presentation.processing_runner"):
        runner.start(
            ProcessRequest(source, convert_to_markdown=True),
            AppSettings(),
            processor=processor,
            early_check=early_check,
        )
    qtbot.waitUntil(lambda: not runner.is_active, timeout=2_000)

    assert started == [True]
    assert len(completed) == 1
    assert completed[0].blocking
    assert not processor_called
    assert failures[0].kind is FailureKind.EARLY_CHECK
    assert runner.failure_snapshot is not None
    assert runner.failure_snapshot.phase is AttemptPhase.EARLY_CHECK
    assert runner.timeline.events[-1].phase is AttemptPhase.EARLY_CHECK
    assert runner.timeline.events[-1].status is AttemptEventStatus.FAILED
    terminal = [
        record.message for record in caplog.records if "processing_attempt_failed" in record.message
    ]
    assert terminal
    assert f"attempt_id={runner.attempt_id}" in terminal[-1]
    assert "phase=early_check" in terminal[-1]
    assert "error_code=early_check" in terminal[-1]
    assert "error_type=EarlyCheckError" in terminal[-1]


def test_safe_early_check_continues_with_the_full_processor(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "book.pdf"
    destination = tmp_path / "book.md"
    source.write_bytes(b"pdf")
    runner = ProcessingRunner()
    calls: list[str] = []
    reports: list[EarlyCheckReport] = []
    results: list[ProcessResult] = []
    runner.early_check_completed.connect(reports.append)
    runner.succeeded.connect(results.append)

    def early_check(_request, _settings, **_arguments):
        calls.append("early")
        return EarlyCheckReport((1, 50, 100))

    def processor(_request, **_arguments):
        calls.append("full")
        destination.write_text("Result", encoding="utf-8")
        return ProcessResult(destination)

    runner.start(
        ProcessRequest(source, convert_to_markdown=True),
        AppSettings(),
        processor=processor,
        early_check=early_check,
    )
    qtbot.waitUntil(lambda: not runner.is_active, timeout=2_000)

    assert calls == ["early", "full"]
    assert len(reports) == 1
    assert not reports[0].blocking
    assert results[0].final_path == destination


@pytest.mark.parametrize(
    ("outcome", "expected_signal"),
    (
        ("success", "succeeded"),
        ("cancel", "cancelled"),
        ("known-error", "failed"),
        ("unexpected-error", "failed"),
    ),
)
def test_worker_terminal_paths_are_deterministic_on_the_calling_thread(
    tmp_path: Path,
    outcome: str,
    expected_signal: str,
) -> None:
    source = tmp_path / f"{outcome}.txt"
    destination = tmp_path / f"{outcome}.md"
    source.write_text("Source", encoding="utf-8")

    def processor(
        _request,
        *,
        work_checkpoint_root,
        **_arguments,
    ) -> ProcessResult:
        assert work_checkpoint_root == tmp_path / "checkpoints"
        if outcome == "cancel":
            raise ProcessingCancelledError("Cancelado")
        if outcome == "known-error":
            raise ConversionError("Conversión fallida")
        if outcome == "unexpected-error":
            raise RuntimeError("private detail")
        destination.write_text("Result", encoding="utf-8")
        return ProcessResult(destination)

    worker = ProcessingWorker(
        ProcessRequest(source, convert_to_markdown=False),
        AppSettings(),
        CancellationToken(),
        tmp_path / "checkpoints",
        processor=processor,
    )
    emitted: list[str] = []
    worker.signals.succeeded.connect(lambda _result: emitted.append("succeeded"))
    worker.signals.failed.connect(lambda _message: emitted.append("failed"))
    worker.signals.cancelled.connect(lambda: emitted.append("cancelled"))
    worker.signals.finished.connect(lambda: emitted.append("finished"))

    worker.run()

    assert emitted == [expected_signal, "finished"]
