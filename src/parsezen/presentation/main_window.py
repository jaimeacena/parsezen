"""Production Parsezen window backed by explicit application services."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from time import monotonic
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from platformdirs import user_data_path
from PySide6.QtCore import QEventLoop, QPoint, QSettings, Qt, QTimer, QUrl, Slot
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
    QWidget,
)

from parsezen import APP_DISPLAY_NAME, APP_STORAGE_NAME
from parsezen.application.configuration_rules import requires_ai
from parsezen.application.early_check import run_early_check
from parsezen.application.job_execution import JobExecutionController
from parsezen.application.job_outcomes import JobOutcomeCoordinator, OutcomeWarning
from parsezen.application.job_queue import JobQueue
from parsezen.application.job_runtime import JobRuntime
from parsezen.application.outcome_summary import build_outcome_summary
from parsezen.application.phase_review_sequence import PhaseReviewSequenceCoordinator
from parsezen.application.preflight import (
    DocumentPreflight,
    RuntimeEstimate,
    analyze_preflight,
    combine_preflights,
    estimate_remaining_time,
    processing_metric,
)
from parsezen.application.processing_explanation import linguistic_review_summary
from parsezen.application.quality_review_adapter import (
    apply_pdf_review,
    apply_translation_review,
)
from parsezen.application.queue_configuration import QueueConfigurationService
from parsezen.application.queue_persistence import (
    QueuePersistenceCoordinator,
    QueuePersistenceStatus,
)
from parsezen.application.review_finalization import (
    ReviewFinalizationCoordinator,
    ReviewFinalizationWarning,
)
from parsezen.application.review_materialization import (
    ReviewMaterializationService,
)
from parsezen.application.review_materialization import (
    previous_review_kind as _previous_review_kind,
)
from parsezen.application.review_publication import (
    PublishedReview,
    ReviewPublicationCoordinator,
)
from parsezen.application.review_recommendation import (
    rebind_review_recommendation,
    recommend_targeted_review,
    recommendation_summary,
    reconstruct_completed_result,
)
from parsezen.application.revision_materializer import (
    render_revision_reviews,
)
from parsezen.application.run_preparation import PreparedQueueRun
from parsezen.application.run_validation import BatchValidationIssue
from parsezen.application.runtime_mapping import (
    request_and_settings_from_job,
    stage_kind_from_process_stage,
)
from parsezen.application.scheduler import QueueRunPlan, RunMode
from parsezen.application.workspace_recovery import (
    SOURCE_CHANGED_MESSAGE as _SOURCE_CHANGED_MESSAGE,
)
from parsezen.application.workspace_recovery import (
    recover_workspace,
)
from parsezen.application.workspace_recovery import (
    source_is_unchanged as _source_is_unchanged,
)
from parsezen.branding import APP_ICON_PATH
from parsezen.diagnostics import build_diagnostic_report
from parsezen.domain.attempt_activity import AttemptTimeline, FailureSnapshot
from parsezen.domain.books import BookDocument
from parsezen.domain.estimates import ProcessingMetric
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
from parsezen.domain.outcomes import EarlyCheckReport, OutcomeSummary
from parsezen.domain.reviews import ReviewChoice, ReviewKind, ReviewSession, ReviewStatus
from parsezen.domain.stages import StageKind, StageStatus
from parsezen.errors import ParsezenError
from parsezen.failure_recovery import ProcessingFailure, recovery_plan
from parsezen.infrastructure.artifact_store import ArtifactStore
from parsezen.infrastructure.result_snapshots import ResultSnapshotStore
from parsezen.infrastructure.state_store import StateStore, StateStoreError
from parsezen.local_models import (
    OllamaModel,
    OllamaStatus,
    is_reasoning_model_id,
)
from parsezen.model_recommendations import ModelRecommendations
from parsezen.presentation.activity_view import ActivityView
from parsezen.presentation.book_editor_dialog import BookEditorDialog
from parsezen.presentation.design_system import (
    ThemeMode,
    apply_parsezen_theme,
    preferred_theme_mode,
)
from parsezen.presentation.diagnostics_dialog import DiagnosticsDialog
from parsezen.presentation.epub_confirmation_dialog import EpubConfirmationDialog
from parsezen.presentation.job_configuration_dialog import JobConfigurationDialog
from parsezen.presentation.local_ai_controller import LocalAIAction, LocalAIController
from parsezen.presentation.local_ai_workflow import (
    LOCAL_AI_WORKFLOW_METHODS,
    LocalAIWorkflow,
)
from parsezen.presentation.model_manager import ModelManagerDialog
from parsezen.presentation.phase_review_dialog import PhaseReviewDialog
from parsezen.presentation.preflight_dialog import PreflightDialog
from parsezen.presentation.preflight_runner import (
    ForecastBatch,
    PreflightRunner,
    forecast_cache_key,
)
from parsezen.presentation.processing_runner import ProcessingRunner
from parsezen.presentation.workspace import ParsezenWorkspace
from parsezen.processing import (
    ProcessRequest,
    ProcessResult,
    ProcessStage,
    clear_document_work_checkpoints,
    process_document,
    review_completed_result,
)
from parsezen.recent_activity import (
    RecentJob,
    RecentJobStatus,
    append_recent_jobs,
    clear_recent_jobs,
    load_recent_jobs,
)
from parsezen.revision import RevisionDraft, RevisionKind, build_revision_draft
from parsezen.settings import AppSettings, validate_settings
from parsezen.windows_integration import SystemSleepBlocker

_STATE_FILENAME = "workspace.sqlite3"
_PERSIST_INTERVAL_SECONDS = 1.0
_RECOVERY_WRITE_WARNING = (
    "No se pudo guardar la recuperación automática; Parsezen seguirá intentándolo."
)
_RECOVERY_READ_WARNING = (
    "La recuperación automática está protegida, pero no puede actualizarse porque "
    "el estado anterior no se pudo leer."
)

_AttemptActivity = tuple[str | None, AttemptTimeline, FailureSnapshot | None]
LOGGER = logging.getLogger(__name__)


class _ExecutableDialog(Protocol):
    def exec(self) -> int: ...


class ParsezenMainWindow(QMainWindow):
    """Document-by-stage workspace with one configuration per document."""

    def __init__(
        self,
        settings: AppSettings | None = None,
        on_settings_changed: Callable[[AppSettings], None] | None = None,
        startup_message: str | None = None,
        *,
        auto_discover_ai: bool = True,
        history_path: Path | None = None,
        work_checkpoint_root: Path | None = None,
        state_path: Path | None = None,
    ) -> None:
        super().__init__()
        self._settings = settings if settings is not None else AppSettings()
        self._on_settings_changed = on_settings_changed
        self._startup_message = startup_message
        self._history_path = history_path
        self._work_checkpoint_root = work_checkpoint_root
        self._job_queue = JobQueue()
        self._runtime_by_job: dict[str, JobRuntime] = {}
        self._current_job_id: str | None = None
        self._batch_running = False
        self._result: ProcessResult | None = None
        self._selected_result_job_id: str | None = None
        self._pause_requested = False
        self._is_processing = False
        self._targeted_review_active = False
        self._is_discovering_models = False
        self._is_recommending_models = False
        self._is_ai_setup_active = False
        self._auto_discover_ai = auto_discover_ai
        self._ollama_status: OllamaStatus | None = None
        self._ollama_models: tuple[OllamaModel, ...] = ()
        self._model_recommendations: ModelRecommendations | None = None
        self._model_manager: ModelManagerDialog | None = None
        self._active_configuration_dialog: JobConfigurationDialog | None = None
        self._ai_setup_action: LocalAIAction | None = None
        self._pending_ai_model: str | None = None
        self._pending_deleted_model: str | None = None
        self._ai_setup_succeeded = False
        self._sleep_blocker = SystemSleepBlocker()
        self._notification_tray: QSystemTrayIcon | None = None
        self._active_run_job_ids: tuple[str, ...] = ()
        self._early_check_reports: dict[str, EarlyCheckReport] = {}
        self._outcome_summaries: dict[str, OutcomeSummary] = {}
        self._latest_forecasts: dict[str, DocumentPreflight] = {}
        # One terminal snapshot per job keeps delayed review tied to its attempt.
        # It is pruned against the live queue in ``_sync_workspace``.
        self._terminal_attempt_activity: dict[str, _AttemptActivity] = {}
        self._local_ai_workflow = LocalAIWorkflow(self)

        self._processing_runner = ProcessingRunner(self)
        self._processing_runner.stage_changed.connect(self._show_stage)
        self._processing_runner.improvement_progress.connect(self._show_improvement_progress)
        self._processing_runner.early_check_started.connect(self._early_check_started)
        self._processing_runner.early_check_progress.connect(self._early_check_progress)
        self._processing_runner.early_check_completed.connect(self._early_check_completed)
        self._processing_runner.succeeded.connect(self._processing_succeeded)
        self._processing_runner.failed.connect(self._processing_failed)
        self._processing_runner.cancelled.connect(self._processing_cancelled)
        self._processing_runner.finished.connect(self._processing_worker_finished)
        self._local_ai = LocalAIController(self)
        self._local_ai.discovery_succeeded.connect(self._model_discovery_succeeded)
        self._local_ai.discovery_failed.connect(self._model_discovery_failed)
        self._local_ai.discovery_finished.connect(self._model_discovery_finished)
        self._local_ai.recommendations_succeeded.connect(self._model_recommendations_succeeded)
        self._local_ai.recommendations_failed.connect(self._model_recommendations_failed)
        self._local_ai.recommendations_finished.connect(self._model_recommendations_finished)
        self._local_ai.setup_progress.connect(self._ai_setup_progress_changed)
        self._local_ai.setup_succeeded.connect(self._ai_setup_succeeded_slot)
        self._local_ai.setup_cancelled.connect(self._ai_setup_cancelled)
        self._local_ai.setup_failed.connect(self._ai_setup_failed)
        self._local_ai.setup_finished.connect(self._ai_setup_finished)

        self.setWindowTitle(APP_DISPLAY_NAME)
        app_icon = QIcon(str(APP_ICON_PATH))
        if not app_icon.isNull():
            self.setWindowIcon(app_icon)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._notification_tray = QSystemTrayIcon(app_icon, self)
            self._notification_tray.setToolTip(APP_DISPLAY_NAME)
        self.setAcceptDrops(True)
        self.settings_menu = QMenu(self)
        self.settings_menu.setObjectName("settingsMenu")
        self._queue_configuration = QueueConfigurationService(self._job_queue)
        self._job_execution = JobExecutionController(self._job_queue)
        self._prepared_run: PreparedQueueRun | None = None
        self._last_projection: tuple[DocumentJob, ...] = ()
        self._state_recovery_notice: str | None = startup_message
        state_destination = (
            state_path or user_data_path(APP_STORAGE_NAME, appauthor=False) / _STATE_FILENAME
        )
        try:
            self._state_store = StateStore(state_destination)
        except StateStoreError:
            recovery_directory = _quarantine_structurally_unreadable_state(state_destination)
            self._state_recovery_notice = (
                "El estado anterior estaba dañado. Se conservó una copia local segura "
                f"({recovery_directory.name}) y Parsezen inició una cola limpia."
            )
            LOGGER.warning("structurally_unreadable_state_preserved")
            self._state_store = StateStore(state_destination)
        self._queue_persistence = QueuePersistenceCoordinator(
            self._state_store,
            interval_seconds=_PERSIST_INTERVAL_SECONDS,
        )
        self._processing_metrics: tuple[ProcessingMetric, ...] = ()
        try:
            self._processing_metrics = self._state_store.load_processing_metrics()
        except StateStoreError:
            LOGGER.warning("processing_estimate_history_unavailable")
        self._metrics_generation = 0
        self._forecast_cache: dict[
            tuple[object, ...],
            DocumentPreflight,
        ] = {}
        self._forecast_unavailable: set[tuple[object, ...]] = set()
        self._preparation_error_title = "No se pudo preparar el procesamiento"
        self._start_after_preparation = False
        self._preflight_runner = PreflightRunner(self)
        self._preflight_runner.preparation_succeeded.connect(self._preparation_succeeded)
        self._preflight_runner.preparation_failed.connect(self._preparation_failed)
        self._preflight_runner.preparation_cancelled.connect(self._preparation_cancelled)
        self._preflight_runner.forecasts_succeeded.connect(self._forecasts_succeeded)
        self._preflight_runner.forecasts_finished.connect(self._forecasts_finished)
        self._artifact_store = ArtifactStore(state_destination.parent / "artifacts")
        self._result_snapshots = ResultSnapshotStore(
            self._state_store,
            self._artifact_store,
        )
        self._job_outcomes = JobOutcomeCoordinator(
            self._job_queue,
            self._job_execution,
            self._result_snapshots,
        )
        self._review_finalization = ReviewFinalizationCoordinator(
            self._job_queue,
            self._job_execution,
            self._state_store,
            self._artifact_store,
        )
        self._review_publication = ReviewPublicationCoordinator(
            self._state_store,
            self._artifact_store,
            self._review_finalization,
        )
        self._phase_reviews = PhaseReviewSequenceCoordinator(
            self._job_queue,
            self._job_execution,
            self._state_store,
        )
        self._review_materialization = ReviewMaterializationService(
            self._state_store,
            self._artifact_store,
        )

        self._appearance_settings = QSettings("Parsezen", "Parsezen")
        self._theme_mode = preferred_theme_mode(self._appearance_settings)
        application = QApplication.instance()
        if isinstance(application, QApplication):
            apply_parsezen_theme(application, self._theme_mode)
            application.styleHints().colorSchemeChanged.connect(self._handle_system_theme_change)

        self.parsezen_workspace = ParsezenWorkspace(self)
        self.setCentralWidget(self.parsezen_workspace)
        self.setMinimumSize(320, 520)
        self.resize(1440, 860)
        self._connect_workspace()
        self._install_settings_actions()
        retained_artifact_jobs = self._restore_workspace()
        if retained_artifact_jobs is not None:
            self._prune_orphaned_review_artifacts(retained_artifact_jobs)
        self._projection_timer = QTimer(self)
        self._projection_timer.setInterval(200)
        self._projection_timer.timeout.connect(self._sync_workspace)
        self._projection_timer.start()
        self._sync_workspace(force_persist=True)

    def __getattr__(self, name: str) -> Any:
        workflow = self.__dict__.get("_local_ai_workflow")
        if workflow is not None and name in LOCAL_AI_WORKFLOW_METHODS:
            return getattr(workflow, name)
        raise AttributeError(f"{type(self).__name__!s} has no attribute {name!r}")

    def _start_ai_setup(
        self,
        action: LocalAIAction,
        *,
        model_id: str | None = None,
        expected_download_size_bytes: int | None = None,
    ) -> None:
        """Retain the established window hook while delegating the local-AI workflow."""
        self._local_ai_workflow._start_ai_setup(
            action,
            model_id=model_id,
            expected_download_size_bytes=expected_download_size_bytes,
        )

    @property
    def is_processing(self) -> bool:
        return self._is_processing

    def set_source_paths(self, paths: Sequence[str | Path]) -> None:
        selected = _unique_paths(Path(path) for path in paths)
        if not selected:
            return
        self._replace_sources(tuple(selected))
        self._current_job_id = None
        self._batch_running = False
        self.parsezen_workspace.clear_batch_summary()
        self._ensure_configurations()
        self._sync_workspace(force_persist=True)

    def add_source_paths(self, paths: Sequence[str | Path]) -> None:
        candidates = _unique_paths(Path(path) for path in paths)
        known = {_path_key(job.source.path) for job in self._job_queue.jobs}
        added = [path for path in candidates if _path_key(path) not in known]
        if not added:
            return
        self.parsezen_workspace.clear_batch_summary()
        self._replace_sources(tuple(job.source.path for job in self._job_queue.jobs) + tuple(added))
        self._sync_workspace(force_persist=True)

    def _replace_sources(self, paths: tuple[Path, ...]) -> None:
        """Make the domain queue the only owner of source identity and order."""

        self._job_queue.retain_sources(paths)
        for path in paths:
            if self._job_queue.for_source(path) is not None:
                continue
            key = _path_key(path)
            job = self._job_queue.add(
                _source_from_path(path),
                _default_configuration(path, self._settings),
                job_id=uuid5(NAMESPACE_URL, key).hex,
            )
            self._runtime_by_job[job.id] = JobRuntime()
        self._job_queue.order_sources(paths)
        active_ids = {job.id for job in self._job_queue.jobs}
        self._runtime_by_job = {
            job_id: runtime
            for job_id, runtime in self._runtime_by_job.items()
            if job_id in active_ids
        }
        for job_id in active_ids:
            self._runtime_by_job.setdefault(job_id, JobRuntime())
        if self._selected_result_job_id not in active_ids:
            self._selected_result_job_id = None
            self._result = None

    def _current_job(self) -> DocumentJob | None:
        return (
            self._job_queue.get(self._current_job_id) if self._current_job_id is not None else None
        )

    def _current_runtime(self) -> JobRuntime | None:
        return (
            self._runtime_by_job.get(self._current_job_id)
            if self._current_job_id is not None
            else None
        )

    def _current_source_path(self) -> Path | None:
        current = self._current_job()
        if current is not None:
            return current.source.path
        jobs = self._job_queue.jobs
        return jobs[0].source.path if jobs else None

    @Slot()
    def _start_processing(self) -> None:
        prepared = self._prepared_run
        if (
            prepared is None
            or not prepared.plan.job_ids
            or prepared.issues
            or self._batch_running
            or self._processing_runner.is_active
            or self._is_discovering_models
            or self._is_recommending_models
            or self._is_ai_setup_active
        ):
            self.parsezen_workspace.set_preparing_jobs(())
            return
        try:
            self._job_execution.begin_run(
                prepared.plan,
                allow_subset=prepared.explicit_plan,
            )
        except (RuntimeError, ValueError):
            self.parsezen_workspace.set_preparing_jobs(())
            self._job_execution.finish_run()
            self._prepared_run = None
            return
        resumable = set(prepared.plan.job_ids)
        for job_id in resumable:
            runtime = self._runtime_by_job.get(job_id)
            if runtime is None:
                continue
            runtime.reset_for_run()
        self._pause_requested = False
        self._batch_running = True
        self._is_processing = True
        self._active_run_job_ids = prepared.plan.job_ids
        self.parsezen_workspace.clear_batch_summary()
        self._sleep_blocker.start()
        self._current_job_id = None
        self._start_next_batch_entry()

    def _validate_pending_requests(
        self,
        items: tuple[tuple[ProcessRequest, AppSettings], ...],
    ) -> tuple[BatchValidationIssue, ...]:
        del items
        return self._prepared_run.issues if self._prepared_run is not None else ()

    def _pending_runtime_items(
        self,
        settings: AppSettings,
    ) -> tuple[tuple[ProcessRequest, AppSettings], ...]:
        del settings
        prepared = self._prepared_run
        return (
            tuple((item.request, item.settings) for item in prepared.items)
            if prepared is not None
            else ()
        )

    def _runtime_for_entry(
        self,
        job_id: str,
    ) -> tuple[ProcessRequest, AppSettings] | None:
        prepared = self._prepared_run
        job = self._job_queue.get(job_id)
        if prepared is not None and job is not None:
            item = prepared.item(job_id)
            if item is not None:
                return item.request, item.settings
        if job is None:
            return None
        return request_and_settings_from_job(
            job,
            timeout_seconds=self._settings.timeout_seconds,
            checkpoint_retention_days=self._settings.checkpoint_retention_days,
        )

    def _processing_run_flags(self) -> tuple[bool, bool]:
        plan = self._prepared_run.plan if self._prepared_run is not None else None
        mode = plan.mode if plan is not None else self._job_execution.plan_run().mode
        return mode is RunMode.RESUME, mode is RunMode.RETRY

    def _local_ai_required(self) -> bool:
        return any(
            job.status is JobStatus.QUEUED and requires_ai(job.configuration)
            for job in self._job_queue.jobs
        )

    def _next_pending_job(self) -> DocumentJob | None:
        """Claim the next domain job before constructing its physical worker."""

        return self._job_execution.start_next_available()

    def _start_next_batch_entry(self) -> None:
        job = self._next_pending_job()
        if job is None:
            self._finish_batch()
            return
        physical = self._runtime_for_entry(job.id)
        if physical is None:
            self._job_execution.fail(
                job.id,
                next(stage.kind for stage in job.stages if stage.status is StageStatus.RUNNING),
                error_code="missing_runtime",
                error_message="No se pudo reconstruir la configuración del documento.",
            )
            self._finish_batch()
            return
        request, settings = physical
        self._current_job_id = job.id
        runtime = self._runtime_by_job.setdefault(job.id, JobRuntime())
        runtime.reset_for_run()
        runtime.started_at = monotonic()
        prepared_item = self._prepared_run.item(job.id) if self._prepared_run is not None else None
        early_check = (
            run_early_check
            if prepared_item is not None
            and prepared_item.preflight is not None
            and prepared_item.preflight.early_check_recommended
            and not self._early_check_already_passed(job)
            else None
        )
        if early_check is None:
            self.parsezen_workspace.set_preparing_jobs(())
        else:
            self.parsezen_workspace.set_preparing_jobs(
                (job.id,),
                {job.id: "Comprobando 3 páginas representativas"},
                can_pause=True,
            )
        self._sync_workspace(force_persist=True)
        self._processing_runner.start(
            request,
            settings,
            work_checkpoint_root=self._work_checkpoint_root,
            processor=process_document,
            early_check=early_check,
        )

    def _finish_batch(self) -> None:
        active_ids = self._active_run_job_ids
        self._job_execution.finish_run()
        self._prepared_run = None
        self._batch_running = False
        self._is_processing = False
        self._targeted_review_active = False
        self._pause_requested = False
        self._current_job_id = None
        self._active_run_job_ids = ()
        self._sleep_blocker.stop()
        self.parsezen_workspace.set_preparing_jobs(())
        self._show_finished_batch_summary(active_ids)
        self._sync_workspace(force_persist=True)

    @Slot(object)
    def _show_stage(self, stage: ProcessStage) -> None:
        job = self._current_job()
        if job is not None:
            self._job_execution.advance(
                job.id,
                stage_kind_from_process_stage(stage),
            )
        runtime = self._current_runtime()
        if runtime is not None:
            previous_stage = runtime.stage
            runtime.stage = stage
            if previous_stage is not stage:
                runtime.stage_started_at = monotonic()
            runtime.progress_current = 0
            runtime.progress_total = 0
            if runtime.stages_seen is None:
                runtime.stages_seen = []
            if stage not in runtime.stages_seen:
                runtime.stages_seen.append(stage)
        self.parsezen_workspace.set_preparing_jobs(())
        self._sync_workspace()

    @Slot()
    def _early_check_started(self) -> None:
        job = self._current_job()
        if job is None:
            return
        self.parsezen_workspace.set_preparing_jobs(
            (job.id,),
            {job.id: "Comprobando páginas representativas"},
            can_pause=True,
        )

    @Slot(int, int)
    def _early_check_progress(self, current: int, total: int) -> None:
        job = self._current_job()
        if job is None or total < 1:
            return
        self.parsezen_workspace.set_preparing_jobs(
            (job.id,),
            {job.id: f"Comprobando {current} de {total} páginas representativas"},
            can_pause=True,
        )

    @Slot(object)
    def _early_check_completed(self, value: object) -> None:
        if not isinstance(value, EarlyCheckReport):
            return
        job = self._current_job()
        if job is None:
            return
        self._early_check_reports[job.id] = value
        if value.blocking:
            return
        try:
            self._state_store.upsert_job(
                job,
                event_type=self._early_check_marker(job),
            )
        except StateStoreError:
            LOGGER.warning("early_check_marker_write_failed")

    def _early_check_already_passed(self, job: DocumentJob) -> bool:
        try:
            return self._early_check_marker(job) in self._state_store.event_types(job.id)
        except StateStoreError:
            return False

    @staticmethod
    def _early_check_marker(job: DocumentJob) -> str:
        source_identity = job.source.content_sha256 or (
            f"legacy-{job.source.size_bytes}-{job.source.modified_ns}"
        )
        return (
            f"early_check_passed:{job.configuration_revision}:{source_identity}:"
            f"{job.source.size_bytes}:{job.source.modified_ns}"
        )

    @Slot(int, int)
    def _show_improvement_progress(self, current: int, total: int) -> None:
        if total < 1 or current < 0 or current > total:
            return
        job = self._current_job()
        runtime = self._current_runtime()
        if job is not None and runtime is not None and runtime.stage is not None:
            self._job_execution.report_progress(
                job.id,
                stage_kind_from_process_stage(runtime.stage),
                current,
                total,
                runtime.stage.value,
            )
        if runtime is not None:
            runtime.progress_current = current
            runtime.progress_total = total
        self._sync_workspace()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._preflight_runner.preparing:
            self._start_after_preparation = False
            self._preflight_runner.cancel_preparation()
            if self.isVisible():
                QMessageBox.information(
                    self,
                    "Preparaci\u00f3n en curso",
                    "Parsezen est\u00e1 cancelando la preparaci\u00f3n de forma segura.",
                )
            event.ignore()
            return
        if self._is_processing:
            if self.isVisible():
                QMessageBox.information(
                    self,
                    "Procesamiento en curso",
                    "Pausa o espera a que termine el documento antes de cerrar Parsezen.",
                )
            event.ignore()
            return
        if self._is_discovering_models or self._is_recommending_models or self._is_ai_setup_active:
            if self.isVisible():
                QMessageBox.information(
                    self,
                    "IA local en curso",
                    "Espera a que termine la operación local antes de cerrar Parsezen.",
                )
            event.ignore()
            return
        persisted = self._sync_workspace(force_persist=True)
        if (
            not persisted
            and not self._is_processing
            and self.isVisible()
            and self._has_unfinished_jobs()
            and QMessageBox.question(
                self,
                "Recuperación no disponible",
                "Los cambios de esta sesión no se han podido guardar para continuar "
                "después.\n\n¿Cerrar Parsezen de todos modos?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            event.ignore()
            return
        self._projection_timer.stop()
        self._sleep_blocker.stop()
        if self._notification_tray is not None:
            self._notification_tray.hide()
        QMainWindow.closeEvent(self, event)

    @Slot(object)
    def _processing_succeeded(self, result: ProcessResult) -> None:
        """Persist every reviewable result before the queue advances."""

        job = self._current_job()
        runtime = self._current_runtime()
        targeted_review = self._targeted_review_active
        activity = self._capture_terminal_attempt_activity(job.id if job is not None else None)
        self._remember_processing_duration(job, result)
        if job is not None:
            if targeted_review:
                job = self._job_queue.replace(replace(job, review_recommendation=None))
            elif (
                job.configuration.plan is ProcessingPlan.STANDARD and result.revision_draft is None
            ):
                recommendation = recommend_targeted_review(result)
                if recommendation is not None:
                    job = self._job_queue.replace(
                        replace(job, review_recommendation=recommendation)
                    )
        outcome = self._job_outcomes.resolve_success(job.id, result) if job is not None else None
        if runtime is not None:
            runtime.result = result
            runtime.finished_at = monotonic()
            runtime.stage_started_at = None
        self._result = result
        self._selected_result_job_id = job.id if job is not None else None
        if outcome is None:
            return
        if outcome.warning is OutcomeWarning.REVIEW_RECOVERY_UNAVAILABLE:
            QMessageBox.warning(
                self,
                "No se pudo guardar la revisión",
                "Podrás revisarla mientras Parsezen siga abierto, pero no se podrá "
                "recuperar tras cerrarlo.",
            )
        elif outcome.warning is OutcomeWarning.STALE_SNAPSHOT_NOT_REMOVED:
            LOGGER.warning("stale_result_snapshot_cleanup_failed")
        current_job = self._job_queue.get(job.id) if job is not None else None
        if (
            runtime is not None
            and current_job is not None
            and current_job.status is JobStatus.COMPLETED
        ):
            self._record_completed_outcome(current_job, runtime, result, activity=activity)
        self._sync_workspace(force_persist=True)

    @Slot(object)
    def _processing_failed(self, value: object) -> None:
        job = self._current_job()
        runtime = self._current_runtime()
        activity = self._capture_terminal_attempt_activity(job.id if job is not None else None)
        stage_kind = stage_kind_from_process_stage(runtime.stage if runtime is not None else None)
        failure = (
            value
            if isinstance(value, ProcessingFailure)
            else ProcessingFailure.from_saved(
                None,
                str(value),
                stage=stage_kind,
            )
        )
        if self._targeted_review_active and job is not None:
            self._job_execution.abort_targeted_review(job.id)
            if runtime is not None:
                runtime.finished_at = monotonic()
                runtime.stage_started_at = None
            QMessageBox.warning(
                self,
                "No se pudo completar la revisión sugerida",
                "El resultado anterior sigue intacto. Puedes abrirlo o volver a intentar la "
                "revisión con IA más tarde.",
            )
            self._sync_workspace(force_persist=True)
            return
        if job is not None:
            self._job_outcomes.resolve_failure(
                job.id,
                stage_kind,
                failure.message,
                error_code=failure.kind.value,
            )
        if runtime is not None:
            runtime.finished_at = monotonic()
            runtime.stage_started_at = None
        self.parsezen_workspace.set_preparing_jobs(())
        if job is not None:
            self._record_recent_job(job, RecentJobStatus.FAILED, activity=activity)
        self._sync_workspace(force_persist=True)
        if job is not None:
            self._show_job_error(job.id, stage_kind)

    @Slot()
    def _processing_cancelled(self) -> None:
        pause_requested = self._pause_requested
        job = self._current_job()
        runtime = self._current_runtime()
        activity = self._capture_terminal_attempt_activity(job.id if job is not None else None)
        if self._targeted_review_active and job is not None:
            self._job_execution.abort_targeted_review(job.id)
            if runtime is not None:
                runtime.finished_at = monotonic()
                runtime.stage_started_at = None
            self.parsezen_workspace.set_preparing_jobs(())
            self._sync_workspace(force_persist=True)
            return
        if job is not None:
            self._job_outcomes.resolve_cancellation(job.id, paused=pause_requested)
        if runtime is not None:
            runtime.finished_at = monotonic()
            runtime.stage_started_at = None
            if not pause_requested:
                try:
                    physical = self._runtime_for_entry(job.id) if job is not None else None
                    if physical is not None:
                        request, settings = physical
                        clear_document_work_checkpoints(
                            request,
                            settings,
                            root=self._work_checkpoint_root,
                        )
                except OSError:
                    LOGGER.warning("cancelled_job_checkpoint_cleanup_failed")
        self.parsezen_workspace.set_preparing_jobs(())
        if job is not None and not pause_requested:
            self._record_recent_job(job, RecentJobStatus.CANCELLED, activity=activity)
        self._sync_workspace(force_persist=True)

    @Slot()
    def _processing_worker_finished(self) -> None:
        if self._pause_requested:
            self._finish_batch()
            return
        if self._batch_running and self._job_execution.plan_run().job_ids:
            self._current_job_id = None
            self._start_next_batch_entry()
            return
        self._finish_batch()

    def _connect_workspace(self) -> None:
        workspace = self.parsezen_workspace
        workspace.add_requested.connect(self._select_file)
        workspace.files_dropped.connect(self._add_dropped_paths)
        workspace.settings_requested.connect(self._show_parsezen_settings)
        workspace.local_ai_requested.connect(self._show_model_manager)
        workspace.theme_toggle_requested.connect(self._toggle_theme)
        workspace.output_directory_requested.connect(self._choose_global_output_directory)
        workspace.output_directory_reset_requested.connect(self._reset_global_output_directory)
        workspace.internal_back_requested.connect(self._close_internal_workflow)
        workspace.configure_requested.connect(self._configure_job)
        workspace.review_requested.connect(self._review_job)
        workspace.ai_review_requested.connect(self._start_targeted_ai_review)
        workspace.error_requested.connect(self._show_job_error)
        workspace.retry_requested.connect(self._retry_failed_job)
        workspace.open_result_requested.connect(self._open_job_result)
        workspace.open_folder_requested.connect(self._open_job_result_folder)
        workspace.result_summary_requested.connect(self._show_result_summary)
        workspace.activity_requested.connect(self._show_recent_activity)
        workspace.remove_requested.connect(self._remove_job)
        workspace.move_requested.connect(self._move_job)
        workspace.primary_requested.connect(self._run_primary_action)

    def _install_settings_actions(self) -> None:
        self.settings_menu.clear()
        self.models_settings_action = QAction("IA local", self.settings_menu)
        self.models_settings_action.triggered.connect(self._show_model_manager)
        self.settings_menu.addAction(self.models_settings_action)
        self.activity_action = QAction("Actividad reciente", self.settings_menu)
        self.activity_action.triggered.connect(self._show_recent_activity)
        self.settings_menu.addAction(self.activity_action)
        self.settings_menu.addSeparator()

        self.checkpoint_retention_menu = self.settings_menu.addMenu("Conservar trabajo temporal")
        self.checkpoint_retention_menu.setToolTipsVisible(True)
        self.checkpoint_retention_group = QActionGroup(self)
        self.checkpoint_retention_group.setExclusive(True)
        retention_labels = {
            0: "No conservar al finalizar",
            7: "7 días",
            30: "30 días",
            90: "90 días",
        }
        self.checkpoint_retention_actions: dict[int, QAction] = {}
        for days, label in retention_labels.items():
            action = QAction(label, self.checkpoint_retention_menu)
            action.setCheckable(True)
            action.setChecked(self._settings.checkpoint_retention_days == days)
            action.setToolTip(
                "Los datos se guardan cifrados y solo para poder reanudar trabajo local."
            )
            action.triggered.connect(
                lambda checked, value=days: (
                    self._set_checkpoint_retention(value) if checked else None
                )
            )
            self.checkpoint_retention_group.addAction(action)
            self.checkpoint_retention_menu.addAction(action)
            self.checkpoint_retention_actions[days] = action

        self.appearance_menu = self.settings_menu.addMenu("Apariencia")
        self.appearance_group = QActionGroup(self)
        self.appearance_group.setExclusive(True)
        appearance_labels = {
            ThemeMode.SYSTEM: "Seguir el sistema",
            ThemeMode.LIGHT: "Claro",
            ThemeMode.DARK: "Oscuro",
        }
        self.appearance_actions: dict[ThemeMode, QAction] = {}
        for mode, label in appearance_labels.items():
            action = QAction(label, self.appearance_menu)
            action.setCheckable(True)
            action.setChecked(self._theme_mode is mode)
            action.triggered.connect(
                lambda checked, value=mode: self._set_theme_mode(value) if checked else None
            )
            self.appearance_group.addAction(action)
            self.appearance_menu.addAction(action)
            self.appearance_actions[mode] = action
        self.dark_mode_action = self.appearance_actions[ThemeMode.DARK]

        self.diagnostics_action = QAction("Diagnóstico", self.settings_menu)
        self.diagnostics_action.triggered.connect(self._show_diagnostics)
        self.settings_menu.addAction(self.diagnostics_action)

    def _set_checkpoint_retention(self, days: int) -> None:
        settings = self._apply_settings(replace(self._settings, checkpoint_retention_days=days))
        if settings is None:
            current = self.checkpoint_retention_actions.get(
                self._settings.checkpoint_retention_days
            )
            if current is not None:
                current.setChecked(True)

    def _apply_settings(self, candidate: AppSettings) -> AppSettings | None:
        try:
            settings = validate_settings(candidate)
            if self._on_settings_changed is not None:
                self._on_settings_changed(settings)
        except ParsezenError as exc:
            LOGGER.warning("settings_update_failed error_type=%s", type(exc).__name__)
            QMessageBox.warning(self, "Configuración no válida", str(exc))
            return None
        self._settings = settings
        self.parsezen_workspace.set_output_directory(settings.output_directory)
        self.parsezen_workspace.set_local_ai_status(
            self._ollama_status,
            settings.model,
        )
        return settings

    def _set_theme_mode(self, mode: ThemeMode) -> None:
        self._theme_mode = mode
        self._appearance_settings.setValue("appearance/theme", self._theme_mode.value)
        application = QApplication.instance()
        if isinstance(application, QApplication):
            apply_parsezen_theme(application, self._theme_mode)
        self._refresh_theme_surfaces()

    @Slot(bool)
    def _set_dark_mode(self, enabled: bool) -> None:
        """Compatibility slot for callers of the former binary preference."""

        self._set_theme_mode(ThemeMode.DARK if enabled else ThemeMode.LIGHT)

    @Slot(object)
    def _handle_system_theme_change(self, _scheme: object) -> None:
        if self._theme_mode is not ThemeMode.SYSTEM:
            return
        application = QApplication.instance()
        if isinstance(application, QApplication):
            apply_parsezen_theme(application, ThemeMode.SYSTEM)
        self._refresh_theme_surfaces()

    def _refresh_theme_surfaces(self) -> None:
        application = QApplication.instance()
        if not isinstance(application, QApplication):
            return
        for widget in application.allWidgets():
            refresh = getattr(widget, "apply_theme", None)
            if callable(refresh):
                refresh()

    @Slot()
    def _toggle_theme(self) -> None:
        menu_size = self.appearance_menu.sizeHint()
        button = self.parsezen_workspace.theme_button
        bottom_right = button.mapToGlobal(QPoint(button.width(), button.height()))
        self.appearance_menu.popup(
            QPoint(bottom_right.x() - menu_size.width(), bottom_right.y() + 4)
        )

    @Slot(object)
    def _add_dropped_paths(self, paths: object) -> None:
        if not isinstance(paths, tuple):
            return
        if self._job_queue.jobs:
            self.add_source_paths(paths)
        else:
            self.set_source_paths(paths)

    @Slot()
    def _select_file(self) -> None:
        source = self._current_source_path()
        initial_directory = source.parent if source is not None else Path.home()
        filenames, _selected_filter = QFileDialog.getOpenFileNames(
            self,
            "Seleccionar documentos",
            str(initial_directory),
            "Documentos compatibles (*.txt *.md *.markdown *.docx *.pdf *.epub)",
        )
        if not filenames:
            return
        if self._job_queue.jobs:
            self.add_source_paths(filenames)
        else:
            self.set_source_paths(filenames)

    @Slot()
    def _show_parsezen_settings(self) -> None:
        menu_size = self.settings_menu.sizeHint()
        button = self.parsezen_workspace.theme_button
        bottom_right = button.mapToGlobal(QPoint(button.width(), button.height()))
        self.settings_menu.popup(QPoint(bottom_right.x() - menu_size.width(), bottom_right.y() + 4))

    @Slot()
    def _show_diagnostics(self) -> None:
        report = build_diagnostic_report(
            self._settings,
            ollama_status=(self._ollama_status.value if self._ollama_status is not None else None),
            installed_models=len(self._ollama_models),
            queued_documents=len(self._job_queue.jobs),
            history_path=self._history_path,
            work_checkpoint_root=self._work_checkpoint_root,
        )
        dialog = DiagnosticsDialog(report, parent=self)
        self._exec_internal_dialog(dialog, "Diagnóstico")

    @Slot()
    def _show_recent_activity(self) -> None:
        jobs = load_recent_jobs(path=self._history_path)
        current_job_ids: dict[Path | str, str] = {
            _path_key(job.source.path): job.id
            for job in self._job_queue.jobs
            if job.status is JobStatus.FAILED
        }
        view = ActivityView(jobs, current_job_ids=current_job_ids, parent=self)
        view.open_requested.connect(self._open_local_path)
        view.folder_requested.connect(self._open_local_path)
        view.return_to_document_requested.connect(
            lambda job_id, activity_view=view: self._return_to_current_failure(
                activity_view,
                job_id,
            )
        )
        view.clear_requested.connect(lambda: self._clear_recent_activity(view))
        self.parsezen_workspace.show_internal_view(view, "Actividad reciente", scroll=True)

    def _return_to_current_failure(self, view: ActivityView, job_id: str) -> None:
        self.parsezen_workspace.close_internal_view(view)
        job = self._job_for_id(job_id)
        if job is None or job.status is not JobStatus.FAILED:
            return
        self._show_job_error(job_id, None)

    def _clear_recent_activity(self, view: ActivityView) -> None:
        if not view.jobs:
            return
        if (
            QMessageBox.question(
                self,
                "Borrar actividad reciente",
                "Se borrará únicamente esta lista local. "
                "Los documentos y sus resultados no se modificarán.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        clear_recent_jobs(path=self._history_path)
        view.set_jobs(())

    @Slot(str)
    def _show_result_summary(self, job_id: str) -> None:
        job = self._job_for_id(job_id)
        entry = self._entry_for_job_id(job_id)
        if job is None or entry is None or entry.result is None:
            return
        summary = self._outcome_summaries.get(job_id)
        if summary is None:
            summary = self._build_outcome_summary(job, entry)
        activity = self._activity_for_job(job.id)
        recent = RecentJob(
            source_path=job.source.path,
            status=RecentJobStatus.COMPLETED,
            finished_at=datetime.now(UTC),
            result_path=entry.result.final_path,
            summary=summary,
            attempt_id=activity[0] if activity is not None else None,
            timeline=activity[1] if activity is not None else AttemptTimeline(),
        )
        view = ActivityView((recent,), allow_clear=False, parent=self)
        view.open_requested.connect(self._open_local_path)
        view.folder_requested.connect(self._open_local_path)
        self.parsezen_workspace.show_internal_view(view, "Resumen del resultado")

    @Slot(str)
    def _start_targeted_ai_review(self, job_id: str) -> None:
        """Act on a quality recommendation without reprocessing unaffected content."""

        if self._is_processing or self._processing_runner.is_active:
            return
        job = self._job_for_id(job_id)
        entry = self._entry_for_job_id(job_id)
        if (
            job is None
            or entry is None
            or job.status is not JobStatus.COMPLETED
            or job.review_recommendation is None
        ):
            return
        if not _source_is_unchanged(job.source):
            QMessageBox.warning(self, "El original ha cambiado", _SOURCE_CHANGED_MESSAGE)
            return
        model = self._settings.model or job.configuration.ai.model
        if model is None:
            QMessageBox.information(
                self,
                "Hace falta un modelo local",
                "Instala o elige un modelo de IA local antes de revisar estas señales.",
            )
            self._show_model_manager()
            return
        try:
            base_result = (
                entry.result
                if entry.result is not None and entry.result.review_markdown
                else reconstruct_completed_result(job)
            )
            request, runtime_settings = request_and_settings_from_job(
                job,
                timeout_seconds=self._settings.timeout_seconds,
                checkpoint_retention_days=self._settings.checkpoint_retention_days,
            )
            runtime_settings = replace(
                runtime_settings,
                model=model,
                context_window=self._settings.context_window or job.configuration.ai.context_window,
            )
            validate_settings(runtime_settings)
            self._job_execution.begin_targeted_review(job.id)
        except (OSError, ParsezenError, ValueError) as exc:
            QMessageBox.warning(self, "No se pudo iniciar la revisión", str(exc))
            return

        entry.result = base_result
        entry.stage = None
        entry.progress_current = 0
        entry.progress_total = 0
        entry.stages_seen = []
        entry.started_at = monotonic()
        entry.stage_started_at = None
        entry.finished_at = None
        self._current_job_id = job.id
        self._targeted_review_active = True
        self._batch_running = True
        self._is_processing = True
        self._active_run_job_ids = (job.id,)
        self._pause_requested = False
        self.parsezen_workspace.clear_batch_summary()
        self.parsezen_workspace.show_batch_summary(
            "Revisión local iniciada. " + recommendation_summary(job.review_recommendation),
            tone="warning",
        )
        self._sleep_blocker.start()
        self._sync_workspace(force_persist=True)
        self._processing_runner.start(
            request,
            runtime_settings,
            work_checkpoint_root=self._work_checkpoint_root,
            processor=partial(
                review_completed_result,
                base_result=base_result,
                recommendation=job.review_recommendation,
            ),
        )

    @Slot(str, object)
    def _configure_job(self, job_id: str, requested_stage: object) -> None:
        entry = self._entry_for_job_id(job_id)
        job = self._job_for_id(job_id)
        if entry is None or job is None:
            return
        if job.status not in {JobStatus.QUEUED, JobStatus.FAILED, JobStatus.CANCELLED}:
            QMessageBox.information(
                self,
                "Documento bloqueado",
                "La configuración queda bloqueada al comenzar. "
                "Duplica o reinicia el trabajo para usar otras opciones.",
            )
            return
        models = tuple(
            (model.model_id, model.display_name)
            for model in self._ollama_models
            if not is_reasoning_model_id(model.model_id)
        )
        configured_model = job.configuration.ai.model
        if (
            configured_model
            and not is_reasoning_model_id(configured_model)
            and configured_model not in {model_id for model_id, _ in models}
        ):
            models = ((configured_model, configured_model), *models)
        stage = requested_stage if isinstance(requested_stage, StageKind) else None
        active_dialog = self._active_configuration_dialog
        if active_dialog is not None:
            return
        dialog = JobConfigurationDialog(
            job,
            models=models,
            stage=stage,
            embedded=True,
            default_output_directory=self._settings.output_directory,
            default_ai_model=self._settings.model,
            default_ai_context=self._settings.context_window,
            ollama_status=self._ollama_status,
            parent=self.parsezen_workspace,
        )
        self._active_configuration_dialog = dialog
        dialog.configuration_changed.connect(
            lambda job_id=job.id, editor=dialog: self._save_job_configuration(job_id, editor)
        )
        dialog.finished.connect(
            lambda _result, editor=dialog: self._configuration_dialog_finished(editor)
        )
        dialog.models_requested.connect(
            lambda editor=dialog: self._open_models_from_configuration(editor)
        )
        self.parsezen_workspace.set_configuring(job.id, None)
        self.parsezen_workspace.show_internal_view(
            dialog,
            f"Configurar · {job.source.path.name}",
        )
        dialog.persist_if_valid()
        if (
            self._auto_discover_ai
            and (dialog.review_enabled or requires_ai(job.configuration))
            and not self._ollama_models
            and not self._is_discovering_models
            and not self._is_recommending_models
            and not self._is_ai_setup_active
        ):
            self._start_model_discovery(automatic=True)

    def _configuration_dialog_finished(self, dialog: JobConfigurationDialog) -> None:
        if self._active_configuration_dialog is not dialog:
            return
        self.parsezen_workspace.close_internal_view(dialog)
        self._active_configuration_dialog = None
        self.parsezen_workspace.set_configuring(None, None)

    def _open_models_from_configuration(self, dialog: JobConfigurationDialog) -> None:
        if self._active_configuration_dialog is not dialog:
            return
        self._show_model_manager()
        manager = self._model_manager
        if manager is None:
            return
        manager.finished.connect(
            lambda _result, editor=dialog: self._resume_configuration_dialog(editor)
        )

    def _resume_configuration_dialog(self, dialog: JobConfigurationDialog) -> None:
        if self._active_configuration_dialog is not dialog:
            return
        dialog.set_models(
            tuple(
                (model.model_id, model.display_name)
                for model in self._ollama_models
                if not is_reasoning_model_id(model.model_id)
            )
        )
        dialog.set_default_ai_profile(
            self._settings.model,
            self._settings.context_window,
        )
        dialog.set_ai_status(self._ollama_status)
        dialog.persist_if_valid()

    @Slot(object)
    def _close_internal_workflow(self, widget: object) -> None:
        """Route the shared back action through each workflow's safe exit."""

        if isinstance(widget, JobConfigurationDialog):
            widget.reject()
        elif isinstance(widget, QDialog):
            widget.reject()
        elif isinstance(widget, QWidget):
            self.parsezen_workspace.close_internal_view(widget)

    def _exec_internal_dialog(
        self,
        dialog: QDialog | _ExecutableDialog,
        title: str,
    ) -> int:
        """Run an existing modal contract as a full internal workspace page."""

        if not isinstance(dialog, QDialog):
            return dialog.exec()
        if isinstance(dialog, PhaseReviewDialog):
            title = dialog.internal_page_title
        elif isinstance(dialog, BookEditorDialog):
            title = "Revisión final del EPUB"
        dialog.setModal(False)
        dialog.setWindowFlags(Qt.WindowType.Widget)
        loop = QEventLoop(self)
        dialog.finished.connect(loop.quit)
        self.parsezen_workspace.show_internal_view(dialog, title)
        loop.exec()
        result = dialog.result()
        self.parsezen_workspace.close_internal_view(dialog)
        return result

    @Slot()
    def _choose_global_output_directory(self) -> None:
        previous = self._settings.output_directory
        self._select_output_directory()
        current = self._settings.output_directory
        self._propagate_global_output_directory(previous, current)

    @Slot()
    def _reset_global_output_directory(self) -> None:
        previous = self._settings.output_directory
        self._reset_output_directory()
        self._propagate_global_output_directory(
            previous,
            self._settings.output_directory,
        )

    @Slot()
    def _select_output_directory(self) -> None:
        current = self._settings.output_directory
        if current is None or not current.is_dir():
            source = self._current_source_path()
            current = source.parent if source is not None else Path.home()
        directory = QFileDialog.getExistingDirectory(
            self,
            "Elegir carpeta de salida",
            str(current),
        )
        if not directory:
            return
        destination = Path(directory)
        if not destination.is_dir():
            QMessageBox.warning(
                self,
                "Carpeta no disponible",
                "La carpeta de salida seleccionada no existe.",
            )
            return
        self._apply_settings(replace(self._settings, output_directory=destination))

    @Slot()
    def _reset_output_directory(self) -> None:
        if self._settings.output_directory is not None:
            self._apply_settings(replace(self._settings, output_directory=None))

    def _propagate_global_output_directory(
        self,
        previous: Path | None,
        current: Path | None,
    ) -> None:
        self.parsezen_workspace.set_output_directory(current)
        self._queue_configuration.propagate_output_directory(previous, current)
        self._sync_workspace(force_persist=True)

    def _save_job_configuration(
        self,
        job_id: str,
        dialog: JobConfigurationDialog,
    ) -> None:
        entry = self._entry_for_job_id(job_id)
        job = self._job_for_id(job_id)
        if entry is None or job is None:
            dialog.reject()
            return
        try:
            configuration = dialog.configuration()
            configured_job = job.with_configuration(configuration)
            request = None
            if configured_job.is_configured:
                request, _runtime_settings = request_and_settings_from_job(
                    configured_job,
                    timeout_seconds=self._settings.timeout_seconds,
                    checkpoint_retention_days=self._settings.checkpoint_retention_days,
                )
        except ValueError as exc:
            QMessageBox.warning(self, "Configuración no válida", str(exc))
            return
        self._job_queue.replace(configured_job)
        entry.pdf_page_range = request.pdf_page_range if request is not None else None
        entry.result = None
        self._sync_workspace(force_persist=True)
        dialog.mark_persisted(configured_job)

    def _apply_configuration_to_compatible_jobs(
        self,
        source_job: DocumentJob,
        configuration: JobConfiguration,
    ) -> None:
        """Apply shared choices without overwriting source-specific PDF ranges."""

        updates = self._queue_configuration.apply_to_compatible_jobs(
            source_job,
            configuration,
            timeout_seconds=self._settings.timeout_seconds,
            checkpoint_retention_days=self._settings.checkpoint_retention_days,
        )
        for update in updates:
            entry = self._entry_for_job_id(update.job.id)
            if entry is None:
                continue
            entry.pdf_page_range = update.pdf_page_range
            entry.result = None

    @Slot(str, object)
    def _show_job_error(self, job_id: str, requested_stage: object) -> None:
        job = self._job_for_id(job_id)
        stage_kind = _coerce_stage_kind(requested_stage)
        if job is None:
            return
        if stage_kind is None or job.stage(stage_kind).status is not StageStatus.FAILED:
            failed_stage = next(
                (candidate for candidate in job.stages if candidate.status is StageStatus.FAILED),
                None,
            )
            if failed_stage is None:
                return
            stage_kind = failed_stage.kind
        stage = job.stage(stage_kind)
        details = stage.error_message or "No se registraron más detalles sobre este error."
        failure = ProcessingFailure.from_saved(
            stage.error_code,
            details,
            stage=stage_kind,
        )
        self.parsezen_workspace.show_job_error(
            job.id,
            recovery_plan(failure, stage=stage_kind),
            stage_kind,
        )

    @Slot(str, object)
    def _review_job(self, job_id: str, _stage: object) -> None:
        entry = self._entry_for_job_id(job_id)
        job = self._job_for_id(job_id)
        if entry is None or job is None or entry.result is None:
            return
        if not _source_is_unchanged(job.source):
            self._invalidate_changed_source_review(entry, job)
            QMessageBox.warning(self, "El original ha cambiado", _SOURCE_CHANGED_MESSAGE)
            self._sync_workspace(force_persist=True)
            return
        phase_input = self._review_quality_phases(entry, job)
        if phase_input is None:
            return
        reviewed_source, quality_reviews = phase_input
        draft = entry.result.revision_draft
        if (
            draft is None
            and job.configuration.plan is ProcessingPlan.LOCAL_AI_REVIEWED
            and job.configuration.output.format is DocumentFormat.EPUB
            and entry.result.final_path.suffix.casefold() == ".epub"
            and entry.result.revision_epub_metadata is not None
        ):
            draft = build_revision_draft(
                reviewed_source,
                reviewed_source,
                kinds=frozenset({RevisionKind.STRUCTURE}),
            )
        if draft is not None:
            if reviewed_source != draft.original_markdown:
                draft = build_revision_draft(
                    reviewed_source,
                    draft.proposed_markdown,
                    kinds=draft.kinds,
                )
            self._review_revision_by_phase(
                entry,
                job,
                draft=draft,
                preceding_reviews=quality_reviews,
            )
        elif (
            entry.result.final_path.suffix.casefold() == ".epub"
            and entry.result.revision_epub_metadata is not None
        ):
            self._personalize_epub(
                entry,
                job,
                reviewed_source,
                quality_reviews,
            )
        elif quality_reviews:
            self._apply_reviewed_text(
                entry,
                entry.result,
                reviewed_source,
                quality_reviews,
            )
        else:
            self._open_local_path(entry.result.final_path)
        self._sync_workspace(force_persist=True)

    def _confirm_epub_book(
        self,
        book: BookDocument,
        *,
        job_id: str,
        destination: Path,
    ) -> tuple[BookDocument | None, BookDocument | None]:
        """Return either a book to publish or a draft that must remain recoverable."""

        confirmation = EpubConfirmationDialog(
            book,
            self._artifact_store,
            job_id=job_id,
            parent=self,
        )
        confirmed = (
            self._exec_internal_dialog(confirmation, "Confirmar libro EPUB")
            == QDialog.DialogCode.Accepted
        )
        if not confirmed:
            return None, confirmation.book if confirmation.saved_for_later else None
        if not confirmation.open_editor_requested:
            return confirmation.book, None

        editor = BookEditorDialog(
            confirmation.book,
            self._artifact_store,
            job_id=job_id,
            destination=destination,
            publish_on_accept=False,
            parent=self,
        )
        accepted = (
            self._exec_internal_dialog(editor, "Editor EPUB completo")
            == QDialog.DialogCode.Accepted
        )
        if accepted:
            return editor.book, None
        return None, editor.book if editor.saved_for_later else None

    def _personalize_epub(
        self,
        entry: JobRuntime,
        job: DocumentJob,
        reviewed_text: str,
        reviews: tuple[ReviewSession, ...],
    ) -> None:
        result = entry.result
        if result is None:
            return
        self._sync_workspace(force_persist=True)
        try:
            book = self._review_publication.prepare_book(job.id, result, reviewed_text)
        except (ParsezenError, StateStoreError, ValueError, OSError) as exc:
            self._keep_review_pending(entry, result)
            QMessageBox.warning(self, "No se pudo preparar el editor", str(exc))
            return
        try:
            publishable_book, saved_draft = self._confirm_epub_book(
                book,
                job_id=job.id,
                destination=result.final_path,
            )
        except (ParsezenError, ValueError, OSError) as exc:
            self._keep_review_pending(entry, result)
            QMessageBox.warning(self, "No se pudo abrir el editor", str(exc))
            return
        if publishable_book is None:
            if saved_draft is not None:
                try:
                    self._review_publication.save_book(job.id, saved_draft)
                except (StateStoreError, ValueError, OSError) as exc:
                    QMessageBox.warning(
                        self,
                        "No se pudo guardar el borrador",
                        str(exc),
                    )
            self._keep_review_pending(entry, result)
            return
        try:
            published = self._review_publication.publish_book(
                job.id,
                result,
                reviewed_text,
                publishable_book,
                reviews,
            )
        except (ParsezenError, StateStoreError, OSError, ValueError) as exc:
            self._publication_failed(entry, result, exc)
            return
        self._accept_published_review(entry, published)

    def _review_quality_phases(
        self,
        entry: JobRuntime,
        job: DocumentJob,
    ) -> tuple[str, tuple[ReviewSession, ...]] | None:
        result = entry.result
        if result is None:
            return None
        text = (
            result.revision_draft.original_markdown
            if result.revision_draft is not None
            else result.review_markdown
        )
        if text is None:
            return ("", ())
        try:
            preparation = self._review_materialization.materialize_quality(
                job,
                result,
                job.source.path,
                text,
            )
        except (ParsezenError, StateStoreError, OSError, ValueError) as exc:
            QMessageBox.warning(self, "No se pudo preparar la revisión", str(exc))
            return None
        saved = preparation.saved_reviews
        materialized = preparation.materialized
        phase_plan = preparation.phase_plan
        reviewed_text = preparation.reviewed_text
        # A pending quality dialog leaves ``reviewed_text`` at an intermediate boundary;
        # later candidates must wait for the final rebuilt draft after that dialog applies.
        if preparation.complete:
            post_quality_draft = self._rebuild_revision_draft_after_quality(result, reviewed_text)
            try:
                self._review_materialization.ensure_revision_candidates(
                    post_quality_draft,
                    job,
                    saved,
                )
            except (ParsezenError, StateStoreError, ValueError, OSError) as exc:
                QMessageBox.warning(self, "No se pudo preparar la revisión", str(exc))
                return None

        try:
            self._phase_reviews.reconcile(job.id, result)
        except (KeyError, StateStoreError, ValueError) as exc:
            QMessageBox.warning(self, "No se pudo recuperar la revisión", str(exc))
            return None

        completed: list[ReviewSession] = []
        for kind, materialized_review in materialized:
            if materialized_review is None:
                continue
            review = materialized_review
            if review.status is ReviewStatus.APPLIED:
                completed.append(review)
                continue
            try:
                self._phase_reviews.prepare(job.id, result, review)
            except (StateStoreError, ValueError) as exc:
                QMessageBox.warning(self, "No se pudo preparar la revisión", str(exc))
                return None
            previous_kind = _previous_review_kind(phase_plan, kind)
            previous_requested = False

            def reopen_previous(previous_kind: ReviewKind | None = previous_kind) -> bool:
                nonlocal previous_requested
                if previous_kind is None:
                    return False
                try:
                    self._phase_reviews.reopen_review(
                        job.id,
                        result,
                        kind=previous_kind,
                    )
                except (KeyError, StateStoreError, ValueError) as exc:
                    QMessageBox.warning(self, "No se pudo volver a la fase anterior", str(exc))
                    return False
                previous_requested = True
                return True

            dialog = PhaseReviewDialog(
                review,
                self._artifact_store,
                phase_plan=phase_plan,
                translation_follows_ocr=job.configuration.translation.enabled,
                linguistic_review_context=(
                    linguistic_review_summary(result.linguistic_review_coverage)
                    if review.kind is ReviewKind.TRANSLATION
                    else None
                ),
                previous_phase_callback=reopen_previous if previous_kind is not None else None,
                parent=self,
            )
            if (
                self._exec_internal_dialog(dialog, "Revisión del documento")
                != QDialog.DialogCode.Accepted
            ):
                if previous_requested:
                    return self._review_quality_phases(entry, job)
                try:
                    self._phase_reviews.prepare(job.id, result, dialog.review)
                except (StateStoreError, ValueError) as exc:
                    QMessageBox.warning(self, "No se pudo guardar la revisión", str(exc))
                return None
            review = dialog.review
            try:
                reviewed_text = (
                    apply_pdf_review(reviewed_text, review, self._artifact_store)
                    if kind is ReviewKind.OCR
                    else apply_translation_review(reviewed_text, review, self._artifact_store)
                )
                progress = self._phase_reviews.apply(job.id, result, review)
            except (StateStoreError, OSError, ValueError) as exc:
                QMessageBox.warning(self, "No se pudo aplicar la revisión", str(exc))
                return None
            if progress.applied_review is None:
                QMessageBox.warning(
                    self,
                    "No se pudo aplicar la revisión",
                    "La revisión no quedó registrada como completada.",
                )
                return None
            review = progress.applied_review
            completed.append(review)
        return reviewed_text, tuple(completed)

    @staticmethod
    def _rebuild_revision_draft_after_quality(
        result: ProcessResult,
        reviewed_text: str,
    ) -> RevisionDraft | None:
        draft = result.revision_draft
        if draft is None:
            return None
        return build_revision_draft(
            reviewed_text,
            draft.proposed_markdown,
            kinds=draft.kinds,
        )

    def _review_revision_by_phase(
        self,
        entry: JobRuntime,
        job: DocumentJob,
        *,
        draft: RevisionDraft,
        preceding_reviews: tuple[ReviewSession, ...] = (),
    ) -> None:
        result = entry.result
        if result is None:
            return
        try:
            preparation = self._review_materialization.materialize_revisions(
                job,
                result,
                draft,
                preceding_reviews=preceding_reviews,
            )
        except (ParsezenError, StateStoreError, OSError, ValueError) as exc:
            QMessageBox.warning(self, "No se pudo preparar la revisión", str(exc))
            return
        reviews: list[ReviewSession] = list(preceding_reviews)
        materialized = preparation.materialized
        phase_plan = preparation.phase_plan

        try:
            self._phase_reviews.reconcile(job.id, result)
        except (KeyError, StateStoreError, ValueError) as exc:
            QMessageBox.warning(self, "No se pudo recuperar la revisión", str(exc))
            return

        for revision_kind, _review_kind, materialized_review in materialized:
            if materialized_review is None:
                continue
            review = materialized_review
            if review.status is ReviewStatus.APPLIED:
                reviews.append(review)
                continue
            try:
                self._phase_reviews.prepare(job.id, result, review)
            except (StateStoreError, ValueError) as exc:
                QMessageBox.warning(self, "No se pudo preparar la revisión", str(exc))
                return
            if revision_kind is RevisionKind.STRUCTURE:
                # Structural proposals become the starting point of the real
                # EPUB editor, where the user sees and adjusts the whole book.
                for unit in review.units:
                    if unit.choice is None:
                        review = review.decide(unit.id, ReviewChoice.PROPOSED)
            else:
                previous_kind = _previous_review_kind(phase_plan, review.kind)
                previous_requested = False

                def reopen_previous(previous_kind: ReviewKind | None = previous_kind) -> bool:
                    nonlocal previous_requested
                    if previous_kind is None:
                        return False
                    try:
                        self._phase_reviews.reopen_review(
                            job.id,
                            result,
                            kind=previous_kind,
                        )
                    except (KeyError, StateStoreError, ValueError) as exc:
                        QMessageBox.warning(
                            self,
                            "No se pudo volver a la fase anterior",
                            str(exc),
                        )
                        return False
                    previous_requested = True
                    return True

                dialog = PhaseReviewDialog(
                    review,
                    self._artifact_store,
                    phase_plan=phase_plan,
                    translation_follows_ocr=job.configuration.translation.enabled,
                    structure_outline=(
                        (draft.original_markdown, draft.proposed_markdown)
                        if review.kind is ReviewKind.STRUCTURE
                        else None
                    ),
                    previous_phase_callback=(
                        reopen_previous if previous_kind is not None else None
                    ),
                    parent=self,
                )
                if (
                    self._exec_internal_dialog(dialog, "Revisión de correcciones")
                    != QDialog.DialogCode.Accepted
                ):
                    if previous_requested:
                        self._review_job(job.id, None)
                        return
                    try:
                        self._phase_reviews.prepare(job.id, result, dialog.review)
                    except (StateStoreError, ValueError) as exc:
                        QMessageBox.warning(self, "No se pudo guardar la revisión", str(exc))
                    return
                review = dialog.review
            try:
                progress = self._phase_reviews.apply(job.id, result, review)
            except (StateStoreError, ValueError) as exc:
                QMessageBox.warning(self, "No se pudo aplicar la revisión", str(exc))
                return
            if progress.applied_review is None:
                QMessageBox.warning(
                    self,
                    "No se pudo aplicar la revisión",
                    "La revisión no quedó registrada como completada.",
                )
                return
            reviews.append(progress.applied_review)
        try:
            reviewed_text = render_revision_reviews(
                draft,
                tuple(reviews),
                self._artifact_store,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Revisión incompleta", str(exc))
            return

        self._sync_workspace(force_persist=True)
        if (
            result.final_path.suffix.casefold() == ".epub"
            and result.revision_epub_metadata is not None
        ):
            try:
                book = self._review_publication.prepare_book(
                    job.id,
                    result,
                    reviewed_text,
                )
            except (ParsezenError, StateStoreError, ValueError, OSError) as exc:
                self._keep_review_pending(entry, result)
                QMessageBox.warning(self, "No se pudo preparar el editor", str(exc))
                return
            try:
                publishable_book, saved_draft = self._confirm_epub_book(
                    book,
                    job_id=job.id,
                    destination=result.final_path,
                )
            except (ParsezenError, ValueError, OSError) as exc:
                self._keep_review_pending(entry, result)
                QMessageBox.warning(self, "No se pudo abrir el editor", str(exc))
                return
            if publishable_book is None:
                if saved_draft is not None:
                    try:
                        self._review_publication.save_book(job.id, saved_draft)
                    except (StateStoreError, ValueError, OSError) as exc:
                        QMessageBox.warning(
                            self,
                            "No se pudo guardar el borrador",
                            str(exc),
                        )
                self._keep_review_pending(entry, result)
                return
            try:
                published = self._review_publication.publish_book(
                    job.id,
                    result,
                    reviewed_text,
                    publishable_book,
                    tuple(reviews),
                )
            except (ParsezenError, StateStoreError, OSError, ValueError) as exc:
                self._publication_failed(entry, result, exc)
                return
            self._accept_published_review(entry, published)
        else:
            self._apply_reviewed_text(
                entry,
                result,
                reviewed_text,
                tuple(reviews),
            )
            return

    def _apply_reviewed_text(
        self,
        entry: JobRuntime,
        result: ProcessResult,
        reviewed_text: str,
        reviews: tuple[ReviewSession, ...],
    ) -> None:
        self._sync_workspace(force_persist=True)
        job = next(
            (
                candidate
                for candidate in self._job_queue.jobs
                if self._runtime_by_job.get(candidate.id) is entry
            ),
            None,
        )
        if job is None:
            return
        try:
            published = self._review_publication.publish_text(
                job.id,
                result,
                reviewed_text,
                reviews,
            )
        except (ParsezenError, StateStoreError, OSError, ValueError) as exc:
            self._publication_failed(entry, result, exc)
            return
        self._accept_published_review(entry, published)

    def _publication_failed(
        self,
        entry: JobRuntime,
        result: ProcessResult,
        error: Exception,
    ) -> None:
        message = str(error).strip() or "No se pudo finalizar la revisión."
        self._keep_review_pending(entry, result)
        job = self._job_for_runtime(entry)
        if job is not None:
            self._job_queue.replace(replace(job, warnings=(*job.warnings, message)))
        QMessageBox.warning(
            self,
            "No se pudo finalizar la revisión",
            "El borrador y tus decisiones siguen guardados. Inténtalo de nuevo.",
        )

    def _accept_published_review(
        self,
        entry: JobRuntime,
        published: PublishedReview,
    ) -> None:
        updated = published.result
        finalized = published.finalization
        entry.result = updated
        current_job = self._job_queue.get(finalized.job.id)
        if current_job is not None:
            recommendation = current_job.review_recommendation
            if recommendation is not None:
                rebound = (
                    rebind_review_recommendation(updated.review_markdown, recommendation)
                    if updated.review_markdown is not None
                    else None
                )
                if rebound != recommendation:
                    current_job = self._job_queue.replace(
                        replace(current_job, review_recommendation=rebound)
                    )
            summary = self._record_completed_outcome(
                current_job,
                entry,
                updated,
                reviews=finalized.reviews,
                activity=self._activity_for_job(current_job.id),
            )
            self.parsezen_workspace.show_batch_summary(
                (
                    "Resultado final listo: la revisión se aplicó y la integridad técnica "
                    "quedó comprobada."
                    if summary.integrity_verified
                    else "Resultado final listo: la revisión se aplicó. "
                    "No consta un informe detallado del control técnico."
                ),
                tone="success",
            )
            self._show_system_notification(
                "Resultado listo",
                f"{current_job.source.path.name} ya está disponible.",
                QSystemTrayIcon.MessageIcon.Information,
            )
        if finalized.warning is ReviewFinalizationWarning.STALE_REVIEW_MATERIAL_NOT_REMOVED:
            LOGGER.warning("stale_review_material_cleanup_failed")
        try:
            request, runtime_settings = request_and_settings_from_job(
                finalized.job,
                timeout_seconds=self._settings.timeout_seconds,
                checkpoint_retention_days=self._settings.checkpoint_retention_days,
            )
            if runtime_settings.checkpoint_retention_days == 0:
                clear_document_work_checkpoints(
                    request,
                    runtime_settings,
                    root=self._work_checkpoint_root,
                )
        except (OSError, ValueError):
            LOGGER.warning("review_checkpoint_cleanup_failed")

    def _record_completed_outcome(
        self,
        job: DocumentJob,
        entry: JobRuntime,
        result: ProcessResult,
        *,
        reviews: tuple[ReviewSession, ...] = (),
        activity: _AttemptActivity | None = None,
    ) -> OutcomeSummary:
        summary = self._build_outcome_summary(job, entry, reviews=reviews)
        self._outcome_summaries[job.id] = summary
        self._record_recent_job(
            job,
            RecentJobStatus.COMPLETED,
            result_path=result.final_path,
            summary=summary,
            activity=activity,
        )
        return summary

    def _build_outcome_summary(
        self,
        job: DocumentJob,
        entry: JobRuntime,
        *,
        reviews: tuple[ReviewSession, ...] = (),
    ) -> OutcomeSummary:
        if entry.result is None:
            raise ValueError("A completed outcome requires its processing result.")
        duration_seconds = (
            max(1, round(entry.finished_at - entry.started_at))
            if entry.started_at is not None and entry.finished_at is not None
            else None
        )
        forecast = self._latest_forecasts.get(job.id)
        return build_outcome_summary(
            job,
            entry.result,
            reviews=reviews,
            duration_seconds=duration_seconds,
            estimate=forecast.estimate if forecast is not None else None,
            early_check=self._early_check_reports.get(job.id),
        )

    def _record_recent_job(
        self,
        job: DocumentJob,
        status: RecentJobStatus,
        *,
        result_path: Path | None = None,
        summary: OutcomeSummary | None = None,
        activity: _AttemptActivity | None = None,
    ) -> None:
        terminal_activity = activity if activity is not None else self._activity_for_job(job.id)
        try:
            append_recent_jobs(
                (
                    RecentJob(
                        source_path=job.source.path,
                        status=status,
                        finished_at=datetime.now(UTC),
                        result_path=result_path,
                        summary=summary,
                        attempt_id=(
                            terminal_activity[0] if terminal_activity is not None else None
                        ),
                        timeline=(
                            terminal_activity[1]
                            if terminal_activity is not None
                            else AttemptTimeline()
                        ),
                        failure=(
                            terminal_activity[2]
                            if status is RecentJobStatus.FAILED and terminal_activity is not None
                            else None
                        ),
                    ),
                ),
                path=self._history_path,
            )
        except OSError:
            LOGGER.warning("recent_activity_write_failed")

    def _capture_terminal_attempt_activity(self, job_id: str | None) -> _AttemptActivity:
        activity = (
            self._processing_runner.attempt_id,
            self._processing_runner.timeline,
            self._processing_runner.failure_snapshot,
        )
        if job_id is not None:
            self._terminal_attempt_activity[job_id] = activity
        return activity

    def _activity_for_job(self, job_id: str) -> _AttemptActivity | None:
        return self._terminal_attempt_activity.get(job_id)

    def _prune_terminal_attempt_activity(self, jobs: tuple[DocumentJob, ...]) -> None:
        current_ids = {job.id for job in jobs}
        for job_id in tuple(self._terminal_attempt_activity):
            if job_id not in current_ids:
                del self._terminal_attempt_activity[job_id]

    def _show_finished_batch_summary(self, job_ids: tuple[str, ...]) -> None:
        jobs = tuple(job for job_id in job_ids if (job := self._job_queue.get(job_id)) is not None)
        if not jobs:
            return
        completed = sum(job.status is JobStatus.COMPLETED for job in jobs)
        reviews = sum(job.status is JobStatus.WAITING_REVIEW for job in jobs)
        failed = sum(job.status is JobStatus.FAILED for job in jobs)
        paused = sum(job.status is JobStatus.PAUSED for job in jobs)
        cancelled = sum(job.status is JobStatus.CANCELLED for job in jobs)
        parts = []
        if completed:
            parts.append(f"{completed} {'listo' if completed == 1 else 'listos'}")
        if reviews:
            parts.append(f"{reviews} {'requiere' if reviews == 1 else 'requieren'} revisión")
        if failed:
            parts.append(f"{failed} con error")
        if paused:
            parts.append(f"{paused} {'pausado' if paused == 1 else 'pausados'}")
        if cancelled:
            parts.append(f"{cancelled} {'cancelado' if cancelled == 1 else 'cancelados'}")
        if not parts:
            return
        message = "Procesamiento terminado: " + " · ".join(parts) + "."
        tone = "error" if failed else "warning" if reviews or paused or cancelled else "success"
        self.parsezen_workspace.show_batch_summary(
            message,
            tone=tone,
            activity_available=bool(completed or failed or cancelled),
        )
        icon = (
            QSystemTrayIcon.MessageIcon.Critical
            if failed
            else QSystemTrayIcon.MessageIcon.Warning
            if reviews or paused or cancelled
            else QSystemTrayIcon.MessageIcon.Information
        )
        self._show_system_notification("Parsezen", message, icon)

    def _show_system_notification(
        self,
        title: str,
        message: str,
        icon: QSystemTrayIcon.MessageIcon,
    ) -> None:
        tray = self._notification_tray
        if tray is None or (self.isActiveWindow() and not self.isMinimized()):
            return
        tray.show()
        tray.showMessage(title, message, icon, 10_000)
        QTimer.singleShot(15_000, tray.hide)

    def _keep_review_pending(
        self,
        entry: JobRuntime,
        result: ProcessResult,
    ) -> None:
        """Restore a visible review gate when publication cannot finish."""

        job = self._job_for_runtime(entry)
        if job is None:
            return
        try:
            self._phase_reviews.reopen_last_review(job.id, result)
        except (KeyError, StateStoreError, ValueError):
            LOGGER.warning("review_gate_restore_failed")
        self._sync_workspace(force_persist=True)

    @Slot(str)
    def _open_job_result(self, job_id: str) -> None:
        entry = self._entry_for_job_id(job_id)
        job = self._job_queue.get(job_id)
        if (
            entry is None
            or job is None
            or job.status is not JobStatus.COMPLETED
            or entry.result is None
        ):
            return
        self._open_local_path(entry.result.final_path)

    @Slot(str)
    def _open_job_result_folder(self, job_id: str) -> None:
        entry = self._entry_for_job_id(job_id)
        job = self._job_queue.get(job_id)
        if (
            entry is None
            or job is None
            or job.status is not JobStatus.COMPLETED
            or entry.result is None
        ):
            return
        self._open_local_path(entry.result.final_path.parent)

    def _open_local_path(self, path: Path) -> None:
        if not path.exists() or not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            QMessageBox.warning(
                self,
                "No se pudo abrir",
                "El resultado ya no está disponible en la ubicación guardada.",
            )

    def _select_result_card(self, key: str) -> None:
        job = next(
            (candidate for candidate in self._job_queue.jobs if str(candidate.source.path) == key),
            None,
        )
        if job is None:
            return
        entry = self._runtime_by_job.get(job.id)
        if entry is None:
            return
        self._result = entry.result
        self._selected_result_job_id = job.id

    def _open_result_folder(self) -> None:
        result = self._result
        if result is not None:
            self._open_local_path(result.final_path.parent)

    def _review_result_card(self, key: str) -> None:
        job = next(
            (candidate for candidate in self._job_queue.jobs if str(candidate.source.path) == key),
            None,
        )
        entry = self._runtime_by_job.get(job.id) if job is not None else None
        if entry is not None and entry.result is not None:
            self._open_local_path(entry.result.final_path)

    @Slot(str)
    def _remove_job(self, job_id: str) -> None:
        entry = self._entry_for_job_id(job_id)
        job = self._job_for_id(job_id)
        if entry is None or job is None:
            return
        if job.status in {JobStatus.PAUSED, JobStatus.WAITING_REVIEW}:
            pending_review = job.status is JobStatus.WAITING_REVIEW
            answer = QMessageBox.question(
                self,
                (
                    "Quitar documento pendiente de revisión"
                    if pending_review
                    else "Quitar documento pausado"
                ),
                "Se eliminará de la cola y se descartará "
                + ("la revisión pendiente. " if pending_review else "su progreso recuperable. ")
                + "El documento original no se modificará.\n\n¿Quieres continuar?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            if not pending_review:
                try:
                    request, runtime_settings = request_and_settings_from_job(
                        job,
                        timeout_seconds=self._settings.timeout_seconds,
                        checkpoint_retention_days=self._settings.checkpoint_retention_days,
                    )
                    clear_document_work_checkpoints(
                        request,
                        runtime_settings,
                        root=self._work_checkpoint_root,
                    )
                except (OSError, ValueError):
                    LOGGER.warning("paused_job_checkpoint_cleanup_failed")
        self._job_queue.remove(job_id)
        self._runtime_by_job.pop(job_id, None)
        if self._selected_result_job_id == job_id:
            self._selected_result_job_id = None
            self._result = None
        self._sync_workspace(force_persist=True)
        try:
            self._artifact_store.remove_job(job_id)
        except OSError:
            pass

    @Slot(str, int)
    def _move_job(self, job_id: str, target_row: int) -> None:
        job = self._job_queue.get(job_id)
        if job is None or job.status is JobStatus.RUNNING:
            return
        self._job_queue.move(job_id, target_row)
        self._sync_workspace(force_persist=True)

    @Slot()
    def _pause_processing(self) -> None:
        if self._preflight_runner.preparing:
            self._start_after_preparation = False
            self._preflight_runner.cancel_preparation()
            return
        if not self._is_processing:
            return
        self._pause_requested = True
        if not self._processing_runner.cancel():
            self._pause_requested = False

    @Slot(str)
    def _run_primary_action(self, mode: str) -> None:
        if mode == "pause":
            self._pause_processing()
            return
        if mode == "review":
            job = next(
                (
                    candidate
                    for candidate in self._job_queue.jobs
                    if candidate.status is JobStatus.WAITING_REVIEW
                ),
                None,
            )
            if job is not None:
                self._review_job(job.id, None)
            return
        if mode == "open_folder":
            job = next(
                (
                    candidate
                    for candidate in self._job_queue.jobs
                    if (runtime := self._runtime_by_job.get(candidate.id)) is not None
                    and runtime.result is not None
                ),
                None,
            )
            if job is not None:
                self._select_result_card(str(job.source.path))
                self._open_result_folder()
            return
        if mode == "configure_result":
            job = next(
                (candidate for candidate in self._job_queue.jobs if not candidate.is_configured),
                None,
            )
            if job is not None:
                self._configure_job(job.id, StageKind.PUBLISH)
            return
        self._start_after_preparation = True
        try:
            self._prepare_independent_requests()
        except (OSError, ValueError) as exc:
            self._start_after_preparation = False
            QMessageBox.warning(
                self,
                "No se pudo preparar el procesamiento",
                str(exc),
            )
            self._prepared_run = None
            return
        if self._prepared_run is not None:
            self._start_after_preparation = False
            self._start_prepared_run()

    def _start_prepared_run(self) -> None:
        prepared = self._prepared_run
        if prepared is None or not prepared.plan.job_ids:
            self.parsezen_workspace.set_preparing_jobs(())
            QMessageBox.information(
                self,
                "Nada pendiente",
                "No hay documentos preparados que necesiten procesamiento.",
            )
            return
        if prepared.issues:
            self.parsezen_workspace.set_preparing_jobs(())
            first_issue = prepared.issues[0]
            QMessageBox.warning(
                self,
                "Revisa la configuración",
                f"{first_issue.document_name}: {first_issue.message}",
            )
            self._prepared_run = None
            return
        if (
            prepared.preflight is not None
            and prepared.preflight.requires_confirmation
            and PreflightDialog(
                prepared.preflight,
                {
                    job.id: job.source.path.name
                    for job in self._job_queue.jobs
                    if job.id in prepared.plan.job_ids
                },
                self,
            ).exec()
            != QDialog.DialogCode.Accepted
        ):
            self.parsezen_workspace.set_preparing_jobs(())
            self._prepared_run = None
            self._sync_workspace()
            return
        self._start_processing()
        failed_to_start = not self._batch_running and any(
            (job := self._job_queue.get(job_id)) is not None
            and job.status
            in {
                JobStatus.QUEUED,
                JobStatus.PAUSED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            }
            for job_id in prepared.plan.job_ids
        )
        if failed_to_start:
            QMessageBox.warning(
                self,
                "No se pudo iniciar",
                "Parsezen no pudo iniciar el trabajo. Revisa su configuración "
                "e inténtalo de nuevo.",
            )
        self._sync_workspace(force_persist=True)

    @Slot(str)
    def _retry_failed_job(self, job_id: str) -> None:
        if (
            self._is_processing
            or self._batch_running
            or self._processing_runner.is_active
            or self._is_discovering_models
            or self._is_recommending_models
            or self._is_ai_setup_active
            or self._preflight_runner.preparing
        ):
            return
        job = self._job_queue.get(job_id)
        if (
            job is None
            or not job.is_configured
            or job.status not in {JobStatus.FAILED, JobStatus.CANCELLED}
        ):
            return
        plan = QueueRunPlan(RunMode.RETRY, (job_id,))
        self._start_after_preparation = True
        self._begin_queue_preparation(plan, error_title="No se pudo preparar el reintento")

    def _begin_queue_preparation(self, plan: QueueRunPlan, *, error_title: str) -> None:
        self._prepared_run = None
        self._preparation_error_title = error_title
        self.parsezen_workspace.set_preparing_jobs(plan.job_ids, can_pause=True)
        try:
            for job_id in plan.job_ids:
                job = self._job_queue.get(job_id)
                if job is not None:
                    self._job_queue.refresh_source(job.id, _source_from_path(job.source.path))
        except (OSError, ValueError) as exc:
            self._preparation_failed(str(exc))
            return
        if not self._preflight_runner.prepare(
            self._job_queue.jobs,
            timeout_seconds=self._settings.timeout_seconds,
            checkpoint_retention_days=self._settings.checkpoint_retention_days,
            metrics=self._processing_metrics,
            plan=plan,
        ):
            self.parsezen_workspace.set_preparing_jobs(())

    @Slot(object)
    def _preparation_succeeded(self, value: object) -> None:
        if not isinstance(value, PreparedQueueRun):
            self._preparation_failed(
                "La preparaci\u00f3n devolvi\u00f3 un resultado no v\u00e1lido."
            )
            return
        self._prepared_run = value
        for item in value.items:
            if item.preflight is not None:
                self._latest_forecasts[item.job_id] = item.preflight
                job = self._job_queue.get(item.job_id)
                if job is not None:
                    key = forecast_cache_key(
                        job,
                        item.settings.model,
                        self._metrics_generation,
                    )
                    self._forecast_cache[key] = item.preflight
            entry = self._entry_for_job_id(item.job_id)
            if entry is not None:
                entry.pdf_page_range = item.request.pdf_page_range
        if self._start_after_preparation:
            self._start_after_preparation = False
            self._start_prepared_run()

    @Slot(str)
    def _preparation_failed(self, message: str) -> None:
        self.parsezen_workspace.set_preparing_jobs(())
        self._start_after_preparation = False
        self._prepared_run = None
        QMessageBox.warning(self, self._preparation_error_title, message)
        self._sync_workspace()

    @Slot()
    def _preparation_cancelled(self) -> None:
        self.parsezen_workspace.set_preparing_jobs(())
        self._prepared_run = None
        self._start_after_preparation = False
        self._sync_workspace()

    def _prepare_independent_requests(self) -> None:
        if self._preflight_runner.preparing:
            return
        self._ensure_configurations()
        self._begin_queue_preparation(
            self._job_execution.plan_run(),
            error_title="No se pudo preparar el procesamiento",
        )

    def _refresh_preflight_forecasts(self, jobs: tuple[DocumentJob, ...]) -> None:
        planned_ids = frozenset(self._job_execution.plan_run().job_ids)
        forecasts: dict[str, DocumentPreflight] = {}
        pending = False
        known_keys = frozenset((*self._forecast_cache, *self._forecast_unavailable))
        for job in jobs:
            if job.id not in planned_ids or not job.is_configured:
                continue
            try:
                _request, settings = request_and_settings_from_job(
                    job,
                    timeout_seconds=self._settings.timeout_seconds,
                    checkpoint_retention_days=self._settings.checkpoint_retention_days,
                )
                key = forecast_cache_key(job, settings.model, self._metrics_generation)
                forecast = self._forecast_cache.get(key)
                if forecast is not None:
                    forecasts[job.id] = forecast
                elif (
                    job.source.format is not DocumentFormat.PDF
                    and key not in self._forecast_unavailable
                ):
                    pending = True
            except (OSError, ParsezenError, ValueError):
                fallback_key = forecast_cache_key(job, None, self._metrics_generation)
                if fallback_key not in self._forecast_unavailable:
                    pending = True
        queue_preflight = combine_preflights(tuple(forecasts.values())) if forecasts else None
        active_ids = {job.id for job in jobs}
        self._latest_forecasts = {
            job_id: forecast
            for job_id, forecast in self._latest_forecasts.items()
            if job_id in active_ids
        }
        self._latest_forecasts.update(forecasts)
        self.parsezen_workspace.set_preflight(forecasts, queue_preflight)
        if pending and not self._preflight_runner.forecasting:
            self._preflight_runner.forecast(
                jobs,
                planned_ids,
                timeout_seconds=self._settings.timeout_seconds,
                checkpoint_retention_days=self._settings.checkpoint_retention_days,
                metrics=self._processing_metrics,
                metrics_generation=self._metrics_generation,
                excluded_keys=known_keys,
            )

    @Slot(object)
    def _forecasts_succeeded(self, value: object) -> None:
        if not isinstance(value, ForecastBatch):
            return
        current_jobs = self._job_queue.jobs
        if value.metrics_generation != self._metrics_generation or value.jobs != current_jobs:
            return
        for result in value.results:
            if result.forecast is None:
                self._forecast_unavailable.add(result.key)
            else:
                self._forecast_cache[result.key] = result.forecast
        self._apply_cached_forecasts(current_jobs)

    @Slot()
    def _forecasts_finished(self) -> None:
        jobs = self._job_queue.jobs
        if jobs != self._last_projection:
            return
        self._refresh_preflight_forecasts(jobs)

    def _apply_cached_forecasts(self, jobs: tuple[DocumentJob, ...]) -> None:
        planned_ids = frozenset(self._job_execution.plan_run().job_ids)
        forecasts: dict[str, DocumentPreflight] = {}
        for job in jobs:
            if job.id not in planned_ids or not job.is_configured:
                continue
            try:
                _request, settings = request_and_settings_from_job(
                    job,
                    timeout_seconds=self._settings.timeout_seconds,
                    checkpoint_retention_days=self._settings.checkpoint_retention_days,
                )
            except (OSError, ParsezenError, ValueError):
                continue
            key = forecast_cache_key(job, settings.model, self._metrics_generation)
            forecast = self._forecast_cache.get(key)
            if forecast is not None:
                forecasts[job.id] = forecast
        active_ids = {job.id for job in jobs}
        self._latest_forecasts = {
            job_id: forecast
            for job_id, forecast in self._latest_forecasts.items()
            if job_id in active_ids
        }
        self._latest_forecasts.update(forecasts)
        queue_preflight = combine_preflights(tuple(forecasts.values())) if forecasts else None
        self.parsezen_workspace.set_preflight(forecasts, queue_preflight)

    def _runtime_estimates(self) -> dict[str, RuntimeEstimate]:
        now = monotonic()
        estimates = {}
        for job in self._job_queue.jobs:
            entry = self._runtime_by_job.get(job.id)
            if job.status is not JobStatus.RUNNING or entry is None or entry.started_at is None:
                continue
            forecast = self._latest_forecasts.get(job.id)
            if forecast is None:
                continue
            estimates[job.id] = estimate_remaining_time(
                forecast.estimate,
                elapsed_seconds=now - entry.started_at,
                stage_elapsed_seconds=(
                    now - entry.stage_started_at if entry.stage_started_at is not None else None
                ),
                progress_current=entry.progress_current,
                progress_total=entry.progress_total,
            )
        return estimates

    def _remember_processing_duration(
        self,
        job: DocumentJob | None,
        result: ProcessResult,
    ) -> None:
        if job is None or result.telemetry is None:
            return
        item = self._prepared_run.item(job.id) if self._prepared_run is not None else None
        profile = item.workload_profile if item is not None else None
        if profile is None:
            if self._current_runtime() is None:
                return
            try:
                runtime = self._runtime_for_entry(job.id)
                if runtime is None:
                    return
                request, settings = runtime
                _analysis, profile = analyze_preflight(
                    job,
                    request,
                    settings,
                    self._processing_metrics,
                )
            except (OSError, ParsezenError, ValueError):
                return
        metric = processing_metric(
            profile,
            result.telemetry,
            created_at=datetime.now(UTC),
        )
        if metric is None:
            return
        try:
            self._state_store.save_processing_metric(metric)
        except StateStoreError:
            LOGGER.warning("processing_estimate_history_write_failed")
            return
        self._processing_metrics = (metric, *self._processing_metrics)[:200]
        self._metrics_generation += 1
        self._forecast_cache.clear()
        self._forecast_unavailable.clear()

    def _ensure_configurations(self) -> None:
        for job in self._job_queue.jobs:
            self._runtime_by_job.setdefault(job.id, JobRuntime())

    def _project_jobs(self) -> tuple[DocumentJob, ...]:
        self._ensure_configurations()
        return self._job_queue.jobs

    def _sync_workspace(self, *, force_persist: bool = False) -> bool:
        jobs = self._project_jobs()
        self._prune_terminal_attempt_activity(jobs)
        self.parsezen_workspace.set_output_directory(self._settings.output_directory)
        self.parsezen_workspace.set_local_ai_status(
            self._ollama_status,
            self._settings.model,
        )
        integrity_reports = {
            job_id: entry.result.final_integrity_report
            for job_id, entry in self._runtime_by_job.items()
            if entry.result is not None
            and entry.result.final_integrity_report is not None
            and self._job_queue.get(job_id) is not None
        }
        self.parsezen_workspace.set_integrity_reports(integrity_reports)
        self.parsezen_workspace.set_runtime_estimates(self._runtime_estimates())
        if jobs != self._last_projection:
            self.parsezen_workspace.set_jobs(jobs)
            self._refresh_preflight_forecasts(jobs)
            self._last_projection = jobs
        persistence = self._queue_persistence.persist(jobs, force=force_persist)
        if persistence.status is QueuePersistenceStatus.UNAVAILABLE:
            self.parsezen_workspace.set_recovery_warning(_RECOVERY_READ_WARNING)
            return False
        if persistence.status is QueuePersistenceStatus.FAILED:
            self.parsezen_workspace.set_recovery_warning(_RECOVERY_WRITE_WARNING)
            return False
        if persistence.status is QueuePersistenceStatus.SAVED:
            self.parsezen_workspace.set_recovery_warning(self._state_recovery_notice)
        return True

    def _restore_workspace(self) -> frozenset[str] | None:
        try:
            saved_jobs = self._state_store.load_jobs()
        except StateStoreError:
            if self._backup_and_reset_unreadable_state():
                return None
            self._queue_persistence.mark_unavailable()
            return None
        recovered = recover_workspace(saved_jobs, self._result_snapshots, self._settings)
        if recovered.jobs:
            self._job_queue.restore(recovered.jobs)
            for job_id in recovered.reset_paused_job_ids:
                paused = self._job_execution.reset_paused(job_id)
                if job_id in recovered.source_changed_job_ids:
                    self._job_queue.replace(
                        replace(
                            paused,
                            warnings=(*paused.warnings, _SOURCE_CHANGED_MESSAGE),
                        )
                    )
            for job_id in recovered.interrupted_job_ids:
                self._job_execution.recover_interrupted(job_id)
            self._runtime_by_job = dict(recovered.runtime)
        return recovered.retained_artifact_job_ids

    def _backup_and_reset_unreadable_state(self) -> bool:
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        source = self._state_store.path
        backup = source.with_name(f"{source.stem}.unreadable-{timestamp}{source.suffix}")
        artifact_source = self._artifact_store.root
        artifact_backup = artifact_source.with_name(
            f"{artifact_source.name}.unreadable-{timestamp}"
        )
        try:
            self._state_store.backup_to(backup)
            if artifact_source.exists():
                if artifact_backup.exists():
                    raise FileExistsError("The artifact backup already exists.")
                artifact_source.replace(artifact_backup)
            self._state_store.reset_queue()
        except (FileExistsError, OSError, StateStoreError, ValueError):
            LOGGER.warning("unreadable_state_preservation_failed")
            return False
        preserved = backup.name
        if artifact_backup.exists():
            preserved = f"{backup.name} y {artifact_backup.name}"
        self._state_recovery_notice = (
            "No se pudo recuperar la cola anterior. Se conservó una copia local "
            f"segura ({preserved})."
        )
        LOGGER.warning(
            "unreadable_state_preserved backup_created=true artifacts_preserved=%s",
            artifact_backup.exists(),
        )
        return True

    def _invalidate_changed_source_review(
        self,
        entry: JobRuntime,
        job: DocumentJob,
    ) -> None:
        entry.result = None
        paused = self._job_execution.reset_paused(job.id)
        if _SOURCE_CHANGED_MESSAGE not in paused.warnings:
            self._job_queue.replace(
                replace(paused, warnings=(*paused.warnings, _SOURCE_CHANGED_MESSAGE))
            )
        try:
            self._state_store.delete_review_material(job.id)
            self._artifact_store.remove_job(job.id)
        except (StateStoreError, OSError, ValueError):
            LOGGER.warning("changed_source_review_cleanup_failed")

    def _has_unfinished_jobs(self) -> bool:
        return any(
            job.status not in {JobStatus.COMPLETED, JobStatus.CANCELLED}
            for job in self._job_queue.jobs
        )

    def _prune_orphaned_review_artifacts(self, retained_job_ids: frozenset[str]) -> None:
        try:
            removed = self._artifact_store.prune_orphaned_jobs(retained_job_ids)
        except (OSError, ValueError):
            LOGGER.warning("orphaned_review_artifact_cleanup_failed")
            return
        if removed:
            LOGGER.info("orphaned_review_artifacts_removed count=%d", len(removed))

    def _entry_for_job_id(self, job_id: str) -> JobRuntime | None:
        return self._runtime_by_job.get(job_id) if self._job_queue.get(job_id) is not None else None

    def _job_for_runtime(self, runtime: JobRuntime) -> DocumentJob | None:
        return next(
            (job for job in self._job_queue.jobs if self._runtime_by_job.get(job.id) is runtime),
            None,
        )

    def _job_for_id(self, job_id: str) -> DocumentJob | None:
        return self._job_queue.get(job_id)


def _source_from_path(path: Path) -> DocumentSource:
    try:
        return DocumentSource.inspect(path)
    except OSError:
        return DocumentSource(path, DocumentFormat.from_path(path), 0, 0)


def _quarantine_structurally_unreadable_state(state_path: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    recovery = state_path.parent / f"recovery-unreadable-{timestamp}"
    recovery.mkdir(parents=True)
    candidates = (
        state_path,
        state_path.with_name(f"{state_path.name}-wal"),
        state_path.with_name(f"{state_path.name}-shm"),
    )
    try:
        for candidate in candidates:
            if candidate.exists():
                candidate.replace(recovery / candidate.name)
        artifact_root = state_path.parent / "artifacts"
        if artifact_root.exists():
            artifact_root.replace(recovery / artifact_root.name)
    except OSError as exc:
        raise StateStoreError(
            "The unreadable Parsezen state could not be preserved safely."
        ) from exc
    return recovery


def _default_configuration(path: Path, settings: AppSettings) -> JobConfiguration:
    source_format = DocumentFormat.from_path(path)
    output_format = (
        DocumentFormat.EPUB if source_format is DocumentFormat.EPUB else DocumentFormat.MARKDOWN
    )
    image_capable = output_format in {
        DocumentFormat.MARKDOWN,
        DocumentFormat.DOCX,
        DocumentFormat.EPUB,
    }
    return JobConfiguration(
        output=OutputConfiguration(
            configured=False,
            format=output_format,
            directory=settings.output_directory,
            include_images=image_capable,
            image_directory=settings.image_output_directory,
            preserve_styles=output_format in {DocumentFormat.DOCX, DocumentFormat.EPUB},
            title=path.stem if output_format is DocumentFormat.EPUB else None,
        ),
        ai=AIProfileConfiguration(
            model=settings.model,
            context_window=settings.context_window,
        ),
        translation=TranslationConfiguration(),
        plan=ProcessingPlan.STANDARD,
    )


def _coerce_stage_kind(value: object) -> StageKind | None:
    """Normalize Qt signal payloads without falling back to the global form."""

    if isinstance(value, StageKind):
        return value
    if isinstance(value, str):
        try:
            return StageKind(value)
        except ValueError:
            return None
    return None


def _path_key(path: Path) -> str:
    return str(path.resolve(strict=False)).casefold()


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = _path_key(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique
