from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from unittest.mock import Mock

from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QMainWindow,
    QMessageBox,
    QSystemTrayIcon,
)

import parsezen.presentation.main_window as main_window_module
from parsezen.application.job_execution import JobExecutionController
from parsezen.application.job_queue import JobQueue
from parsezen.application.job_runtime import JobRuntime
from parsezen.application.quality_review_adapter import create_translation_review
from parsezen.application.review_materialization import (
    phase_plan_with_materialized_reviews,
    review_for_current_candidate,
)
from parsezen.application.revision_materializer import create_revision_review
from parsezen.application.run_preparation import PreparedQueueRun
from parsezen.application.run_validation import BatchValidationIssue
from parsezen.application.scheduler import QueueRunPlan, RunMode
from parsezen.domain.attempt_activity import (
    AttemptEvent,
    AttemptEventStatus,
    AttemptPhase,
    AttemptTimeline,
    FailureSnapshot,
    ReusableWork,
    durable_failure_message,
)
from parsezen.domain.jobs import (
    AIProfileConfiguration,
    DocumentFormat,
    DocumentJob,
    DocumentSource,
    JobConfiguration,
    JobStatus,
    OutputConfiguration,
    ProcessingPlan,
    TranslationConfiguration,
)
from parsezen.domain.reviews import ReviewChoice, ReviewKind, ReviewSession, ReviewUnit
from parsezen.domain.stages import StageKind, StageStatus
from parsezen.epub_builder import EpubBookMetadata
from parsezen.errors import LocalModelUnavailableError
from parsezen.failure_recovery import FailureKind, ProcessingFailure
from parsezen.final_integrity import FinalIntegrityReport
from parsezen.infrastructure.state_store import StateStore, StateStoreError
from parsezen.local_models import OllamaConnection, OllamaModel, OllamaStatus
from parsezen.model_recommendations import ModelRecommendation, ModelRecommendations
from parsezen.pdf_conversion import PdfQualityReport, PdfReviewIssue
from parsezen.presentation.design_system import (
    ThemeMode,
    apply_parsezen_theme,
    current_theme_mode,
)
from parsezen.presentation.job_configuration_dialog import JobConfigurationDialog
from parsezen.presentation.job_table import (
    CELL_PRESENTATION_ROLE,
    COLUMNS,
    CONFIGURING_ROLE,
    JobColumn,
)
from parsezen.presentation.local_ai_controller import LocalAIAction
from parsezen.presentation.main_window import ParsezenMainWindow
from parsezen.processing import ProcessResult, ProcessStage
from parsezen.recent_activity import load_recent_jobs
from parsezen.revision import RevisionChange, RevisionDraft, RevisionKind, build_revision_draft
from parsezen.settings import AppSettings
from parsezen.translation_quality import (
    TranslationIssueKind,
    TranslationQualityIssue,
    TranslationQualityReport,
)


class ProjectedStatus:
    """Old labels mapped to the authoritative domain state for concise UI tests."""

    PENDING = JobStatus.QUEUED
    PROCESSING = JobStatus.RUNNING
    COMPLETED = JobStatus.COMPLETED
    FAILED = JobStatus.FAILED
    PAUSED = JobStatus.PAUSED
    REVIEW_PENDING = JobStatus.WAITING_REVIEW


class _RuntimeView:
    def __init__(self, window: ParsezenMainWindow, job_id: str) -> None:
        object.__setattr__(self, "window", window)
        object.__setattr__(self, "job_id", job_id)

    @property
    def job(self) -> DocumentJob:
        job = self.window._job_queue.get(self.job_id)  # noqa: SLF001
        assert job is not None
        return job

    @property
    def runtime(self):
        return self.window._runtime_by_job[self.job_id]  # noqa: SLF001

    @property
    def path(self) -> Path:
        return self.job.source.path

    @property
    def status(self) -> JobStatus:
        return self.job.status

    @status.setter
    def status(self, status: JobStatus) -> None:
        job = self.job
        fresh = DocumentJob.create(
            job.source,
            job.configuration,
            order=job.order,
            job_id=job.id,
        )
        fresh = replace(
            fresh,
            configuration_revision=job.configuration_revision,
            warnings=job.warnings,
        )
        self.window._job_queue.replace(fresh)  # noqa: SLF001
        if status is JobStatus.QUEUED:
            return
        controller = JobExecutionController(self.window._job_queue)  # noqa: SLF001
        running = controller.start_next(job.id)
        active = next(stage for stage in running.stages if stage.status is StageStatus.RUNNING)
        if status is JobStatus.RUNNING:
            return
        if status is JobStatus.PAUSED:
            controller.pause(job.id)
        elif status is JobStatus.FAILED:
            controller.fail(
                job.id,
                active.kind,
                error_code="test_failure",
                error_message="Test failure",
            )
        elif status is JobStatus.CANCELLED:
            controller.cancel(job.id)
        elif status is JobStatus.WAITING_REVIEW:
            controller.block_for_review(job.id, active.kind, review_id="test-review")
        elif status is JobStatus.COMPLETED:
            destination = (
                self.runtime.result.final_path
                if self.runtime.result is not None
                else job.source.path
            )
            controller.complete(job.id, destination)
        else:
            raise AssertionError(f"Unsupported fixture status: {status}")

    @property
    def request(self):
        return None

    @property
    def settings(self):
        return None

    @property
    def error(self) -> str | None:
        failed = next(
            (stage.error_message for stage in self.job.stages if stage.error_message),
            None,
        )
        return failed or (self.job.warnings[-1] if self.job.warnings else None)

    def __getattr__(self, name: str):
        return getattr(self.runtime, name)

    def __setattr__(self, name: str, value) -> None:
        if name in {"window", "job_id", "status"}:
            object.__setattr__(self, name, value)
            return
        setattr(self.runtime, name, value)

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, _RuntimeView)
            and other.window is self.window
            and other.job_id == self.job_id
        )


class _RuntimeViews:
    def __init__(self, window: ParsezenMainWindow) -> None:
        self.window = window

    def __iter__(self):
        return iter(tuple(_RuntimeView(self.window, job.id) for job in self.window._job_queue.jobs))  # noqa: SLF001

    def __getitem__(self, index: int) -> _RuntimeView:
        job = self.window._job_queue.jobs[index]  # noqa: SLF001
        return _RuntimeView(self.window, job.id)

    def __len__(self) -> int:
        return len(self.window._job_queue)  # noqa: SLF001

    def clear(self) -> None:
        self.window._job_queue.clear()  # noqa: SLF001
        self.window._runtime_by_job.clear()  # noqa: SLF001


def _entries(window: ParsezenMainWindow) -> _RuntimeViews:
    return _RuntimeViews(window)


def test_phase_plan_uses_materialized_review_units_and_excludes_structure() -> None:
    result = ProcessResult(
        Path("book.epub"),
        revision_draft=RevisionDraft(
            "Original\n",
            "Propuesta\n",
            (
                RevisionChange(
                    "content",
                    RevisionKind.CONTENT,
                    0,
                    1,
                    "Original\n",
                    "Propuesta\n",
                    "CorrecciÃ³n",
                ),
                RevisionChange(
                    "structure",
                    RevisionKind.STRUCTURE,
                    0,
                    1,
                    "Propuesta\n",
                    "# Propuesta\n",
                    "Estructura",
                ),
            ),
            frozenset({RevisionKind.CONTENT, RevisionKind.STRUCTURE}),
        ),
        review_required=True,
        revision_epub_metadata=EpubBookMetadata("Book", "en"),
    )
    configuration = JobConfiguration(
        output=OutputConfiguration(format=DocumentFormat.EPUB),
        plan=ProcessingPlan.LOCAL_AI_REVIEWED,
    )
    refinement_review = ReviewSession.create(
        job_id="job",
        stage=StageKind.REFINE,
        kind=ReviewKind.REFINEMENT,
        input_artifact_id="input",
        input_version=1,
        units=(ReviewUnit("refinement", "original", "proposed"),),
    )
    structure_review = ReviewSession.create(
        job_id="job",
        stage=StageKind.STRUCTURE,
        kind=ReviewKind.STRUCTURE,
        input_artifact_id="input",
        input_version=1,
        units=(ReviewUnit("structure", "original", "proposed"),),
    )

    assert phase_plan_with_materialized_reviews(
        result,
        configuration,
        (
            (ReviewKind.REFINEMENT, refinement_review),
            (ReviewKind.STRUCTURE, structure_review),
        ),
    ) == ((ReviewKind.REFINEMENT, 1),)


def test_main_window_has_no_legacy_shell_or_hidden_form(qtbot, tmp_path: Path) -> None:
    window = ParsezenMainWindow(
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)

    assert ParsezenMainWindow.__bases__ == (QMainWindow,)
    assert not hasattr(window, "_compatibility_shell")
    assert not hasattr(window, "improve_checkbox")


def test_early_check_marker_changes_with_configuration_and_source_snapshot(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "book.pdf"
    source_path.write_bytes(b"pdf")
    job = DocumentJob.create(
        DocumentSource.inspect(source_path),
        JobConfiguration(),
        order=0,
    )

    marker = ParsezenMainWindow._early_check_marker(job)
    changed_configuration = replace(job, configuration_revision=2)
    changed_source = replace(
        job,
        source=replace(job.source, size_bytes=job.source.size_bytes + 1),
    )

    assert ParsezenMainWindow._early_check_marker(changed_configuration) != marker
    assert ParsezenMainWindow._early_check_marker(changed_source) != marker


def test_system_notification_is_sent_only_while_window_is_inactive(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    window = ParsezenMainWindow(
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    tray = Mock()
    window._notification_tray = tray  # noqa: SLF001

    monkeypatch.setattr(window, "isActiveWindow", lambda: False)
    window._show_system_notification(  # noqa: SLF001
        "Parsezen",
        "Procesamiento terminado.",
        QSystemTrayIcon.MessageIcon.Information,
    )

    tray.show.assert_called_once_with()
    tray.showMessage.assert_called_once_with(
        "Parsezen",
        "Procesamiento terminado.",
        QSystemTrayIcon.MessageIcon.Information,
        10_000,
    )

    tray.reset_mock()
    monkeypatch.setattr(window, "isActiveWindow", lambda: True)
    window._show_system_notification(  # noqa: SLF001
        "Parsezen",
        "No debe mostrarse.",
        QSystemTrayIcon.MessageIcon.Information,
    )
    tray.showMessage.assert_not_called()


def test_settings_menu_changes_encrypted_checkpoint_retention(
    qtbot,
    tmp_path: Path,
) -> None:
    saved: list[AppSettings] = []
    window = ParsezenMainWindow(
        settings=AppSettings(checkpoint_retention_days=7),
        on_settings_changed=saved.append,
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)

    assert window.checkpoint_retention_actions[7].isChecked()

    window.checkpoint_retention_actions[0].trigger()

    assert window._settings.checkpoint_retention_days == 0  # noqa: SLF001
    assert saved[-1].checkpoint_retention_days == 0


def test_appearance_menu_switches_and_persists_three_theme_modes(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    class MemorySettings:
        values = {"appearance/theme": ThemeMode.SYSTEM.value}

        def __init__(self, *_args) -> None:
            pass

        def value(self, key: str, default: object = None) -> object:
            return self.values.get(key, default)

        def setValue(self, key: str, value: object) -> None:  # noqa: N802
            self.values[key] = value

    monkeypatch.setattr(main_window_module, "QSettings", MemorySettings)
    window = ParsezenMainWindow(
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)

    assert window.appearance_group.isExclusive()
    assert set(window.appearance_actions) == {
        ThemeMode.SYSTEM,
        ThemeMode.LIGHT,
        ThemeMode.DARK,
    }
    assert window.appearance_actions[ThemeMode.SYSTEM].isChecked()

    try:
        window.appearance_actions[ThemeMode.LIGHT].trigger()

        assert window._theme_mode is ThemeMode.LIGHT  # noqa: SLF001
        assert MemorySettings.values["appearance/theme"] == ThemeMode.LIGHT.value
        assert current_theme_mode() is ThemeMode.LIGHT
    finally:
        qt_application = QApplication.instance()
        assert isinstance(qt_application, QApplication)
        apply_parsezen_theme(qt_application, ThemeMode.DARK)


def test_main_window_uses_independent_configuration_and_persists_queue(
    qtbot,
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.md"
    first.write_text("one", encoding="utf-8")
    second.write_text("# two", encoding="utf-8")
    state_path = tmp_path / "workspace.sqlite3"
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=state_path,
    )
    qtbot.addWidget(window)

    window.set_source_paths((first, second))
    first_job = window._job_queue.for_source(first)  # noqa: SLF001
    second_job = window._job_queue.for_source(second)  # noqa: SLF001
    assert first_job is not None and second_job is not None
    window._job_queue.configure(  # noqa: SLF001
        first_job.id,
        replace(
            first_job.configuration,
            output=replace(first_job.configuration.output, configured=True),
        ),
    )
    window._job_queue.configure(  # noqa: SLF001
        second_job.id,
        second_job.configuration.__class__(
            output=OutputConfiguration(format=DocumentFormat.EPUB),
            translation=second_job.configuration.translation,
            plan=second_job.configuration.plan,
        ),
    )
    window._prepare_independent_requests()  # noqa: SLF001
    qtbot.waitUntil(lambda: window._prepared_run is not None, timeout=3_000)  # noqa: SLF001
    window._sync_workspace(force_persist=True)  # noqa: SLF001

    entries = tuple(_entries(window))  # noqa: SLF001
    prepared = window._prepared_run  # noqa: SLF001
    status_column = COLUMNS.index(JobColumn.NEXT_STEP)
    assert all(
        window.parsezen_workspace.job_table.job_model.index(row, status_column)
        .data(CELL_PRESENTATION_ROLE)
        .title
        == "Preparando"
        for row in range(2)
    )
    window.parsezen_workspace.set_preparing_jobs(())
    assert prepared is not None
    assert prepared.items[0].request.output_format.value == "markdown"
    assert prepared.items[1].request.output_format.value == "epub"
    assert prepared.items[0].settings is not prepared.items[1].settings
    assert all(entry.request is None and entry.settings is None for entry in entries)

    restored = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=state_path,
    )
    qtbot.addWidget(restored)
    assert tuple(entry.path for entry in _entries(restored)) == (first, second)  # noqa: SLF001


def test_primary_button_launches_prepared_runtime_from_domain_configuration(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "notes.txt"
    output = tmp_path / "notes.md"
    source.write_text("Original", encoding="utf-8")
    captured: list[tuple[object, object]] = []

    def process(request, **arguments):
        captured.append((request, arguments["settings"]))
        output.write_text("# Converted", encoding="utf-8")
        return ProcessResult(output)

    monkeypatch.setattr(main_window_module, "process_document", process)
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    job = window._job_queue.for_source(source)  # noqa: SLF001
    assert job is not None
    window._job_queue.configure(  # noqa: SLF001
        job.id,
        JobConfiguration(
            output=OutputConfiguration(format=DocumentFormat.MARKDOWN),
        ),
    )
    entry = _entries(window)[0]  # noqa: SLF001
    window._sync_workspace()  # noqa: SLF001
    window.parsezen_workspace.primary_button.click()
    qtbot.waitUntil(lambda: bool(captured), timeout=3_000)
    qtbot.waitUntil(lambda: not window.is_processing, timeout=3_000)

    assert len(captured) == 1
    request, settings = captured[0]
    assert request.source_path == source
    assert settings == AppSettings()
    assert entry.request is None
    assert entry.settings is None
    job = window._job_queue.for_source(source)  # noqa: SLF001
    assert job is not None
    assert job.status is JobStatus.COMPLETED
    assert job.result_path == output


def test_completed_processing_records_summary_and_exposes_batch_feedback(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "notes.txt"
    output = tmp_path / "notes.md"
    history = tmp_path / "recent.json"
    source.write_text("Original", encoding="utf-8")

    def process(_request, **_arguments):
        output.write_text("# Resultado", encoding="utf-8")
        return ProcessResult(
            output,
            final_integrity_report=FinalIntegrityReport(
                "Markdown",
                ("Contenido aprobado conservado",),
            ),
        )

    monkeypatch.setattr(main_window_module, "process_document", process)
    window = ParsezenMainWindow(
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
        history_path=history,
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    job = window._job_queue.for_source(source)  # noqa: SLF001
    assert job is not None
    window._job_queue.configure(  # noqa: SLF001
        job.id,
        JobConfiguration(
            output=OutputConfiguration(format=DocumentFormat.MARKDOWN),
        ),
    )
    window._sync_workspace()  # noqa: SLF001

    window.parsezen_workspace.primary_button.click()
    qtbot.waitUntil(
        lambda: history.exists() and len(load_recent_jobs(path=history)) == 1,
        timeout=3_000,
    )

    recent = load_recent_jobs(path=history)
    assert len(recent) == 1
    assert recent[0].summary is not None
    assert recent[0].summary.integrity_verified
    assert "1 listo" in window.parsezen_workspace.batch_message.message.text()
    assert not window.parsezen_workspace.batch_message.action.isHidden()


def test_cancelled_batch_has_an_explicit_summary_and_activity_action(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    window = ParsezenMainWindow(
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    cancelled_job = Mock(status=JobStatus.CANCELLED)
    monkeypatch.setattr(window._job_queue, "get", lambda _job_id: cancelled_job)  # noqa: SLF001
    monkeypatch.setattr(window, "_show_system_notification", Mock())

    window._show_finished_batch_summary(("cancelled",))  # noqa: SLF001

    assert "1 cancelado" in window.parsezen_workspace.batch_message.message.text()
    assert not window.parsezen_workspace.batch_message.action.isHidden()


def test_main_window_inspects_each_source_only_when_its_snapshot_is_created(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Original", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window._projection_timer.stop()
    inspected: list[Path] = []
    original_inspect = main_window_module._source_from_path

    def inspect_once(path: Path) -> DocumentSource:
        inspected.append(path)
        return original_inspect(path)

    monkeypatch.setattr(main_window_module, "_source_from_path", inspect_once)

    window.set_source_paths((source,))
    window._ensure_configurations()
    window._ensure_configurations()

    assert inspected == [source]
    _entries(window).clear()


def test_main_window_prunes_unrecoverable_review_artifacts_on_startup(
    qtbot,
    tmp_path: Path,
) -> None:
    artifact_directory = tmp_path / "artifacts" / "orphan-job"
    artifact_directory.mkdir(parents=True)
    (artifact_directory / "snapshot.pza").write_bytes(b"encrypted orphan")
    (artifact_directory / ".artifact-crashed.tmp").write_bytes(b"encrypted temporary")

    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)

    assert not artifact_directory.exists()


def test_main_window_replaces_domain_jobs_with_a_new_source_selection(
    qtbot,
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("First", encoding="utf-8")
    second.write_text("Second", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)

    window.set_source_paths((first,))
    first_id = window._project_jobs()[0].id
    window.set_source_paths((second,))
    jobs = window._project_jobs()

    assert tuple(job.source.path for job in jobs) == (second,)
    assert window._job_queue.get(first_id) is None


def test_main_window_reorders_and_removes_pending_jobs(qtbot, tmp_path: Path) -> None:
    paths = tuple(tmp_path / f"{name}.txt" for name in ("one", "two", "three"))
    for path in paths:
        path.write_text(path.stem, encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "state.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths(paths)
    jobs = window._project_jobs()  # noqa: SLF001

    window._move_job(jobs[0].id, 2)  # noqa: SLF001
    assert tuple(entry.path for entry in _entries(window)) == (  # noqa: SLF001
        paths[1],
        paths[2],
        paths[0],
    )
    assert all(entry.status is ProjectedStatus.PENDING for entry in _entries(window))  # noqa: SLF001

    moved_id = window._project_jobs()[1].id  # noqa: SLF001
    window._remove_job(moved_id)  # noqa: SLF001
    assert len(_entries(window)) == 2  # noqa: SLF001


def test_main_window_recovers_exact_pending_review(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Original", encoding="utf-8")
    final_path = tmp_path / "notes.mended.txt"
    final_path.write_text("Propuesta", encoding="utf-8")
    state_path = tmp_path / "workspace.sqlite3"
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=state_path,
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    job = window._project_jobs()[0]
    window._job_queue.configure(
        job.id,
        replace(
            job.configuration,
            output=replace(job.configuration.output, configured=True),
        ),
    )
    entry = _entries(window)[0]  # noqa: SLF001
    entry.status = ProjectedStatus.PROCESSING
    window._current_job_id = entry.job_id  # noqa: SLF001

    window._processing_succeeded(  # noqa: SLF001
        ProcessResult(
            final_path=final_path,
            review_original_path=source,
            review_markdown="Propuesta",
            review_required=True,
        )
    )

    restored = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=state_path,
    )
    qtbot.addWidget(restored)
    recovered = _entries(restored)[0]  # noqa: SLF001
    assert recovered.status is ProjectedStatus.REVIEW_PENDING
    assert recovered.result is not None
    assert recovered.result.review_markdown == "Propuesta"
    recovered_job = restored._project_jobs()[0]  # noqa: SLF001
    assert recovered_job.status is JobStatus.WAITING_REVIEW
    assert recovered_job.stage(StageKind.PREPARE).status is StageStatus.BLOCKED_FOR_REVIEW


def test_completed_direct_result_can_start_recommended_targeted_review(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Texto original.", encoding="utf-8")
    final_path = tmp_path / "notes.md"
    final_path.write_text("Texto con �.\n", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(model="local-model"),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    job = window._project_jobs()[0]  # noqa: SLF001
    window._job_queue.configure(  # noqa: SLF001
        job.id,
        replace(
            job.configuration,
            output=replace(job.configuration.output, configured=True),
        ),
    )
    entry = _entries(window)[0]  # noqa: SLF001
    entry.status = ProjectedStatus.PROCESSING
    window._current_job_id = job.id  # noqa: SLF001
    window._processing_succeeded(  # noqa: SLF001
        ProcessResult(
            final_path,
            review_original_path=source,
            review_markdown="Texto con �.\n",
        )
    )

    recommended = window._job_queue.get(job.id)  # noqa: SLF001
    assert recommended is not None
    assert recommended.status is JobStatus.COMPLETED
    assert recommended.review_recommendation is not None
    starts: list[tuple[object, object, dict[str, object]]] = []
    monkeypatch.setattr(
        window._processing_runner,  # noqa: SLF001
        "start",
        lambda request, settings, **kwargs: starts.append((request, settings, kwargs)),
    )

    window._start_targeted_ai_review(job.id)  # noqa: SLF001

    reviewing = window._job_queue.get(job.id)  # noqa: SLF001
    assert reviewing is not None
    assert reviewing.stage(StageKind.REFINE).status is StageStatus.READY
    assert starts and callable(starts[0][2]["processor"])
    assert entry.result is not None
    assert entry.result.review_markdown == "Texto con �.\n"
    assert window.is_processing
    assert final_path.read_text(encoding="utf-8") == "Texto con �.\n"
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, text, *_args, **_kwargs: warnings.append(str(text)),
    )

    window._processing_failed("Ollama no disponible")  # noqa: SLF001

    restored = window._job_queue.get(job.id)  # noqa: SLF001
    assert restored is not None
    assert restored.status is JobStatus.COMPLETED
    assert restored.review_recommendation == recommended.review_recommendation
    assert final_path.read_text(encoding="utf-8") == "Texto con �.\n"
    assert warnings and "sigue intacto" in warnings[0]
    window._finish_batch()  # noqa: SLF001


def test_direct_quality_recommendation_survives_an_existing_manual_review(
    qtbot,
    tmp_path: Path,
) -> None:
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"local source")
    final_path = tmp_path / "scan.md"
    final_path.write_text("word word word.\n", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    job = window._project_jobs()[0]  # noqa: SLF001
    window._job_queue.configure(  # noqa: SLF001
        job.id,
        replace(
            job.configuration,
            output=replace(job.configuration.output, configured=True),
        ),
    )
    entry = _entries(window)[0]  # noqa: SLF001
    entry.status = ProjectedStatus.PROCESSING
    window._current_job_id = job.id  # noqa: SLF001

    window._processing_succeeded(  # noqa: SLF001
        ProcessResult(
            final_path,
            review_original_path=source,
            review_markdown="word word word.\n",
            review_required=True,
        )
    )

    pending = window._job_queue.get(job.id)  # noqa: SLF001
    assert pending is not None
    assert pending.status is JobStatus.WAITING_REVIEW
    assert pending.review_recommendation is not None


def test_main_window_worker_events_update_authoritative_job_state(
    qtbot,
    tmp_path: Path,
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Original", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    entry = _entries(window)[0]  # noqa: SLF001
    job = window._project_jobs()[0]  # noqa: SLF001
    job = window._job_queue.configure(  # noqa: SLF001
        job.id,
        replace(
            job.configuration,
            output=replace(job.configuration.output, configured=True),
        ),
    )
    entry.status = ProjectedStatus.PROCESSING
    window._current_job_id = entry.job_id  # noqa: SLF001
    window._job_execution.start_next(job.id)  # noqa: SLF001

    window._show_stage(ProcessStage.CONVERTING)  # noqa: SLF001
    window._show_improvement_progress(2, 5)  # noqa: SLF001

    running = window._job_queue.get(job.id)  # noqa: SLF001
    assert running is not None
    prepare = running.stage(StageKind.PREPARE)
    assert prepare.status is StageStatus.RUNNING
    assert (prepare.progress_current, prepare.progress_total) == (2, 5)
    status = window.parsezen_workspace.job_table.job_model.index(
        0,
        COLUMNS.index(JobColumn.NEXT_STEP),
    ).data(CELL_PRESENTATION_ROLE)
    assert status.title == "Preparando · 40 %"

    window._processing_failed("Fallo reproducible")  # noqa: SLF001

    failed = window._job_queue.get(job.id)  # noqa: SLF001
    assert failed is not None
    assert failed.status is JobStatus.FAILED
    assert failed.stage(StageKind.PREPARE).error_message == "Fallo reproducible"


def test_main_window_launch_order_comes_from_application_run_plan(
    qtbot,
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("First", encoding="utf-8")
    second.write_text("Second", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((first, second))
    for job in window._project_jobs():
        window._job_queue.configure(
            job.id,
            replace(
                job.configuration,
                output=replace(job.configuration.output, configured=True),
            ),
        )
    window._job_execution.begin_run()  # noqa: SLF001

    first_running = window._next_pending_job()  # noqa: SLF001
    assert first_running is not None
    first_job = window._job_queue.for_source(first)  # noqa: SLF001
    assert first_job is not None
    window._job_execution.fail(  # noqa: SLF001
        first_job.id,
        StageKind.PREPARE,
        error_code="failed",
        error_message="Error",
    )
    second_running = window._next_pending_job()  # noqa: SLF001

    assert first_running.source.path == first
    assert second_running is not None
    assert second_running.source.path == second
    failed = window._job_queue.for_source(first)  # noqa: SLF001
    running = window._job_queue.for_source(second)  # noqa: SLF001
    assert failed is not None
    assert running is not None
    assert failed.status is JobStatus.FAILED
    assert running.status is JobStatus.RUNNING


def test_main_window_restores_interrupted_domain_execution_as_paused(
    qtbot,
    tmp_path: Path,
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Original", encoding="utf-8")
    state_path = tmp_path / "workspace.sqlite3"
    job = DocumentJob.create(
        DocumentSource.inspect(source),
        JobConfiguration(),
        order=0,
        job_id="interrupted-job",
    )
    queue = JobQueue((job,))
    running = JobExecutionController(queue).start_next(job.id)
    StateStore(state_path).replace_jobs((running,))

    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=state_path,
    )
    qtbot.addWidget(window)

    restored = window._job_queue.get(job.id)  # noqa: SLF001
    assert restored is not None
    assert restored.status is JobStatus.PAUSED
    assert restored.stage(StageKind.PREPARE).status is StageStatus.PAUSED
    assert _entries(window)[0].status is ProjectedStatus.PAUSED  # noqa: SLF001


def test_main_window_invalidates_review_when_original_changed(
    qtbot,
    tmp_path: Path,
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Original", encoding="utf-8")
    final_path = tmp_path / "notes.mended.txt"
    final_path.write_text("Propuesta", encoding="utf-8")
    state_path = tmp_path / "workspace.sqlite3"
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=state_path,
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    job = window._project_jobs()[0]
    window._job_queue.configure(
        job.id,
        replace(
            job.configuration,
            output=replace(job.configuration.output, configured=True),
        ),
    )
    entry = _entries(window)[0]
    entry.status = ProjectedStatus.PROCESSING
    window._current_job_id = entry.job_id
    window._processing_succeeded(
        ProcessResult(
            final_path=final_path,
            review_original_path=source,
            review_markdown="Propuesta",
            review_required=True,
        )
    )
    job_id = window._project_jobs()[0].id
    original_source = window._project_jobs()[0].source
    window._projection_timer.stop()
    source.write_text("El original ahora contiene otro texto.", encoding="utf-8")
    window._sync_workspace(force_persist=True)

    assert StateStore(state_path).load_jobs()[0].source == original_source

    restored = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=state_path,
    )
    qtbot.addWidget(restored)
    recovered = _entries(restored)[0]

    assert recovered.status is ProjectedStatus.PAUSED
    assert recovered.result is None
    assert recovered.error is not None
    assert "El original cambió" in recovered.error
    assert not (tmp_path / "artifacts" / job_id).exists()


def test_main_window_blocks_stale_review_before_opening_it(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Original", encoding="utf-8")
    final_path = tmp_path / "notes.mended.txt"
    final_path.write_text("Propuesta", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    entry = _entries(window)[0]
    entry.status = ProjectedStatus.PROCESSING
    window._current_job_id = entry.job_id
    window._processing_succeeded(
        ProcessResult(
            final_path=final_path,
            review_original_path=source,
            review_markdown="Propuesta",
            review_required=True,
        )
    )
    job_id = window._project_jobs()[0].id
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, title, message: warnings.append((title, message)),
    )
    source.write_text("Una versión nueva y diferente.", encoding="utf-8")

    window._review_job(job_id, None)

    assert entry.status is ProjectedStatus.PAUSED
    assert entry.result is None
    assert entry.error is not None
    assert "El original cambió" in entry.error
    assert warnings and warnings[0][0] == "El original ha cambiado"


def test_main_window_keeps_review_pending_when_finalization_cannot_be_recorded(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "notes.txt"
    final_path = tmp_path / "notes.md"
    source.write_text("Original", encoding="utf-8")
    final_path.write_text("Proposal", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    entry = _entries(window)[0]  # noqa: SLF001
    entry.status = ProjectedStatus.PROCESSING
    window._current_job_id = entry.job_id  # noqa: SLF001
    result = ProcessResult(
        final_path,
        review_original_path=source,
        review_markdown="Proposal",
        review_required=True,
    )
    window._processing_succeeded(result)  # noqa: SLF001
    warnings: list[str] = []
    monkeypatch.setattr(
        window._review_finalization,  # noqa: SLF001
        "finalize",
        lambda *_args: (_ for _ in ()).throw(StateStoreError("disk unavailable")),
    )
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message),
    )

    window._apply_reviewed_text(entry.runtime, result, "Proposal", ())  # noqa: SLF001

    assert entry.status is ProjectedStatus.REVIEW_PENDING
    assert entry.error == "disk unavailable"
    job = window._job_queue.for_source(source)  # noqa: SLF001
    assert job is not None
    assert job.status is JobStatus.WAITING_REVIEW
    assert warnings


def test_main_window_warns_and_retries_when_state_write_fails(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Original", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    original_replace = window._state_store.replace_jobs

    def fail_write(_jobs) -> None:
        raise StateStoreError("disk unavailable")

    monkeypatch.setattr(window._state_store, "replace_jobs", fail_write)

    assert not window._sync_workspace(force_persist=True)
    assert not window.parsezen_workspace.recovery_warning.isHidden()

    monkeypatch.setattr(window._state_store, "replace_jobs", original_replace)

    assert window._sync_workspace()
    assert window.parsezen_workspace.recovery_warning.isHidden()
    window._projection_timer.stop()
    _entries(window).clear()


def test_main_window_preserves_unreadable_queue_before_starting_clean(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Original", encoding="utf-8")
    state_path = tmp_path / "workspace.sqlite3"
    saved_job = DocumentJob.create(
        DocumentSource.inspect(source),
        JobConfiguration(),
        order=0,
        job_id="saved-job",
    )
    StateStore(state_path).replace_jobs((saved_job,))
    artifact_path = tmp_path / "artifacts" / saved_job.id / "review.pza"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(b"encrypted review")

    with monkeypatch.context() as context:
        context.setattr(
            StateStore,
            "load_jobs",
            lambda _store: (_ for _ in ()).throw(StateStoreError("invalid payload")),
        )
        window = ParsezenMainWindow(
            settings=AppSettings(),
            auto_discover_ai=False,
            state_path=state_path,
        )
        qtbot.addWidget(window)
        assert not window._queue_persistence.unavailable
        assert not window.parsezen_workspace.recovery_warning.isHidden()
        assert "Se conservó una copia local segura" in (
            window.parsezen_workspace.recovery_warning.text()
        )
        window._projection_timer.stop()
        _entries(window).clear()

    backups = tuple(tmp_path.glob("workspace.unreadable-*.sqlite3"))
    artifact_backups = tuple(tmp_path.glob("artifacts.unreadable-*"))
    assert len(backups) == 1
    assert len(artifact_backups) == 1
    assert StateStore(backups[0]).load_jobs() == (saved_job,)
    assert (artifact_backups[0] / saved_job.id / "review.pza").read_bytes() == (b"encrypted review")
    assert not (tmp_path / "artifacts").exists()
    assert StateStore(state_path).load_jobs() == ()


def test_main_window_never_overwrites_unreadable_queue_if_backup_fails(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Original", encoding="utf-8")
    state_path = tmp_path / "workspace.sqlite3"
    saved_job = DocumentJob.create(
        DocumentSource.inspect(source),
        JobConfiguration(),
        order=0,
        job_id="saved-job",
    )
    StateStore(state_path).replace_jobs((saved_job,))

    with monkeypatch.context() as context:
        context.setattr(
            StateStore,
            "load_jobs",
            lambda _store: (_ for _ in ()).throw(StateStoreError("invalid payload")),
        )
        context.setattr(
            StateStore,
            "backup_to",
            lambda _store, _destination: (_ for _ in ()).throw(StateStoreError("disk unavailable")),
        )
        window = ParsezenMainWindow(
            settings=AppSettings(),
            auto_discover_ai=False,
            state_path=state_path,
        )
        qtbot.addWidget(window)
        assert window._queue_persistence.unavailable
        assert not window.parsezen_workspace.recovery_warning.isHidden()
        window._projection_timer.stop()
        _entries(window).clear()

    assert StateStore(state_path).load_jobs() == (saved_job,)
    assert tuple(tmp_path.glob("workspace.unreadable-*.sqlite3")) == ()


def test_main_window_quarantines_structurally_corrupted_state(
    qtbot,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "workspace.sqlite3"
    state_path.write_bytes(b"not a sqlite database")
    artifact_path = tmp_path / "artifacts" / "saved-job" / "review.pza"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(b"encrypted review")

    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=state_path,
    )
    qtbot.addWidget(window)

    recoveries = tuple(tmp_path.glob("recovery-unreadable-*"))
    assert len(recoveries) == 1
    assert (recoveries[0] / state_path.name).read_bytes() == b"not a sqlite database"
    assert (recoveries[0] / "artifacts" / "saved-job" / "review.pza").read_bytes() == (
        b"encrypted review"
    )
    assert StateStore(state_path).load_jobs() == ()
    assert not window.parsezen_workspace.recovery_warning.isHidden()
    assert "estado anterior estaba dañado" in window.parsezen_workspace.recovery_warning.text()
    window._projection_timer.stop()
    _entries(window).clear()


def test_main_window_confirms_close_when_recovery_is_unavailable(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Original", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    window.show()
    qtbot.waitExposed(window)
    window._queue_persistence.mark_unavailable()
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.No,
    )
    event = QCloseEvent()
    event.accept()

    window.closeEvent(event)

    assert not event.isAccepted()
    window._queue_persistence.mark_available()


def test_main_window_routes_configuration_review_and_primary_actions(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "notes.txt"
    second = tmp_path / "second.txt"
    source.write_text("Original", encoding="utf-8")
    second.write_text("Second", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    job = window._project_jobs()[0]
    entry = _entries(window)[0]
    notices: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda _parent, _title, text: notices.append(text),
    )

    entry.status = ProjectedStatus.PROCESSING
    window._configure_job(job.id, None)
    assert notices
    entry.status = ProjectedStatus.PENDING

    configured = job.configuration.__class__(
        output=job.configuration.output,
        translation=job.configuration.translation,
        ai=AIProfileConfiguration(model="qwen3:4b"),
        plan=ProcessingPlan.LOCAL_AI_REVIEWED,
    )

    window._configure_job(job.id, None)
    editor = window._active_configuration_dialog  # noqa: SLF001
    assert isinstance(editor, JobConfigurationDialog)
    monkeypatch.setattr(editor, "configuration", lambda: configured)
    editor.configuration_changed.emit()
    updated_job = window._job_queue.get(job.id)  # noqa: SLF001
    assert updated_job is not None
    assert updated_job.configuration.plan is ProcessingPlan.LOCAL_AI_REVIEWED
    assert entry.request is None
    assert entry.settings is None

    window._add_dropped_paths("not-a-tuple")
    window._add_dropped_paths((second,))
    assert tuple(item.path for item in _entries(window)) == (source, second)

    entry.result = ProcessResult(tmp_path / "notes.md", review_markdown="Reviewed")
    entry.status = ProjectedStatus.REVIEW_PENDING
    reviewed_cards: list[str] = []
    monkeypatch.setattr(window, "_review_quality_phases", lambda *_args: ("Reviewed", ()))
    monkeypatch.setattr(window, "_open_local_path", lambda path: reviewed_cards.append(str(path)))
    window._review_job(job.id, None)
    assert reviewed_cards == [str(entry.result.final_path)]

    actions: list[str] = []
    monkeypatch.setattr(window, "_pause_processing", lambda: actions.append("pause"))
    monkeypatch.setattr(window, "_review_job", lambda *_args: actions.append("review"))
    monkeypatch.setattr(window, "_select_result_card", lambda *_args: actions.append("select"))
    monkeypatch.setattr(window, "_open_result_folder", lambda: actions.append("folder"))

    def prepare() -> None:
        actions.append("prepare")
        window._prepared_run = PreparedQueueRun(  # noqa: SLF001
            QueueRunPlan(RunMode.NEW, (job.id,)),
            (),
            (),
        )

    monkeypatch.setattr(window, "_prepare_independent_requests", prepare)

    def start() -> None:
        actions.append("start")
        window._batch_running = True

    monkeypatch.setattr(window, "_start_processing", start)

    window._run_primary_action("pause")
    window._run_primary_action("review")
    entry.status = ProjectedStatus.COMPLETED
    window._run_primary_action("open_folder")
    window._run_primary_action("process")
    window._batch_running = False

    assert actions == ["pause", "review", "select", "folder", "prepare", "start"]


def test_table_click_opens_the_compact_configuration_sheet(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Original", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    job = window._project_jobs()[0]
    window._job_queue.configure(
        job.id,
        replace(
            job.configuration,
            output=replace(job.configuration.output, configured=True),
        ),
    )
    window._sync_workspace()
    discovery: list[bool] = []
    window._auto_discover_ai = True
    monkeypatch.setattr(
        window,
        "_start_model_discovery",
        lambda *, automatic: discovery.append(automatic),
    )
    table = window.parsezen_workspace.job_table
    index = table.job_model.index(0, 2)

    table._cell_clicked(index)

    editor = window._active_configuration_dialog  # noqa: SLF001
    assert isinstance(editor, JobConfigurationDialog)
    assert window.parsezen_workspace.current_internal_widget is editor
    assert editor.window() is window
    assert not editor.review_row.isHidden()
    assert not editor.translate_row.isHidden()
    assert editor.translator_row.isHidden()
    assert editor.glossary_row.isHidden()
    assert not hasattr(editor, "scroll_area")
    assert not hasattr(editor, "save_button")
    assert not hasattr(editor, "cancel_button")
    assert index.data(CONFIGURING_ROLE) is True
    assert discovery == []

    window._ollama_models = (OllamaModel("qwen3:4b-instruct", "Qwen3 4B Instruct"),)
    window._select_model_from_manager("qwen3:4b-instruct")
    editor._set_review_enabled(True)  # noqa: SLF001

    assert editor.configuration().ai.model == "qwen3:4b-instruct"
    assert editor.plan_reviewed.isChecked()

    editor.reject()

    assert index.data(CONFIGURING_ROLE) is False


def test_configuration_sheet_resumes_after_local_ai_settings(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Original", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "_start_model_discovery", lambda *, automatic: None)
    monkeypatch.setattr(window, "_start_model_recommendations", lambda: None)
    window.set_source_paths((source,))
    job = window._project_jobs()[0]

    window._configure_job(job.id, None)
    editor = window._active_configuration_dialog  # noqa: SLF001
    assert editor is not None

    editor.models_requested.emit()

    manager = window._model_manager  # noqa: SLF001
    assert manager is not None
    assert window.parsezen_workspace.current_internal_widget is manager

    manager.reject()
    qtbot.waitUntil(lambda: window.parsezen_workspace.current_internal_widget is editor)

    assert window._active_configuration_dialog is editor  # noqa: SLF001
    assert window.parsezen_workspace.current_internal_widget is editor
    editor.reject()
    assert window.parsezen_workspace.current_internal_widget is None


def test_internal_back_closes_the_immediately_saved_configuration(
    qtbot,
    tmp_path: Path,
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Original", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    job = window._project_jobs()[0]
    window._configure_job(job.id, None)
    editor = window._active_configuration_dialog  # noqa: SLF001
    assert isinstance(editor, JobConfigurationDialog)
    editor._set_review_enabled(False)  # noqa: SLF001
    editor._set_output_format(DocumentFormat.EPUB)  # noqa: SLF001

    window._close_internal_workflow(editor)  # noqa: SLF001

    configured = window._job_queue.get(job.id).configuration  # noqa: SLF001
    assert configured.output.format is DocumentFormat.EPUB
    assert window._active_configuration_dialog is None  # noqa: SLF001
    assert window.parsezen_workspace.current_internal_widget is None


def test_model_manager_opens_before_discovery_and_starts_it(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    discovery: list[bool] = []
    monkeypatch.setattr(
        window,
        "_start_model_discovery",
        lambda *, automatic: discovery.append(automatic),
    )

    window._show_model_manager()

    assert window._model_manager is not None
    assert window.parsezen_workspace.current_internal_widget is window._model_manager
    assert discovery == [False]
    window._model_manager.reject()


def test_local_ai_page_updates_one_profile_for_all_unstarted_documents(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Original", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    model = OllamaModel("qwen3:4b-instruct", "Qwen3 4B Instruct")
    window._model_discovery_succeeded(  # noqa: SLF001
        OllamaConnection(OllamaStatus.READY, (model,))
    )
    monkeypatch.setattr(window, "_start_model_recommendations", lambda **_kwargs: None)

    window._show_model_manager()  # noqa: SLF001
    manager = window._model_manager  # noqa: SLF001
    assert manager is not None

    manager.select_requested.emit(model.model_id)
    manager.context_window.setValue(16_384)

    job = window._job_queue.for_source(source)  # noqa: SLF001
    assert job is not None
    assert window._settings.model == model.model_id  # noqa: SLF001
    assert window._settings.context_window == 16_384  # noqa: SLF001
    assert job.configuration.ai.model == model.model_id
    assert job.configuration.ai.context_window == 16_384
    manager.reject()


def test_failed_job_inline_message_uses_the_real_failed_stage(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Original", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    job = window._job_queue.jobs[0]
    job = job.with_configuration(
        replace(
            job.configuration,
            output=replace(job.configuration.output, configured=True),
            translation=TranslationConfiguration(enabled=True, target_language="Español"),
        )
    )
    failed = replace(
        job,
        stages=tuple(
            replace(stage, status=StageStatus.FAILED, error_message="Ollama no respondió.")
            if stage.kind is StageKind.TRANSLATE
            else stage
            for stage in job.stages
        ),
    )
    window._job_queue.replace(failed)
    window._show_job_error(job.id, StageKind.PUBLISH)

    assert not window.parsezen_workspace.job_message.isHidden()
    message = window.parsezen_workspace.job_message.message.text()
    assert "La IA local necesita atención" in message
    assert "Ollama no respondió" in message
    assert window.parsezen_workspace.job_message.action.text() == "Abrir IA local"
    assert window.parsezen_workspace.job_message.secondary_action.text() == "Reintentar"


def test_contextual_retry_targets_only_the_failed_document(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    failed_source = tmp_path / "failed.txt"
    queued_source = tmp_path / "queued.txt"
    failed_source.write_text("Failed", encoding="utf-8")
    queued_source.write_text("Queued", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((failed_source, queued_source))
    for job in window._job_queue.jobs:
        window._job_queue.replace(  # noqa: SLF001
            job.with_configuration(
                replace(
                    job.configuration,
                    output=replace(job.configuration.output, configured=True),
                )
            )
        )
    failed_job = window._job_queue.for_source(failed_source)  # noqa: SLF001
    assert failed_job is not None
    window._job_execution.start_next(failed_job.id)  # noqa: SLF001
    window._job_execution.fail(  # noqa: SLF001
        failed_job.id,
        StageKind.PREPARE,
        error_code="source",
        error_message="Error",
    )
    failed_entry = window._entry_for_job_id(failed_job.id)  # noqa: SLF001
    assert failed_entry is not None
    started: list[QueueRunPlan] = []
    monkeypatch.setattr(
        window,
        "_start_prepared_run",
        lambda: started.append(window._prepared_run.plan),  # noqa: SLF001
    )

    window._retry_failed_job(failed_job.id)  # noqa: SLF001

    qtbot.waitUntil(lambda: bool(started), timeout=3_000)
    assert started == [QueueRunPlan(RunMode.RETRY, (failed_job.id,))]


def test_main_window_keeps_previous_configuration_when_runtime_mapping_fails(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Original", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    job = window._project_jobs()[0]
    changed = job.configuration.__class__(
        output=replace(job.configuration.output, configured=True),
        translation=job.configuration.translation,
        ai=AIProfileConfiguration(model="qwen3:4b"),
        plan=ProcessingPlan.LOCAL_AI_REVIEWED,
    )

    monkeypatch.setattr(
        main_window_module,
        "request_and_settings_from_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("invalid mapping")),
    )
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args, **_kwargs: None)

    window._configure_job(job.id, None)
    editor = window._active_configuration_dialog  # noqa: SLF001
    assert isinstance(editor, JobConfigurationDialog)
    monkeypatch.setattr(editor, "configuration", lambda: changed)
    editor.configuration_changed.emit()

    assert window._job_queue.get(job.id).configuration == job.configuration


def test_main_window_handles_unknown_jobs_and_empty_drop_queue(
    qtbot,
    tmp_path: Path,
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Original", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)

    window._add_dropped_paths((source,))
    assert tuple(entry.path for entry in _entries(window)) == (source,)
    assert window._entry_for_job_id("missing") is None
    assert window._job_for_id("missing") is None
    window._configure_job("missing", None)
    window._review_job("missing", None)
    window._remove_job("missing")
    window._move_job("missing", 0)


def test_main_window_confirms_and_cleans_progress_before_removing_paused_job(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Original intact", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    job = window._project_jobs()[0]
    configured = window._job_queue.configure(
        job.id,
        replace(
            job.configuration,
            output=replace(job.configuration.output, configured=True),
        ),
    )
    _entries(window)[0].status = ProjectedStatus.PAUSED
    cleaned: list[Path] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(
        main_window_module,
        "clear_document_work_checkpoints",
        lambda request, *_args, **_kwargs: cleaned.append(request.source_path),
    )

    window._remove_job(configured.id)

    assert cleaned == [source]
    assert window._job_queue.get(configured.id) is None
    assert tuple(_entries(window)) == ()
    assert source.read_text(encoding="utf-8") == "Original intact"


def test_main_window_confirms_before_discarding_pending_review(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "review.txt"
    source.write_text("Original intact", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    job = window._project_jobs()[0]
    _entries(window)[0].status = ProjectedStatus.REVIEW_PENDING
    prompts: list[tuple[str, str]] = []

    def confirm(_parent, title, message, *_args):
        prompts.append((title, message))
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", confirm)

    window._remove_job(job.id)

    assert prompts
    assert "pendiente de revisión" in prompts[0][0]
    assert "revisión pendiente" in prompts[0][1]
    assert window._job_queue.get(job.id) is None
    assert tuple(_entries(window)) == ()
    assert source.read_text(encoding="utf-8") == "Original intact"


def test_main_window_opens_completed_result_and_its_folder(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Original", encoding="utf-8")
    final_path = tmp_path / "notes.final.txt"
    final_path.write_text("Final", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    entry = _entries(window)[0]
    job_id = window._project_jobs()[0].id
    entry.status = ProjectedStatus.COMPLETED
    entry.result = ProcessResult(final_path)
    actions: list[Path] = []
    monkeypatch.setattr(
        window,
        "_open_local_path",
        actions.append,
    )

    window._open_job_result(job_id)
    window._open_job_result_folder(job_id)
    window._open_job_result("missing")
    window._open_job_result_folder("missing")

    assert actions == [final_path, final_path.parent]


def test_main_window_materializes_and_applies_each_quality_review_phase(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Start. Hello. End.", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    entry = _entries(window)[0]
    job = window._project_jobs()[0]
    job = window._job_queue.configure(
        job.id,
        JobConfiguration(
            translation=TranslationConfiguration(enabled=True, target_language="es"),
        ),
    )
    entry.result = ProcessResult(
        tmp_path / "notes.md",
        review_markdown="Start. Hola. End.",
        translation_quality_report=TranslationQualityReport(
            "en",
            "Español",
            "es",
            1,
            3,
            2,
            1,
            (
                TranslationQualityIssue(
                    1,
                    TranslationIssueKind.ALIGNMENT,
                    "Revisar",
                    "Hello.",
                    "Hola.",
                    "segment",
                ),
            ),
        ),
        review_required=True,
    )
    window._job_execution.start_next(job.id)
    window._job_execution.advance(job.id, StageKind.PUBLISH)
    window._job_execution.block_completed_result_for_review(
        job.id,
        StageKind.TRANSLATE,
        review_id="translation-gate",
    )

    class AcceptedReviewDialog:
        def __init__(self, review, _artifacts, **_kwargs) -> None:
            self.review = review
            for unit in review.units:
                self.review = self.review.decide(
                    unit.id,
                    main_window_module.ReviewChoice.PROPOSED,
                )

        def exec(self):
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(main_window_module, "PhaseReviewDialog", AcceptedReviewDialog)

    reviewed = window._review_quality_phases(entry.runtime, job)

    assert reviewed is not None
    text, reviews = reviewed
    assert text == "Start. Hola. End."
    assert len(reviews) == 1
    assert reviews[0].units[0].choice is main_window_module.ReviewChoice.PROPOSED


def test_review_resume_replaces_stale_units_but_keeps_the_durable_review_id() -> None:
    candidate = ReviewSession.create(
        job_id="job",
        stage=StageKind.REFINE,
        kind=ReviewKind.REFINEMENT,
        input_artifact_id="current-input",
        input_version=3,
        units=(ReviewUnit("current-one", "original", "proposal"),),
    )
    stale = ReviewSession.create(
        job_id="job",
        stage=StageKind.REFINE,
        kind=ReviewKind.REFINEMENT,
        input_artifact_id="old-input",
        input_version=3,
        units=(ReviewUnit("old-one", "original", "proposal"),),
    )

    resumed = review_for_current_candidate(stale, candidate)

    assert resumed.id == stale.id
    assert resumed.input_artifact_id == "current-input"
    assert tuple(unit.id for unit in resumed.units) == ("current-one",)
    assert resumed.units[0].choice is None


def test_translation_resume_does_not_reuse_a_legacy_original_replacement() -> None:
    candidate = ReviewSession.create(
        job_id="job",
        stage=StageKind.TRANSLATE,
        kind=ReviewKind.TRANSLATION,
        input_artifact_id="current-input",
        input_version=3,
        units=(ReviewUnit("segment", "source", "translation"),),
    )
    saved = replace(
        candidate,
        id="saved-review",
        status=main_window_module.ReviewStatus.APPLIED,
        units=(
            replace(
                candidate.units[0],
                choice=main_window_module.ReviewChoice.ORIGINAL,
                original_selectable=True,
            ),
        ),
    )

    resumed = review_for_current_candidate(saved, candidate)

    assert resumed.status is main_window_module.ReviewStatus.PENDING
    assert resumed.units[0].choice is None
    assert not resumed.units[0].original_selectable


def test_quality_review_rebuilds_revision_candidate_after_applied_quality_choice(
    qtbot,
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.md"
    source.write_text("source", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    job = window._job_queue.jobs[0]  # noqa: SLF001
    entry = _entries(window)[0]  # noqa: SLF001

    raw_text = "# Title\n\nRaw paragraph.\n\nAnchor paragraph.\n\nStable paragraph.\n"
    reviewed_text = "# Title\n\nRaw paragraph.\n\nAnchor paragraph.\n\nStable correction.\n"
    proposed_text = "# Title\n\nRaw correction.\n\nAnchor paragraph.\n\nStable correction.\n"
    raw_draft = build_revision_draft(
        raw_text,
        proposed_text,
        kinds=frozenset({RevisionKind.CONTENT}),
    )
    post_quality_draft = build_revision_draft(
        reviewed_text,
        proposed_text,
        kinds=frozenset({RevisionKind.CONTENT}),
    )
    raw_candidate = create_revision_review(
        raw_draft,
        revision_kind=RevisionKind.CONTENT,
        job_id=job.id,
        configuration_revision=job.configuration_revision,
        artifacts=window._artifact_store,  # noqa: SLF001
    )
    saved_refinement = create_revision_review(
        post_quality_draft,
        revision_kind=RevisionKind.CONTENT,
        job_id=job.id,
        configuration_revision=job.configuration_revision,
        artifacts=window._artifact_store,  # noqa: SLF001
    )
    assert raw_candidate is not None
    assert saved_refinement is not None
    assert tuple(unit.id for unit in raw_candidate.units) != tuple(
        unit.id for unit in saved_refinement.units
    )
    assert len(raw_candidate.units) != len(saved_refinement.units)
    saved_refinement = saved_refinement.decide(
        saved_refinement.units[0].id,
        ReviewChoice.PROPOSED,
    )

    quality_report = TranslationQualityReport(
        "en",
        "Español",
        "es",
        1,
        len(raw_text),
        len(raw_text),
        1,
        (
            TranslationQualityIssue(
                1,
                TranslationIssueKind.ALIGNMENT,
                "Revisar",
                "Stable paragraph.",
                "Stable paragraph.",
                "quality-choice",
            ),
        ),
    )
    quality_input = window._artifact_store.put_text(  # noqa: SLF001
        job_id=job.id,
        text=raw_text,
    )
    quality_candidate = create_translation_review(
        quality_report,
        job_id=job.id,
        configuration_revision=job.configuration_revision,
        input_artifact_id=quality_input.id,
        artifacts=window._artifact_store,  # noqa: SLF001
    )
    assert quality_candidate is not None
    edited = window._artifact_store.put_text(  # noqa: SLF001
        job_id=job.id,
        text="Stable correction.",
    )
    applied_quality = quality_candidate.decide(
        quality_candidate.units[0].id,
        ReviewChoice.EDITED,
        edited_artifact_id=edited.id,
    ).apply()
    window._state_store.save_review(applied_quality)  # noqa: SLF001
    window._state_store.save_review(saved_refinement)  # noqa: SLF001

    entry.result = ProcessResult(
        tmp_path / "book.md.out",
        review_markdown=raw_text,
        translation_quality_report=quality_report,
        revision_draft=raw_draft,
        review_required=True,
    )

    reviewed = window._review_quality_phases(entry.runtime, job)  # noqa: SLF001

    assert reviewed is not None
    assert reviewed[0] == reviewed_text
    resumed = tuple(
        review
        for review in window._state_store.load_reviews(job_id=job.id)  # noqa: SLF001
        if review.kind is ReviewKind.REFINEMENT
    )
    assert len(resumed) == 1
    assert tuple(unit.id for unit in resumed[0].units) == tuple(
        unit.id for unit in saved_refinement.units
    )
    assert resumed[0].units[0].choice is ReviewChoice.PROPOSED


def test_pending_quality_review_does_not_replace_later_progress_before_reconcile(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "book.md"
    source.write_text("source", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    job = window._job_queue.configure(  # noqa: SLF001
        window._job_queue.jobs[0].id,  # noqa: SLF001
        JobConfiguration(
            translation=TranslationConfiguration(enabled=True, target_language="es"),
        ),
    )
    entry = _entries(window)[0]  # noqa: SLF001

    raw_text = "# Title\n\nRaw paragraph.\n\nAnchor paragraph.\n\nStable paragraph.\n"
    reviewed_text = "# Title\n\nRaw paragraph.\n\nAnchor paragraph.\n\nStable correction.\n"
    proposed_text = "# Title\n\nRaw correction.\n\nAnchor paragraph.\n\nStable correction.\n"
    raw_draft = build_revision_draft(
        raw_text,
        proposed_text,
        kinds=frozenset({RevisionKind.CONTENT}),
    )
    post_quality_draft = build_revision_draft(
        reviewed_text,
        proposed_text,
        kinds=frozenset({RevisionKind.CONTENT}),
    )
    raw_candidate = create_revision_review(
        raw_draft,
        revision_kind=RevisionKind.CONTENT,
        job_id=job.id,
        configuration_revision=job.configuration_revision,
        artifacts=window._artifact_store,  # noqa: SLF001
    )
    saved_refinement = create_revision_review(
        post_quality_draft,
        revision_kind=RevisionKind.CONTENT,
        job_id=job.id,
        configuration_revision=job.configuration_revision,
        artifacts=window._artifact_store,  # noqa: SLF001
    )
    assert raw_candidate is not None
    assert saved_refinement is not None
    assert len(raw_candidate.units) != len(saved_refinement.units)
    saved_refinement = saved_refinement.decide(
        saved_refinement.units[0].id,
        ReviewChoice.PROPOSED,
    ).apply()
    window._state_store.save_review(saved_refinement)  # noqa: SLF001

    quality_report = TranslationQualityReport(
        "en",
        "Español",
        "es",
        1,
        len(raw_text),
        len(raw_text),
        1,
        (
            TranslationQualityIssue(
                1,
                TranslationIssueKind.ALIGNMENT,
                "Revisar",
                "Stable paragraph.",
                "Stable paragraph.",
                "quality-choice",
            ),
        ),
    )
    quality_input = window._artifact_store.put_text(  # noqa: SLF001
        job_id=job.id,
        text=raw_text,
    )
    quality_candidate = create_translation_review(
        quality_report,
        job_id=job.id,
        configuration_revision=job.configuration_revision,
        input_artifact_id=quality_input.id,
        artifacts=window._artifact_store,  # noqa: SLF001
    )
    assert quality_candidate is not None
    window._state_store.save_review(quality_candidate)  # noqa: SLF001
    window._job_execution.start_next(job.id)  # noqa: SLF001
    window._job_execution.advance(job.id, StageKind.PUBLISH)  # noqa: SLF001
    window._job_execution.block_completed_result_for_review(  # noqa: SLF001
        job.id,
        StageKind.TRANSLATE,
        review_id="translation-gate",
    )

    entry.result = ProcessResult(
        tmp_path / "book.md.out",
        review_markdown=raw_text,
        translation_quality_report=quality_report,
        revision_draft=raw_draft,
        review_required=True,
    )

    class DeferredReviewDialog:
        def __init__(self, review, _artifacts, **_kwargs) -> None:
            self.review = review

        def exec(self):
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(main_window_module, "PhaseReviewDialog", DeferredReviewDialog)

    assert window._review_quality_phases(entry.runtime, job) is None  # noqa: SLF001
    resumed = next(
        review
        for review in window._state_store.load_reviews(job_id=job.id)  # noqa: SLF001
        if review.kind is ReviewKind.REFINEMENT
    )
    assert resumed.id == saved_refinement.id
    assert resumed.status is main_window_module.ReviewStatus.APPLIED
    assert tuple(unit.id for unit in resumed.units) == tuple(
        unit.id for unit in saved_refinement.units
    )


def test_stale_post_quality_revision_candidate_is_replaced_safely(
    qtbot,
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.md"
    source.write_text("source", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    job = window._job_queue.jobs[0]  # noqa: SLF001
    stale_draft = build_revision_draft(
        "# Title\n\nOld paragraph.\n",
        "# Title\n\nOld correction.\n",
        kinds=frozenset({RevisionKind.CONTENT}),
    )
    current_draft = build_revision_draft(
        "# Title\n\nCurrent paragraph.\n\nSecond paragraph.\n",
        "# Title\n\nCurrent correction.\n\nSecond correction.\n",
        kinds=frozenset({RevisionKind.CONTENT}),
    )
    stale = create_revision_review(
        stale_draft,
        revision_kind=RevisionKind.CONTENT,
        job_id=job.id,
        configuration_revision=job.configuration_revision,
        artifacts=window._artifact_store,  # noqa: SLF001
    )
    current = create_revision_review(
        current_draft,
        revision_kind=RevisionKind.CONTENT,
        job_id=job.id,
        configuration_revision=job.configuration_revision,
        artifacts=window._artifact_store,  # noqa: SLF001
    )
    assert stale is not None
    assert current is not None
    stale = replace(
        stale.decide(stale.units[0].id, ReviewChoice.PROPOSED),
        id="saved-stale-review",
    )
    window._state_store.save_review(stale)  # noqa: SLF001

    window._review_materialization.ensure_revision_candidates(  # noqa: SLF001
        current_draft,
        job,
        (stale,),
    )

    resumed = next(
        review
        for review in window._state_store.load_reviews(job_id=job.id)  # noqa: SLF001
        if review.kind is ReviewKind.REFINEMENT
    )
    assert resumed.id == stale.id
    assert tuple(unit.id for unit in resumed.units) == tuple(unit.id for unit in current.units)
    assert all(unit.choice is None for unit in resumed.units)


def test_unanchorable_translation_does_not_overwrite_saved_review_with_empty_session(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "book.md"
    source.write_text("source", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    job = window._job_queue.jobs[0]  # noqa: SLF001
    entry = _entries(window)[0]  # noqa: SLF001
    original = window._artifact_store.put_text(job_id=job.id, text="context")  # noqa: SLF001
    proposed = window._artifact_store.put_text(job_id=job.id, text="translation")  # noqa: SLF001
    saved = ReviewSession.create(
        job_id=job.id,
        stage=StageKind.TRANSLATE,
        kind=ReviewKind.TRANSLATION,
        input_artifact_id=original.id,
        input_version=job.configuration_revision,
        units=(ReviewUnit("saved-translation", original.id, proposed.id),),
    )
    window._state_store.save_review(saved)  # noqa: SLF001
    report = TranslationQualityReport(
        "en",
        "Español",
        "es",
        1,
        10,
        10,
        1,
        (
            TranslationQualityIssue(
                1,
                TranslationIssueKind.ALIGNMENT,
                "No se pudo alinear",
                "Missing source",
                "Missing translated...",
                "unanchorable",
            ),
        ),
    )
    entry.result = ProcessResult(
        tmp_path / "book.md.out",
        review_markdown="Current translated text.",
        translation_quality_report=report,
        review_required=True,
    )
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, text, *_args, **_kwargs: warnings.append(str(text)),
    )

    assert window._review_quality_phases(entry.runtime, job) is None  # noqa: SLF001
    reviews = tuple(
        review
        for review in window._state_store.load_reviews(job_id=job.id)  # noqa: SLF001
        if review.kind is ReviewKind.TRANSLATION
    )
    assert len(reviews) == 1
    assert tuple(unit.id for unit in reviews[0].units) == ("saved-translation",)
    assert reviews[0].units
    assert warnings


def test_main_window_always_confirms_epub_even_without_ai_structure_changes(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "book.md"
    source.write_text("# Chapter\n\nBody.", encoding="utf-8")
    destination = tmp_path / "book.epub"
    destination.write_bytes(b"preliminary")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    original = window._project_jobs()[0]
    job = window._job_queue.configure(  # noqa: SLF001
        original.id,
        JobConfiguration(
            output=OutputConfiguration(format=DocumentFormat.EPUB),
        ),
    )
    entry = _entries(window)[0]  # noqa: SLF001
    entry.result = ProcessResult(
        destination,
        review_markdown="# Chapter\n\nBody.",
        review_required=True,
        revision_epub_metadata=EpubBookMetadata("Book", "en"),
    )
    window._job_execution.block_completed_result_for_review(  # noqa: SLF001
        job.id,
        StageKind.PUBLISH,
        review_id="personalization-gate",
    )
    opened: list[str] = []

    def confirm(book, **_kwargs):
        opened.append(book.metadata.title)
        return book, None

    monkeypatch.setattr(window, "_confirm_epub_book", confirm)

    window._review_job(job.id, None)  # noqa: SLF001

    assert opened == ["Book"]
    assert entry.status is ProjectedStatus.COMPLETED
    assert destination.read_bytes().startswith(b"PK")


def test_global_destination_updates_every_editable_job(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    old_destination = tmp_path / "old"
    new_destination = tmp_path / "new"
    sources = (tmp_path / "one.txt", tmp_path / "two.txt")
    for source in sources:
        source.write_text("Text", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(output_directory=old_destination),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths(sources)
    monkeypatch.setattr(
        window,
        "_select_output_directory",
        lambda: setattr(
            window,
            "_settings",
            replace(window._settings, output_directory=new_destination),
        ),
    )

    window._choose_global_output_directory()

    one, two = window._job_queue.jobs
    assert one.configuration.output.directory == new_destination
    assert two.configuration.output.directory == new_destination


def test_flat_configuration_updates_only_the_selected_document(
    qtbot,
    tmp_path: Path,
) -> None:
    sources = (
        tmp_path / "one.txt",
        tmp_path / "two.txt",
        tmp_path / "notes.md",
    )
    for source in sources:
        source.write_text("Text", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths(sources)
    first = window._job_queue.jobs[0]

    window._configure_job(first.id, None)
    editor = window._active_configuration_dialog  # noqa: SLF001
    assert isinstance(editor, JobConfigurationDialog)
    editor._set_review_enabled(False)  # noqa: SLF001
    editor._set_output_format(DocumentFormat.MARKDOWN)  # noqa: SLF001

    one, two, markdown = window._job_queue.jobs
    assert one.configuration.output.configured
    assert not two.configuration.output.configured
    assert one.configuration.output.format is DocumentFormat.MARKDOWN
    assert not markdown.configuration.output.configured


def test_global_ai_default_updates_every_editable_profile(
    qtbot,
    tmp_path: Path,
) -> None:
    sources = tuple(tmp_path / name for name in ("one.txt", "two.txt", "three.txt"))
    for source in sources:
        source.write_text("Text", encoding="utf-8")
    old = AIProfileConfiguration(model="old-model", context_window=4096)
    new = AIProfileConfiguration(model="new-model", context_window=8192)
    window = ParsezenMainWindow(
        settings=AppSettings(model=old.model, context_window=old.context_window),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths(sources)
    inherited, custom, failed = window._job_queue.jobs
    failed = failed.replace_stage(
        failed.stage(StageKind.PREPARE)
        .transition(StageStatus.READY)
        .transition(StageStatus.RUNNING)
        .transition(StageStatus.FAILED, error_message="fallo previo")
    )
    window._job_queue.replace(failed)

    window._propagate_global_ai_profile(old, new)

    inherited, custom, failed = window._job_queue.jobs
    assert inherited.configuration.ai == new
    assert custom.configuration.ai == new
    assert failed.configuration.ai == new


def test_model_in_use_by_unfinished_work_cannot_be_deleted_silently(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "book.txt"
    source.write_text("Text", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(model="qwen3:4b-instruct"),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    job = window._job_queue.jobs[0]
    window._job_queue.replace(
        job.with_configuration(
            replace(
                job.configuration,
                ai=AIProfileConfiguration(model="qwen3:4b-instruct"),
                plan=ProcessingPlan.LOCAL_AI_REVIEWED,
            )
        )
    )
    window._ollama_models = (OllamaModel("qwen3:4b-instruct", "Qwen3 4B Instruct"),)
    messages: list[tuple[str, str]] = []
    starts: list[object] = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda _parent, title, text: messages.append((title, text)),
    )
    monkeypatch.setattr(window, "_start_ai_setup", lambda *args, **kwargs: starts.append(args))

    window._confirm_model_delete("qwen3:4b-instruct")

    assert not starts
    assert messages
    assert messages[0][0] == "Modelo utilizado por trabajos pendientes"
    assert "book.txt" in messages[0][1]
    assert "otro modelo predeterminado" in messages[0][1]


def test_local_ai_controller_projection_covers_every_user_visible_state(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    manager = Mock()
    window._model_manager = manager  # noqa: SLF001
    available = OllamaModel(
        "qwen3:4b-instruct",
        "Qwen3 4B Instruct",
        size_bytes=4_000_000_000,
    )
    reasoning = OllamaModel("deepseek-r1:7b", "DeepSeek R1")
    window._pending_ai_model = available.model_id  # noqa: SLF001

    window._model_discovery_succeeded(  # noqa: SLF001
        OllamaConnection(
            OllamaStatus.READY,
            (reasoning, available),
            message="Disponible",
        )
    )

    assert window._ollama_models == (available,)  # noqa: SLF001
    assert window._settings.model == available.model_id  # noqa: SLF001
    assert window._pending_ai_model is None  # noqa: SLF001
    manager.set_installed_models.assert_called()
    manager.set_connection_status.assert_called_with(OllamaStatus.READY, "Disponible")

    monkeypatch.setattr(window._local_ai, "discover", lambda _model: True)  # noqa: SLF001
    window._start_model_discovery(automatic=False)  # noqa: SLF001
    assert window._is_discovering_models  # noqa: SLF001
    manager.set_connection_status.assert_called_with(None)

    window._model_discovery_failed("Sin conexión local")  # noqa: SLF001
    assert window._ollama_status is OllamaStatus.UNAVAILABLE  # noqa: SLF001
    assert window._ollama_models == ()  # noqa: SLF001
    window._model_discovery_finished()  # noqa: SLF001
    assert not window._is_discovering_models  # noqa: SLF001

    recommendations = ModelRecommendations(
        (),
        "Equipo de prueba",
        "1.0",
        datetime.now(UTC),
    )
    monkeypatch.setattr(
        window._local_ai,  # noqa: SLF001
        "recommend",
        lambda *, force_refresh=False: force_refresh,
    )
    window._start_model_recommendations(force_refresh=True)  # noqa: SLF001
    assert window._is_recommending_models  # noqa: SLF001
    window._model_recommendations_succeeded(recommendations)  # noqa: SLF001
    assert window._model_recommendations is recommendations  # noqa: SLF001
    window._model_recommendations_failed("No se pudo recomendar")  # noqa: SLF001
    assert window._model_recommendations is None  # noqa: SLF001
    window._model_recommendations_finished()  # noqa: SLF001
    assert not window._is_recommending_models  # noqa: SLF001

    installs: list[tuple[str, int | None]] = []
    monkeypatch.setattr(
        window,
        "_start_model_install",
        lambda model_id, *, expected_download_size_bytes=None: installs.append(
            (model_id, expected_download_size_bytes)
        ),
    )
    recommendation = ModelRecommendation(
        "qwen3:4b-instruct",
        "Qwen3 4B",
        "Equilibrado",
        4_000,
        8_192,
        20.0,
        5.0,
        0.9,
    )
    window._install_recommendation(recommendation)  # noqa: SLF001
    window._install_custom_model("qwen3:4b-instruct")  # noqa: SLF001
    window._install_custom_model("")  # noqa: SLF001
    assert installs == [
        ("qwen3:4b-instruct", 4_000),
        ("qwen3:4b-instruct", None),
    ]
    manager.set_custom_error.assert_called()

    requested_actions: list[LocalAIAction] = []
    discoveries: list[bool] = []
    shown: list[bool] = []
    monkeypatch.setattr(
        window,
        "_start_ai_setup",
        lambda action, **_kwargs: requested_actions.append(action),
    )
    monkeypatch.setattr(
        window,
        "_start_model_discovery",
        lambda *, automatic: discoveries.append(automatic),
    )
    monkeypatch.setattr(window, "_show_model_manager", lambda: shown.append(True))
    for status in (
        OllamaStatus.NOT_INSTALLED,
        OllamaStatus.STOPPED,
        OllamaStatus.LOCAL_ONLY_REQUIRED,
    ):
        window._ollama_status = status  # noqa: SLF001
        window._handle_ai_primary_action()  # noqa: SLF001
    window._ollama_status = OllamaStatus.MISSING_MODEL  # noqa: SLF001
    window._handle_ai_primary_action()  # noqa: SLF001
    window._ollama_status = OllamaStatus.READY  # noqa: SLF001
    window._handle_ai_primary_action()  # noqa: SLF001
    assert requested_actions == [
        LocalAIAction.INSTALL,
        LocalAIAction.START,
        LocalAIAction.PROTECT,
    ]
    assert shown == [True]
    assert discoveries == [False]

    monkeypatch.setattr(window._local_ai, "setup", lambda *_args, **_kwargs: True)  # noqa: SLF001
    ParsezenMainWindow._start_ai_setup(  # noqa: SLF001
        window,
        LocalAIAction.PULL_MODEL,
        model_id=available.model_id,
        expected_download_size_bytes=4_000,
    )
    assert window._is_ai_setup_active  # noqa: SLF001
    assert window._ai_setup_action is LocalAIAction.PULL_MODEL  # noqa: SLF001
    window._ai_setup_progress_changed(35, "Descargando")  # noqa: SLF001
    manager.set_operation.assert_called_with(
        "Descargando",
        percent=35,
        cancellable=True,
    )
    window._ai_setup_succeeded_slot(available.model_id)  # noqa: SLF001
    assert window._pending_ai_model == available.model_id  # noqa: SLF001
    window._ai_setup_finished()  # noqa: SLF001
    assert not window._is_ai_setup_active  # noqa: SLF001
    assert discoveries[-1] is False

    monkeypatch.setattr(window._local_ai, "cancel_setup", lambda: True)  # noqa: SLF001
    window._cancel_ai_setup()  # noqa: SLF001
    manager.cancel_button.setEnabled.assert_called_with(False)
    window._ai_setup_cancelled("Cancelado")  # noqa: SLF001
    window._ai_setup_failed("Falló")  # noqa: SLF001
    manager.finish_operation.assert_called_with("Falló")


def test_processing_controller_projects_progress_success_failure_and_pause(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "processing.txt"
    source.write_text("Original", encoding="utf-8")
    final_path = tmp_path / "processing.md"
    final_path.write_text("Resultado", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    job = window._job_queue.jobs[0]  # noqa: SLF001
    window._job_queue.configure(  # noqa: SLF001
        job.id,
        replace(
            job.configuration,
            output=replace(
                job.configuration.output,
                format=DocumentFormat.MARKDOWN,
                configured=True,
            ),
        ),
    )
    window._prepare_independent_requests()  # noqa: SLF001
    qtbot.waitUntil(lambda: window._prepared_run is not None, timeout=3_000)  # noqa: SLF001
    starts: list[object] = []
    monkeypatch.setattr(
        window._processing_runner,  # noqa: SLF001
        "start",
        lambda request, settings, **kwargs: starts.append((request, settings, kwargs)),
    )

    window._start_processing()  # noqa: SLF001

    entry = _entries(window)[0]  # noqa: SLF001
    assert starts
    assert window.is_processing
    assert entry.status is ProjectedStatus.PROCESSING
    window._show_stage(ProcessStage.IMPROVING)  # noqa: SLF001
    window._show_improvement_progress(-1, 2)  # noqa: SLF001
    window._show_improvement_progress(1, 2)  # noqa: SLF001
    assert entry.stage is ProcessStage.IMPROVING
    assert (entry.progress_current, entry.progress_total) == (1, 2)

    window._processing_succeeded(ProcessResult(final_path))  # noqa: SLF001
    assert entry.status is ProjectedStatus.COMPLETED
    assert entry.result is not None
    window._processing_worker_finished()  # noqa: SLF001
    assert not window.is_processing

    assert window._prepared_run is None  # noqa: SLF001

    failed_source = tmp_path / "failure.txt"
    failed_source.write_text("Original", encoding="utf-8")
    window.set_source_paths((failed_source,))
    failed_job = window._job_queue.jobs[0]  # noqa: SLF001
    window._job_queue.configure(  # noqa: SLF001
        failed_job.id,
        replace(
            failed_job.configuration,
            output=replace(
                failed_job.configuration.output,
                format=DocumentFormat.MARKDOWN,
                configured=True,
            ),
        ),
    )
    window._prepare_independent_requests()  # noqa: SLF001
    qtbot.waitUntil(lambda: window._prepared_run is not None, timeout=3_000)  # noqa: SLF001
    window._start_processing()  # noqa: SLF001
    failed_entry = _entries(window)[0]  # noqa: SLF001
    window._show_stage(ProcessStage.READING)  # noqa: SLF001
    window._processing_failed("Lectura fallida")  # noqa: SLF001
    assert failed_entry.status is ProjectedStatus.FAILED
    assert failed_entry.error == "Lectura fallida"
    window._processing_worker_finished()  # noqa: SLF001

    paused_source = tmp_path / "pause.txt"
    paused_source.write_text("Original", encoding="utf-8")
    window.set_source_paths((paused_source,))
    paused_job = window._job_queue.jobs[0]  # noqa: SLF001
    window._job_queue.configure(  # noqa: SLF001
        paused_job.id,
        replace(
            paused_job.configuration,
            output=replace(
                paused_job.configuration.output,
                format=DocumentFormat.MARKDOWN,
                configured=True,
            ),
        ),
    )
    window._prepare_independent_requests()  # noqa: SLF001
    qtbot.waitUntil(lambda: window._prepared_run is not None, timeout=3_000)  # noqa: SLF001
    window._start_processing()  # noqa: SLF001
    paused_entry = _entries(window)[0]  # noqa: SLF001
    window._pause_requested = True  # noqa: SLF001
    window._processing_cancelled()  # noqa: SLF001
    assert paused_entry.status is ProjectedStatus.PAUSED
    window._processing_worker_finished()  # noqa: SLF001
    assert not window.is_processing


def test_pause_keeps_runner_domain_checkpoints_activity_and_scheduler_consistent(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "pause.txt"
    source.write_text("Original", encoding="utf-8")
    history = tmp_path / "recent.json"
    checkpoint = tmp_path / "checkpoints" / "pause.checkpoint"
    started = Event()
    cleaned: list[Path] = []
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
        history_path=history,
        work_checkpoint_root=checkpoint.parent,
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    job = window._job_queue.jobs[0]  # noqa: SLF001
    window._job_queue.configure(  # noqa: SLF001
        job.id,
        replace(
            job.configuration,
            output=replace(
                job.configuration.output,
                format=DocumentFormat.MARKDOWN,
                configured=True,
            ),
        ),
    )

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
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_text("reusable work", encoding="utf-8")
        started.set()
        assert cancellation._event.wait(timeout=2)  # noqa: SLF001
        cancellation.check()
        raise AssertionError("Pause must stop the physical processor.")

    monkeypatch.setattr(main_window_module, "process_document", processor)
    monkeypatch.setattr(
        main_window_module,
        "clear_document_work_checkpoints",
        lambda request, *_args, **_kwargs: cleaned.append(request.source_path),
    )
    window._prepare_independent_requests()  # noqa: SLF001
    qtbot.waitUntil(lambda: window._prepared_run is not None, timeout=3_000)  # noqa: SLF001
    window._start_processing()  # noqa: SLF001
    qtbot.waitUntil(started.is_set, timeout=2_000)

    window._pause_processing()  # noqa: SLF001

    qtbot.waitUntil(lambda: not window.is_processing, timeout=2_000)
    paused = window._job_queue.get(job.id)  # noqa: SLF001
    activity = window._activity_for_job(job.id)  # noqa: SLF001
    assert paused is not None
    assert paused.status is JobStatus.PAUSED
    assert activity is not None
    assert activity[1].events[-1].status is AttemptEventStatus.PAUSED
    assert all(event.status is not AttemptEventStatus.CANCELLED for event in activity[1].events)
    assert checkpoint.read_text(encoding="utf-8") == "reusable work"
    assert cleaned == []
    assert load_recent_jobs(path=history) == ()
    assert window._job_execution.plan_run() == QueueRunPlan(  # noqa: SLF001
        RunMode.RESUME,
        (job.id,),
    )


def test_recent_failure_captures_the_runner_attempt_before_history_is_written(
    qtbot,
    tmp_path: Path,
) -> None:
    source = tmp_path / "failed.txt"
    source.write_text("Original", encoding="utf-8")
    history = tmp_path / "recent.json"
    window = ParsezenMainWindow(
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
        history_path=history,
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    job = window._job_queue.jobs[0]  # noqa: SLF001
    window._job_queue.configure(  # noqa: SLF001
        job.id,
        replace(
            job.configuration,
            output=replace(
                job.configuration.output,
                format=DocumentFormat.MARKDOWN,
                configured=True,
            ),
            translation=TranslationConfiguration(enabled=True, target_language="es"),
        ),
    )
    entry = _entries(window)[0]  # noqa: SLF001
    entry.status = ProjectedStatus.PROCESSING
    window._current_job_id = entry.job_id  # noqa: SLF001
    window._job_execution.start_next(job.id)  # noqa: SLF001
    window._show_stage(ProcessStage.TRANSLATING)  # noqa: SLF001

    timestamp = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)
    runner = window._processing_runner  # noqa: SLF001
    runner._attempt_id = "attempt-for-this-job"  # noqa: SLF001
    runner._timeline = AttemptTimeline(  # noqa: SLF001
        (
            AttemptEvent(AttemptPhase.TRANSLATION, AttemptEventStatus.STARTED, timestamp=timestamp),
            AttemptEvent(
                AttemptPhase.TRANSLATION,
                AttemptEventStatus.FAILED,
                timestamp=timestamp,
            ),
        )
    )
    runner._failure_snapshot = FailureSnapshot(  # noqa: SLF001
        AttemptPhase.TRANSLATION,
        "transformation",
        "La traducción local se detuvo.",
        diagnostic_reference="reference-for-this-job",
        reusable_work=ReusableWork.PREVIOUS_PHASES,
    )

    window._processing_failed(  # noqa: SLF001
        ProcessingFailure(
            FailureKind.TRANSFORMATION,
            "La traducción local se detuvo.",
            "TranslationError",
        )
    )

    recent = load_recent_jobs(path=history)
    assert len(recent) == 1
    assert recent[0].attempt_id == "attempt-for-this-job"
    assert recent[0].timeline.events == runner.timeline.events
    assert recent[0].failure == replace(
        runner.failure_snapshot,
        message=durable_failure_message("transformation"),
    )
    assert "Error en traducción" in window.parsezen_workspace.job_message.message.text()


def test_revision_pipeline_applies_content_and_structure_before_publication(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "revision.md"
    source.write_text("# Old title\n\nOriginal paragraph.\n", encoding="utf-8")
    destination = tmp_path / "revision.final.md"
    destination.write_text("Borrador", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    original = window._job_queue.jobs[0]  # noqa: SLF001
    job = window._job_queue.configure(  # noqa: SLF001
        original.id,
        JobConfiguration(
            output=OutputConfiguration(
                format=DocumentFormat.MARKDOWN,
                configured=True,
            ),
            plan=ProcessingPlan.LOCAL_AI_REVIEWED,
        ),
    )
    draft = build_revision_draft(
        "# Old title\n\nOriginal paragraph.\n",
        "## New title\n\nImproved paragraph.\n",
        kinds=frozenset({RevisionKind.CONTENT, RevisionKind.STRUCTURE}),
    )
    entry = _entries(window)[0]  # noqa: SLF001
    entry.result = ProcessResult(
        destination,
        revision_draft=draft,
        review_markdown=draft.proposed_markdown,
        review_required=True,
    )
    window._job_execution.start_next(job.id)  # noqa: SLF001
    window._job_execution.advance(job.id, StageKind.PUBLISH)  # noqa: SLF001
    window._job_execution.block_completed_result_for_review(  # noqa: SLF001
        job.id,
        StageKind.REFINE,
        review_id="revision-gate",
    )

    class AcceptedReviewDialog:
        def __init__(self, review, _artifacts, **_kwargs) -> None:
            self.review = review
            for unit in review.units:
                self.review = self.review.decide(
                    unit.id,
                    main_window_module.ReviewChoice.PROPOSED,
                )

        def exec(self):
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(main_window_module, "PhaseReviewDialog", AcceptedReviewDialog)

    window._review_revision_by_phase(entry.runtime, job, draft=draft)  # noqa: SLF001

    assert entry.status is ProjectedStatus.COMPLETED
    assert entry.result is not None
    assert "New title" in destination.read_text(encoding="utf-8")


def test_revision_pipeline_publishes_reviewed_epub_through_the_editor(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "book.md"
    source.write_text("# Old title\n\nOriginal paragraph.\n", encoding="utf-8")
    destination = tmp_path / "book.epub"
    destination.write_bytes(b"preliminary")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    original = window._job_queue.jobs[0]  # noqa: SLF001
    job = window._job_queue.configure(  # noqa: SLF001
        original.id,
        JobConfiguration(
            output=OutputConfiguration(
                format=DocumentFormat.EPUB,
                configured=True,
            ),
            plan=ProcessingPlan.LOCAL_AI_REVIEWED,
        ),
    )
    draft = build_revision_draft(
        "# Old title\n\nOriginal paragraph.\n",
        "## New title\n\nImproved paragraph.\n",
        kinds=frozenset({RevisionKind.CONTENT, RevisionKind.STRUCTURE}),
    )
    entry = _entries(window)[0]  # noqa: SLF001
    entry.result = ProcessResult(
        destination,
        revision_draft=draft,
        revision_epub_metadata=EpubBookMetadata("Book", "en"),
        review_markdown=draft.proposed_markdown,
        review_required=True,
    )
    window._job_execution.start_next(job.id)  # noqa: SLF001
    window._job_execution.advance(job.id, StageKind.PUBLISH)  # noqa: SLF001
    window._job_execution.block_completed_result_for_review(  # noqa: SLF001
        job.id,
        StageKind.REFINE,
        review_id="epub-revision-gate",
    )

    class AcceptedReviewDialog:
        def __init__(self, review, _artifacts, **_kwargs) -> None:
            self.review = review
            for unit in review.units:
                self.review = self.review.decide(
                    unit.id,
                    main_window_module.ReviewChoice.PROPOSED,
                )

        def exec(self):
            return QDialog.DialogCode.Accepted

    opened: list[str] = []

    monkeypatch.setattr(main_window_module, "PhaseReviewDialog", AcceptedReviewDialog)
    monkeypatch.setattr(
        window,
        "_confirm_epub_book",
        lambda book, **_kwargs: (opened.append(book.metadata.title) or book, None),
    )

    window._review_revision_by_phase(entry.runtime, job, draft=draft)  # noqa: SLF001

    assert opened == ["Book"]
    assert entry.status is ProjectedStatus.COMPLETED
    assert destination.read_bytes().startswith(b"PK")


def test_quality_pipeline_applies_pdf_review_before_later_phases(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF")
    destination = tmp_path / "scan.md"
    destination.write_text("<!-- page -->\nOld OCR", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    original = window._job_queue.jobs[0]  # noqa: SLF001
    job = window._job_queue.configure(  # noqa: SLF001
        original.id,
        JobConfiguration(
            output=OutputConfiguration(
                format=DocumentFormat.MARKDOWN,
                configured=True,
            ),
        ),
    )
    entry = _entries(window)[0]  # noqa: SLF001
    entry.result = ProcessResult(
        destination,
        review_markdown="<!-- page -->\nOld OCR",
        pdf_quality_report=PdfQualityReport(
            (1,),
            (1,),
            (
                PdfReviewIssue(
                    1,
                    "OCR dudoso",
                    "Old OCR",
                    "page-one",
                    True,
                    "<!-- page -->",
                ),
            ),
        ),
        review_required=True,
    )
    window._job_execution.start_next(job.id)  # noqa: SLF001
    window._job_execution.advance(job.id, StageKind.PUBLISH)  # noqa: SLF001
    window._job_execution.block_completed_result_for_review(  # noqa: SLF001
        job.id,
        StageKind.PREPARE,
        review_id="ocr-gate",
    )

    monkeypatch.setattr(
        "parsezen.application.quality_review_adapter.render_pdf_page_cover",
        lambda *_args: b"jpeg-page",
    )

    class AcceptedReviewDialog:
        def __init__(self, review, _artifacts, **_kwargs) -> None:
            self.review = review
            for unit in review.units:
                edited = window._artifact_store.put_text(  # noqa: SLF001
                    job_id=review.job_id,
                    text="Corrected OCR",
                )
                self.review = self.review.decide(
                    unit.id,
                    main_window_module.ReviewChoice.EDITED,
                    edited_artifact_id=edited.id,
                )

        def exec(self):
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(main_window_module, "PhaseReviewDialog", AcceptedReviewDialog)

    reviewed = window._review_quality_phases(entry.runtime, job)  # noqa: SLF001

    assert reviewed is not None
    text, reviews = reviewed
    assert "Corrected OCR" in text
    assert tuple(review.kind for review in reviews) == (main_window_module.ReviewKind.OCR,)


def test_translation_review_can_reopen_the_previous_ocr_phase(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    original = window._job_queue.jobs[0]  # noqa: SLF001
    job = window._job_queue.configure(  # noqa: SLF001
        original.id,
        JobConfiguration(
            output=OutputConfiguration(
                format=DocumentFormat.MARKDOWN,
                configured=True,
            ),
            translation=TranslationConfiguration(enabled=True, target_language="es"),
        ),
    )
    entry = _entries(window)[0]  # noqa: SLF001
    marker = "<!-- PZDOC PDF PAGE 1 -->"
    review_text = f"{marker}\n\nHola."
    entry.result = ProcessResult(
        tmp_path / "scan.md",
        review_markdown=review_text,
        pdf_quality_report=PdfQualityReport(
            (1,),
            (1,),
            (PdfReviewIssue(1, "OCR dudoso", "Hola.", "page-one", True, marker),),
        ),
        translation_quality_report=TranslationQualityReport(
            "en",
            "Español",
            "es",
            1,
            5,
            5,
            1,
            (
                TranslationQualityIssue(
                    1,
                    TranslationIssueKind.ALIGNMENT,
                    "Revisar",
                    "Hello.",
                    "Hola.",
                    "translation-one",
                ),
            ),
        ),
        review_required=True,
    )
    window._job_execution.start_next(job.id)  # noqa: SLF001
    window._job_execution.advance(job.id, StageKind.PUBLISH)  # noqa: SLF001
    window._job_execution.block_completed_result_for_review(  # noqa: SLF001
        job.id,
        StageKind.PREPARE,
        review_id="ocr-gate",
    )
    monkeypatch.setattr(
        "parsezen.application.quality_review_adapter.render_pdf_page_cover",
        lambda *_args: b"jpeg-page",
    )
    opened: list[ReviewKind] = []

    class BackwardReviewDialog:
        def __init__(self, review, _artifacts, **kwargs) -> None:
            self.review = review
            self._previous = kwargs.get("previous_phase_callback")
            opened.append(review.kind)

        def exec(self):
            if opened == [ReviewKind.OCR]:
                for unit in self.review.units:
                    self.review = self.review.decide(unit.id, ReviewChoice.PROPOSED)
                return QDialog.DialogCode.Accepted
            if opened == [ReviewKind.OCR, ReviewKind.TRANSLATION]:
                assert self._previous is not None
                assert self._previous()
                return QDialog.DialogCode.Rejected
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(main_window_module, "PhaseReviewDialog", BackwardReviewDialog)

    assert window._review_quality_phases(entry.runtime, job) is None  # noqa: SLF001
    assert opened == [ReviewKind.OCR, ReviewKind.TRANSLATION, ReviewKind.OCR]
    saved = {
        review.kind: review
        for review in window._state_store.load_reviews(job_id=job.id)  # noqa: SLF001
    }
    assert saved[ReviewKind.OCR].status is main_window_module.ReviewStatus.PENDING
    assert saved[ReviewKind.TRANSLATION].status is main_window_module.ReviewStatus.PENDING


def test_main_window_boundary_actions_fail_safely_without_hidden_state(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "one.txt"
    second = tmp_path / "two.md"
    source.write_text("One", encoding="utf-8")
    second.write_text("# Two", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)

    window.set_source_paths(())
    window.add_source_paths(())
    window.add_source_paths((source, source))
    window.add_source_paths((source, second))
    window.add_source_paths((second,))
    assert tuple(entry.path for entry in _entries(window)) == (source, second)  # noqa: SLF001
    window._add_dropped_paths(["not", "a", "tuple"])  # noqa: SLF001
    assert window._validate_pending_requests(()) == ()  # noqa: SLF001
    assert window._pending_runtime_items(window._settings) == ()  # noqa: SLF001
    assert window._runtime_for_entry("missing") is None  # noqa: SLF001
    queued = window._job_queue.jobs[0]  # noqa: SLF001
    window._job_queue.configure(  # noqa: SLF001
        queued.id,
        replace(
            queued.configuration,
            output=replace(
                queued.configuration.output,
                format=DocumentFormat.MARKDOWN,
                configured=True,
            ),
        ),
    )
    assert window._runtime_for_entry(_entries(window)[0].job_id) is not None  # noqa: SLF001
    assert window._processing_run_flags() == (False, False)  # noqa: SLF001
    assert not window._local_ai_required()  # noqa: SLF001

    window._prepared_run = None  # noqa: SLF001
    window._start_processing()  # noqa: SLF001
    window._current_job_id = None  # noqa: SLF001
    window._show_stage(ProcessStage.READING)  # noqa: SLF001
    window._show_improvement_progress(2, 1)  # noqa: SLF001
    window._processing_succeeded(ProcessResult(tmp_path / "orphan.md"))  # noqa: SLF001
    window._processing_failed("Sin trabajo activo")  # noqa: SLF001
    window._processing_cancelled()  # noqa: SLF001

    processing_close = QCloseEvent()
    window._is_processing = True  # noqa: SLF001
    window.closeEvent(processing_close)
    assert not processing_close.isAccepted()
    window._is_processing = False  # noqa: SLF001
    ai_close = QCloseEvent()
    window._is_discovering_models = True  # noqa: SLF001
    window.closeEvent(ai_close)
    assert not ai_close.isAccepted()
    window._is_discovering_models = False  # noqa: SLF001

    monkeypatch.setattr(
        main_window_module.QFileDialog,
        "getOpenFileNames",
        lambda *_args, **_kwargs: ([], ""),
    )
    window._select_file()  # noqa: SLF001
    monkeypatch.setattr(
        main_window_module.QFileDialog,
        "getOpenFileNames",
        lambda *_args, **_kwargs: ([str(second)], ""),
    )
    window._select_file()  # noqa: SLF001

    class Executable:
        def exec(self) -> int:
            return 17

    assert window._exec_internal_dialog(Executable(), "Prueba") == 17  # noqa: SLF001
    transient = QDialog(window)
    transient.show()
    window._close_internal_workflow(transient)  # noqa: SLF001
    assert not transient.isVisible()

    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, text, *_args, **_kwargs: warnings.append(str(text)),
    )
    missing_directory = tmp_path / "missing-output"
    monkeypatch.setattr(
        main_window_module.QFileDialog,
        "getExistingDirectory",
        lambda *_args, **_kwargs: str(missing_directory),
    )
    window._select_output_directory()  # noqa: SLF001
    assert warnings
    output_directory = tmp_path / "output"
    output_directory.mkdir()
    monkeypatch.setattr(
        main_window_module.QFileDialog,
        "getExistingDirectory",
        lambda *_args, **_kwargs: str(output_directory),
    )
    window._select_output_directory()  # noqa: SLF001
    assert window._settings.output_directory == output_directory  # noqa: SLF001
    window._reset_output_directory()  # noqa: SLF001
    assert window._settings.output_directory is None  # noqa: SLF001

    monkeypatch.setattr(
        main_window_module,
        "validate_settings",
        lambda _candidate: (_ for _ in ()).throw(LocalModelUnavailableError("Inválida")),
    )
    assert window._apply_settings(AppSettings()) is None  # noqa: SLF001
    previous_retention = window._settings.checkpoint_retention_days  # noqa: SLF001
    window._set_checkpoint_retention(0)  # noqa: SLF001
    assert window.checkpoint_retention_actions[previous_retention].isChecked()

    opened: list[Path] = []
    monkeypatch.setattr(
        main_window_module.QDesktopServices,
        "openUrl",
        lambda url: opened.append(Path(url.toLocalFile())) or False,
    )
    window._open_local_path(tmp_path / "absent.txt")  # noqa: SLF001
    window._select_result_card("missing")  # noqa: SLF001
    window._open_result_folder()  # noqa: SLF001
    window._review_result_card("missing")  # noqa: SLF001
    assert warnings

    first_entry = _entries(window)[0]  # noqa: SLF001
    first_entry.status = ProjectedStatus.PROCESSING
    window._move_job("missing", 0)  # noqa: SLF001
    window._move_job(first_entry.job_id, 1)  # noqa: SLF001
    assert first_entry in _entries(window)  # noqa: SLF001

    pauses = iter((False, True))
    monkeypatch.setattr(
        window._processing_runner,  # noqa: SLF001
        "pause",
        lambda: next(pauses),
    )
    window._pause_processing()  # noqa: SLF001
    window._is_processing = True  # noqa: SLF001
    window._pause_processing()  # noqa: SLF001
    assert not window._pause_requested  # noqa: SLF001
    window._pause_processing()  # noqa: SLF001
    assert window._pause_requested  # noqa: SLF001


def test_primary_action_reports_each_non_runnable_queue_state(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "action.txt"
    source.write_text("Original", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    job = window._job_queue.jobs[0]  # noqa: SLF001
    notices: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, title, text, *_args, **_kwargs: notices.append((title, text)),
    )
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda _parent, title, text, *_args, **_kwargs: notices.append((title, text)),
    )

    monkeypatch.setattr(
        window,
        "_prepare_independent_requests",
        lambda: (_ for _ in ()).throw(ValueError("No se puede mapear")),
    )
    window._run_primary_action("run")  # noqa: SLF001
    assert notices[-1][0] == "No se pudo preparar el procesamiento"

    empty = PreparedQueueRun(QueueRunPlan(RunMode.NONE, ()), (), ())
    monkeypatch.setattr(
        window,
        "_prepare_independent_requests",
        lambda: setattr(window, "_prepared_run", empty),
    )
    window._run_primary_action("run")  # noqa: SLF001
    assert notices[-1][0] == "Nada pendiente"

    invalid = PreparedQueueRun(
        QueueRunPlan(RunMode.NEW, (job.id,)),
        (),
        (BatchValidationIssue(1, source.name, "Elige una transformación"),),
    )
    monkeypatch.setattr(
        window,
        "_prepare_independent_requests",
        lambda: setattr(window, "_prepared_run", invalid),
    )
    window._run_primary_action("run")  # noqa: SLF001
    assert notices[-1][0] == "Revisa la configuración"

    runnable = PreparedQueueRun(QueueRunPlan(RunMode.NEW, (job.id,)), (), ())
    monkeypatch.setattr(
        window,
        "_prepare_independent_requests",
        lambda: setattr(window, "_prepared_run", runnable),
    )
    monkeypatch.setattr(window, "_start_processing", lambda: None)
    window._run_primary_action("run")  # noqa: SLF001
    assert notices[-1][0] == "No se pudo iniciar"


def test_model_and_destination_commands_cover_safe_recovery_paths(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "settings.txt"
    source.write_text("Text", encoding="utf-8")
    old_output = tmp_path / "old-output"
    old_output.mkdir()
    window = ParsezenMainWindow(
        settings=AppSettings(
            output_directory=old_output,
            model="old-model",
            context_window=4_096,
        ),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    manager = Mock()
    window._model_manager = manager  # noqa: SLF001
    model = OllamaModel(
        "qwen3:4b-instruct",
        "Qwen3 4B Instruct",
        size_bytes=4_000_000_000,
    )
    window._ollama_models = (model,)  # noqa: SLF001
    setup_requests: list[tuple[LocalAIAction, str | None]] = []
    monkeypatch.setattr(
        window,
        "_start_ai_setup",
        lambda action, *, model_id=None, **_kwargs: setup_requests.append((action, model_id)),
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )

    window._confirm_model_delete(model.model_id)  # noqa: SLF001
    window._confirm_model_delete("missing:model")  # noqa: SLF001
    assert setup_requests == [(LocalAIAction.DELETE_MODEL, model.model_id)]

    window._select_model_from_manager("missing:model")  # noqa: SLF001
    manager.set_custom_error.assert_called()
    window._select_model_from_manager(model.model_id)  # noqa: SLF001
    assert window._settings.model == model.model_id  # noqa: SLF001
    window._select_context_from_manager(True)  # noqa: SLF001
    assert window._settings.context_window is None  # noqa: SLF001
    window._select_context_from_manager(8_192)  # noqa: SLF001
    assert window._settings.context_window == 8_192  # noqa: SLF001

    window._reset_global_output_directory()  # noqa: SLF001
    assert window._settings.output_directory is None  # noqa: SLF001
    window._reset_global_output_directory()  # noqa: SLF001
    window._propagate_global_output_directory(None, None)  # noqa: SLF001
    monkeypatch.setattr(
        main_window_module.QFileDialog,
        "getExistingDirectory",
        lambda *_args, **_kwargs: "",
    )
    window._select_output_directory()  # noqa: SLF001

    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, text, *_args, **_kwargs: warnings.append(str(text)),
    )
    monkeypatch.setattr(main_window_module.QDesktopServices, "openUrl", lambda _url: False)
    window._open_ollama_library()  # noqa: SLF001
    assert warnings

    monkeypatch.setattr(window._local_ai, "setup", lambda *_args, **_kwargs: True)  # noqa: SLF001
    window._settings = replace(  # noqa: SLF001
        window._settings,
        model=model.model_id,
        context_window=8_192,
    )
    ParsezenMainWindow._start_ai_setup(  # noqa: SLF001
        window,
        LocalAIAction.DELETE_MODEL,
        model_id=model.model_id,
    )
    window._ai_setup_succeeded_slot("")  # noqa: SLF001
    assert window._settings.model is None  # noqa: SLF001
    assert window._pending_deleted_model == model.model_id  # noqa: SLF001


def test_review_surfaces_preserve_work_across_editor_and_storage_failures(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "book.md"
    source.write_text("# Book\n\nText.", encoding="utf-8")
    destination = tmp_path / "book.epub"
    destination.write_bytes(b"draft")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    job = window._job_queue.jobs[0]  # noqa: SLF001
    entry = _entries(window)[0]  # noqa: SLF001
    result = ProcessResult(
        destination,
        review_markdown="# Book\n\nText.",
        revision_epub_metadata=EpubBookMetadata("Book", "en"),
        review_required=True,
    )
    window._job_execution.block_completed_result_for_review(  # noqa: SLF001
        job.id,
        StageKind.PREPARE,
        review_id="epub-review",
    )
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, title, text, *_args, **_kwargs: warnings.append((title, str(text))),
    )
    monkeypatch.setattr(window, "_keep_review_pending", Mock())

    entry.result = None
    window._personalize_epub(entry.runtime, job, "", ())  # noqa: SLF001

    entry.result = result
    publication = Mock()
    publication.prepare_book.side_effect = ValueError("No se pudo preparar")
    window._review_publication = publication  # noqa: SLF001
    window._personalize_epub(entry.runtime, job, result.review_markdown or "", ())  # noqa: SLF001
    assert entry.status is ProjectedStatus.REVIEW_PENDING

    publication.prepare_book.side_effect = None
    publication.prepare_book.return_value = Mock()
    monkeypatch.setattr(
        window,
        "_confirm_epub_book",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("Editor no disponible")),
    )
    window._personalize_epub(entry.runtime, job, result.review_markdown or "", ())  # noqa: SLF001
    assert warnings[-1][0] == "No se pudo abrir el editor"

    saved_book = Mock()
    monkeypatch.setattr(
        window,
        "_confirm_epub_book",
        lambda *_args, **_kwargs: (None, saved_book),
    )
    publication.save_book.side_effect = OSError("Disco lleno")
    window._personalize_epub(entry.runtime, job, result.review_markdown or "", ())  # noqa: SLF001
    assert warnings[-1][0] == "No se pudo guardar el borrador"
    assert entry.status is ProjectedStatus.REVIEW_PENDING

    empty_entry = JobRuntime()
    assert window._review_quality_phases(empty_entry, job) is None  # noqa: SLF001
    empty_entry.result = ProcessResult(destination)
    assert window._review_quality_phases(empty_entry, job) == ("", ())  # noqa: SLF001

    original_load_reviews = window._state_store.load_reviews  # noqa: SLF001
    monkeypatch.setattr(
        window._state_store,  # noqa: SLF001
        "load_reviews",
        lambda **_kwargs: (_ for _ in ()).throw(StateStoreError("Estado no disponible")),
    )
    entry.result = result
    assert window._review_quality_phases(entry.runtime, job) is None  # noqa: SLF001
    monkeypatch.setattr(window._state_store, "load_reviews", original_load_reviews)  # noqa: SLF001
    assert warnings[-1][0] == "No se pudo preparar la revisión"


def test_workspace_commands_route_through_the_active_surface(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "commands.txt"
    extra = tmp_path / "extra.md"
    source.write_text("Text", encoding="utf-8")
    extra.write_text("# Extra", encoding="utf-8")
    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)
    window.set_source_paths((source,))
    job = window._job_queue.jobs[0]  # noqa: SLF001

    internal_calls: list[tuple[object, str]] = []
    monkeypatch.setattr(
        window,
        "_exec_internal_dialog",
        lambda dialog, title: internal_calls.append((dialog, title)) or 0,
    )
    window._show_diagnostics()  # noqa: SLF001
    assert internal_calls[-1][1] == "Diagnóstico"

    monkeypatch.undo()
    modal = QDialog(window)
    QTimer.singleShot(0, modal.accept)
    assert (
        window._exec_internal_dialog(modal, "Modal")  # noqa: SLF001
        == QDialog.DialogCode.Accepted
    )
    assert window.parsezen_workspace.current_internal_widget is None

    window._set_dark_mode(False)  # noqa: SLF001
    window._handle_system_theme_change(object())  # noqa: SLF001
    window._set_theme_mode(ThemeMode.SYSTEM)  # noqa: SLF001
    window._handle_system_theme_change(object())  # noqa: SLF001
    window._refresh_theme_surfaces()  # noqa: SLF001
    window._toggle_theme()  # noqa: SLF001
    window.appearance_menu.hide()
    window._show_parsezen_settings()  # noqa: SLF001
    window.settings_menu.hide()

    window._add_dropped_paths((extra,))  # noqa: SLF001
    assert tuple(entry.path for entry in _entries(window)) == (source, extra)  # noqa: SLF001

    window._configure_job(job.id, None)  # noqa: SLF001
    editor = window._active_configuration_editor()  # noqa: SLF001
    assert editor is not None
    model = OllamaModel("qwen3:4b-instruct", "Qwen3 4B Instruct")
    window._model_discovery_succeeded(  # noqa: SLF001
        OllamaConnection(OllamaStatus.READY, (model,))
    )
    window._select_model_from_manager(model.model_id)  # noqa: SLF001
    editor._set_review_enabled(True)  # noqa: SLF001
    assert editor.configuration().ai.model == model.model_id
    assert editor.plan_reviewed.isChecked()
    editor.reject()

    entry = _entries(window)[0]  # noqa: SLF001
    entry.status = ProjectedStatus.REVIEW_PENDING
    entry.result = ProcessResult(tmp_path / "result.md")
    reviewed: list[str] = []
    opened: list[Path] = []
    configured: list[tuple[str, object]] = []
    monkeypatch.setattr(window, "_review_job", lambda job_id, _stage: reviewed.append(job_id))
    monkeypatch.setattr(window, "_open_local_path", opened.append)
    monkeypatch.setattr(
        window,
        "_configure_job",
        lambda job_id, stage: configured.append((job_id, stage)),
    )
    window._run_primary_action("review")  # noqa: SLF001
    window._run_primary_action("open_folder")  # noqa: SLF001
    window._run_primary_action("configure_result")  # noqa: SLF001
    assert reviewed == [job.id]
    assert opened == [entry.result.final_path.parent]
    assert configured
