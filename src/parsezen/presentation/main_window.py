"""Production Parsezen window backed by explicit application services."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Protocol
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
from parsezen.application.quality_review_adapter import (
    apply_pdf_review,
    apply_translation_review,
    create_pdf_review,
    create_translation_review,
)
from parsezen.application.recovery import ProcessingFailure, recovery_plan
from parsezen.application.review_finalization import (
    ReviewFinalizationCoordinator,
    ReviewFinalizationWarning,
)
from parsezen.application.review_plan import review_workload_for_result
from parsezen.application.review_publication import (
    PublishedReview,
    ReviewPublicationCoordinator,
)
from parsezen.application.revision_materializer import (
    create_revision_review,
    render_revision_reviews,
)
from parsezen.application.run_preparation import PreparedQueueRun, prepare_queue_run
from parsezen.application.runtime_mapping import (
    request_and_settings_from_job,
    stage_kind_from_process_stage,
)
from parsezen.application.scheduler import QueueRunPlan, RunMode
from parsezen.batch import BatchEntry, BatchQueue, BatchStatus, BatchValidationIssue
from parsezen.branding import APP_ICON_PATH
from parsezen.diagnostics import build_diagnostic_report
from parsezen.diagnostics_dialog import DiagnosticsDialog
from parsezen.domain.estimates import ProcessingMetric
from parsezen.domain.jobs import (
    AIProfileConfiguration,
    DocumentFormat,
    DocumentJob,
    DocumentSource,
    JobConfiguration,
    JobStatus,
    OutputConfiguration,
    RefinementConfiguration,
    StructureConfiguration,
    TranslationConfiguration,
    effective_ai_profile,
)
from parsezen.domain.outcomes import EarlyCheckReport, OutcomeSummary
from parsezen.domain.reviews import ReviewChoice, ReviewKind, ReviewSession, ReviewStatus
from parsezen.domain.stages import StageKind, StageStatus
from parsezen.errors import LocalModelUnavailableError, ParsezenError
from parsezen.infrastructure.artifact_store import ArtifactStore
from parsezen.infrastructure.result_snapshots import ResultSnapshotStore
from parsezen.infrastructure.state_store import StateStore, StateStoreError
from parsezen.job_sessions import (
    RecentJob,
    RecentJobStatus,
    append_recent_jobs,
    clear_recent_jobs,
    load_recent_jobs,
)
from parsezen.local_models import (
    OLLAMA_LIBRARY_URL,
    OllamaConnection,
    OllamaModel,
    OllamaStatus,
    choose_ollama_model,
    is_reasoning_model_id,
    validate_document_model_id,
)
from parsezen.model_manager import ModelManagerDialog
from parsezen.model_recommendations import ModelRecommendation, ModelRecommendations
from parsezen.presentation.activity_view import ActivityView
from parsezen.presentation.book_editor_dialog import BookEditorDialog
from parsezen.presentation.design_system import (
    ThemeMode,
    apply_parsezen_theme,
    preferred_theme_mode,
)
from parsezen.presentation.job_configuration_dialog import JobConfigurationDialog
from parsezen.presentation.local_ai_controller import LocalAIAction, LocalAIController
from parsezen.presentation.phase_review_dialog import PhaseReviewDialog
from parsezen.presentation.preflight_dialog import PreflightDialog
from parsezen.presentation.processing_runner import ProcessingRunner
from parsezen.presentation.workspace import ParsezenWorkspace
from parsezen.processing import (
    ProcessRequest,
    ProcessResult,
    ProcessStage,
    clear_document_work_checkpoints,
    process_document,
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
_SOURCE_CHANGED_MESSAGE = (
    "El original cambió desde que se preparó esta revisión. Para evitar mezclar "
    "versiones, el documento continuará de nuevo desde un punto seguro."
)
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
        self._source_path: Path | None = None
        self._source_paths: tuple[Path, ...] = ()
        self._batch_entries = BatchQueue()
        self._current_batch_entry: BatchEntry | None = None
        self._batch_settings: AppSettings | None = None
        self._batch_running = False
        self._result: ProcessResult | None = None
        self._result_source_path: Path | None = None
        self._pause_requested = False
        self._is_processing = False
        self._is_discovering_models = False
        self._is_recommending_models = False
        self._is_ai_setup_active = False
        self._auto_discover_ai = auto_discover_ai
        self._ollama_status: OllamaStatus | None = None
        self._ollama_models: tuple[OllamaModel, ...] = ()
        self._model_recommendations: ModelRecommendations | None = None
        self._model_manager: ModelManagerDialog | None = None
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
        self._job_queue = JobQueue()
        self._job_execution = JobExecutionController(self._job_queue)
        self._prepared_run: PreparedQueueRun | None = None
        self._last_projection: tuple[DocumentJob, ...] = ()
        self._last_persisted_projection: tuple[DocumentJob, ...] = ()
        self._last_persisted_at = 0.0
        self._state_write_retry_pending = False
        self._state_restore_failed = False
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

    @property
    def is_processing(self) -> bool:
        return self._is_processing

    def set_source_paths(self, paths: Sequence[str | Path]) -> None:
        selected = _unique_paths(Path(path) for path in paths)
        if not selected:
            return
        self._source_paths = tuple(selected)
        self._source_path = self._source_paths[0]
        self._batch_entries = BatchQueue(BatchEntry(path) for path in self._source_paths)
        self._current_batch_entry = None
        self._batch_running = False
        self._batch_settings = None
        self.parsezen_workspace.clear_batch_summary()
        self._ensure_configurations()
        self._sync_workspace(force_persist=True)

    def add_source_paths(self, paths: Sequence[str | Path]) -> None:
        candidates = _unique_paths(Path(path) for path in paths)
        known = {_path_key(path) for path in self._source_paths}
        added = [path for path in candidates if _path_key(path) not in known]
        if not added:
            return
        self.parsezen_workspace.clear_batch_summary()
        self._batch_entries.extend(BatchEntry(path) for path in added)
        self._source_paths = tuple(entry.path for entry in self._batch_entries)
        if self._source_path is None:
            self._source_path = self._source_paths[0]
        self._ensure_configurations()
        self._sync_workspace(force_persist=True)

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
        for entry in self._batch_entries:
            job = self._job_queue.for_source(entry.path)
            if job is None or job.id not in resumable:
                continue
            entry.status = BatchStatus.PENDING
            entry.result = None
            entry.error = None
            entry.stage = None
            entry.progress_current = 0
            entry.progress_total = 0
            entry.stages_seen = []
            entry.started_at = None
            entry.stage_started_at = None
            entry.finished_at = None
        self._batch_settings = self._settings
        self._pause_requested = False
        self._batch_running = True
        self._is_processing = True
        self._active_run_job_ids = prepared.plan.job_ids
        self.parsezen_workspace.clear_batch_summary()
        self._sleep_blocker.start()
        self._current_batch_entry = None
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
        entry: BatchEntry,
    ) -> tuple[ProcessRequest, AppSettings] | None:
        prepared = self._prepared_run
        job = self._job_queue.for_source(entry.path)
        if prepared is not None and job is not None:
            item = prepared.item(job.id)
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

    def _next_pending_batch_entry(self) -> BatchEntry | None:
        """Claim the next domain job before constructing its physical worker."""

        job = self._job_execution.start_next_available()
        if job is None:
            return None
        entry = self._entry_for_job_id(job.id)
        if entry is None or entry.status is not BatchStatus.PENDING:
            raise RuntimeError("The scheduled job has no matching pending worker entry.")
        return entry

    def _start_next_batch_entry(self) -> None:
        entry = self._next_pending_batch_entry()
        if entry is None:
            self._finish_batch()
            return
        job = self._job_queue.for_source(entry.path)
        if job is None:
            entry.status = BatchStatus.FAILED
            entry.error = "No se pudo localizar el documento preparado en la cola."
            self._finish_batch()
            return
        runtime = self._runtime_for_entry(entry)
        if runtime is None:
            entry.status = BatchStatus.FAILED
            entry.error = "No se pudo reconstruir la configuración del documento."
            self._finish_batch()
            return
        request, settings = runtime
        self._current_batch_entry = entry
        self._source_path = entry.path
        entry.status = BatchStatus.PROCESSING
        entry.started_at = monotonic()
        entry.stage_started_at = None
        entry.finished_at = None
        entry.stage = None
        entry.progress_current = 0
        entry.progress_total = 0
        entry.stages_seen = []
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
        self._pause_requested = False
        self._current_batch_entry = None
        self._active_run_job_ids = ()
        self._sleep_blocker.stop()
        self.parsezen_workspace.set_preparing_jobs(())
        self._show_finished_batch_summary(active_ids)
        self._sync_workspace(force_persist=True)

    @Slot(object)
    def _show_stage(self, stage: ProcessStage) -> None:
        entry = self._current_batch_entry
        job = self._job_queue.for_source(entry.path) if entry is not None else None
        if job is not None:
            self._job_execution.advance(
                job.id,
                stage_kind_from_process_stage(stage),
            )
        if entry is not None:
            previous_stage = entry.stage
            entry.stage = stage
            if previous_stage is not stage:
                entry.stage_started_at = monotonic()
            entry.progress_current = 0
            entry.progress_total = 0
            if entry.stages_seen is None:
                entry.stages_seen = []
            if stage not in entry.stages_seen:
                entry.stages_seen.append(stage)
        self.parsezen_workspace.set_preparing_jobs(())
        self._sync_workspace()

    @Slot()
    def _early_check_started(self) -> None:
        entry = self._current_batch_entry
        job = self._job_queue.for_source(entry.path) if entry is not None else None
        if job is None:
            return
        self.parsezen_workspace.set_preparing_jobs(
            (job.id,),
            {job.id: "Comprobando páginas representativas"},
            can_pause=True,
        )

    @Slot(int, int)
    def _early_check_progress(self, current: int, total: int) -> None:
        entry = self._current_batch_entry
        job = self._job_queue.for_source(entry.path) if entry is not None else None
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
        entry = self._current_batch_entry
        job = self._job_queue.for_source(entry.path) if entry is not None else None
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
        return (
            f"early_check_passed:{job.configuration_revision}:"
            f"{job.source.size_bytes}:{job.source.modified_ns}"
        )

    @Slot(int, int)
    def _show_improvement_progress(self, current: int, total: int) -> None:
        if total < 1 or current < 0 or current > total:
            return
        entry = self._current_batch_entry
        job = self._job_queue.for_source(entry.path) if entry is not None else None
        if job is not None and entry is not None and entry.stage is not None:
            self._job_execution.report_progress(
                job.id,
                stage_kind_from_process_stage(entry.stage),
                current,
                total,
                entry.stage.value,
            )
        if entry is not None:
            entry.progress_current = current
            entry.progress_total = total
        self._sync_workspace()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
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

        entry = self._current_batch_entry
        job = self._job_queue.for_source(entry.path) if entry is not None else None
        self._remember_processing_duration(job, result)
        outcome = self._job_outcomes.resolve_success(job.id, result) if job is not None else None
        if entry is not None:
            current_job = self._job_queue.for_source(entry.path)
            entry.status = (
                BatchStatus.REVIEW_PENDING
                if current_job is not None and current_job.status is JobStatus.WAITING_REVIEW
                else BatchStatus.COMPLETED
            )
            entry.result = result
            entry.error = None
            entry.finished_at = monotonic()
            entry.stage_started_at = None
        self._result = result
        self._result_source_path = entry.path if entry is not None else self._source_path
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
            entry is not None
            and current_job is not None
            and current_job.status is JobStatus.COMPLETED
        ):
            self._record_completed_outcome(current_job, entry, result)
        self._sync_workspace(force_persist=True)

    @Slot(object)
    def _processing_failed(self, value: object) -> None:
        entry = self._current_batch_entry
        job = self._job_queue.for_source(entry.path) if entry is not None else None
        stage_kind = stage_kind_from_process_stage(entry.stage if entry is not None else None)
        failure = (
            value
            if isinstance(value, ProcessingFailure)
            else ProcessingFailure.from_saved(
                None,
                str(value),
                stage=stage_kind,
            )
        )
        if job is not None:
            self._job_outcomes.resolve_failure(
                job.id,
                stage_kind,
                failure.message,
                error_code=failure.kind.value,
            )
        if entry is not None:
            entry.status = BatchStatus.FAILED
            entry.error = failure.message
            entry.finished_at = monotonic()
            entry.stage_started_at = None
        self.parsezen_workspace.set_preparing_jobs(())
        if job is not None:
            self._record_recent_job(job, RecentJobStatus.FAILED)
        self._sync_workspace(force_persist=True)

    @Slot()
    def _processing_cancelled(self) -> None:
        pause_requested = self._pause_requested
        entry = self._current_batch_entry
        job = self._job_queue.for_source(entry.path) if entry is not None else None
        if job is not None:
            self._job_outcomes.resolve_cancellation(job.id, paused=pause_requested)
        if entry is not None:
            entry.status = BatchStatus.PAUSED if pause_requested else BatchStatus.CANCELLED
            entry.error = (
                "Procesamiento pausado; el trabajo seguro se conservará."
                if pause_requested
                else "Procesamiento cancelado."
            )
            entry.finished_at = monotonic()
            entry.stage_started_at = None
            if not pause_requested:
                try:
                    runtime = self._runtime_for_entry(entry)
                    if runtime is not None:
                        request, settings = runtime
                        clear_document_work_checkpoints(
                            request,
                            settings,
                            root=self._work_checkpoint_root,
                        )
                except OSError:
                    LOGGER.warning("cancelled_job_checkpoint_cleanup_failed")
        self.parsezen_workspace.set_preparing_jobs(())
        if job is not None and not pause_requested:
            self._record_recent_job(job, RecentJobStatus.CANCELLED)
        self._sync_workspace(force_persist=True)

    @Slot()
    def _processing_worker_finished(self) -> None:
        if self._pause_requested:
            self._finish_batch()
            return
        if self._batch_running and any(
            entry.status is BatchStatus.PENDING for entry in self._batch_entries
        ):
            self._current_batch_entry = None
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
        if self._batch_entries:
            self.add_source_paths(paths)
        else:
            self.set_source_paths(paths)

    @Slot()
    def _select_file(self) -> None:
        initial_directory = (
            self._source_path.parent if self._source_path is not None else Path.home()
        )
        filenames, _selected_filter = QFileDialog.getOpenFileNames(
            self,
            "Seleccionar documentos",
            str(initial_directory),
            "Documentos compatibles (*.txt *.md *.markdown *.docx *.pdf *.epub)",
        )
        if not filenames:
            return
        if self._batch_entries:
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
        view = ActivityView(jobs, parent=self)
        view.open_requested.connect(self._open_local_path)
        view.folder_requested.connect(self._open_local_path)
        view.clear_requested.connect(lambda: self._clear_recent_activity(view))
        self.parsezen_workspace.show_internal_view(view, "Actividad reciente")

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
        recent = RecentJob(
            source_path=job.source.path,
            status=RecentJobStatus.COMPLETED,
            finished_at=datetime.now(UTC),
            result_path=entry.result.final_path,
            summary=summary,
        )
        view = ActivityView((recent,), allow_clear=False, parent=self)
        view.open_requested.connect(self._open_local_path)
        view.folder_requested.connect(self._open_local_path)
        self.parsezen_workspace.show_internal_view(view, "Resumen del resultado")

    @Slot(str, object)
    def _configure_job(self, job_id: str, requested_stage: object) -> None:
        entry = self._entry_for_job_id(job_id)
        job = self._job_for_id(job_id)
        if entry is None or job is None:
            return
        if entry.status not in {
            BatchStatus.PENDING,
            BatchStatus.FAILED,
            BatchStatus.CANCELLED,
        }:
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
        configured_model = effective_ai_profile(job.configuration).model
        if (
            configured_model
            and not is_reasoning_model_id(configured_model)
            and configured_model not in {model_id for model_id, _ in models}
        ):
            models = ((configured_model, configured_model), *models)
        stage = requested_stage if isinstance(requested_stage, StageKind) else None
        compatible_count = sum(
            candidate.id != job.id
            and candidate.source.format is job.source.format
            and candidate.status in {JobStatus.QUEUED, JobStatus.FAILED, JobStatus.CANCELLED}
            for candidate in self._job_queue.jobs
        )
        dialog = JobConfigurationDialog(
            job,
            models=models,
            stage=stage,
            embedded=True,
            default_output_directory=self._settings.output_directory,
            default_ai_model=self._settings.model,
            default_ai_context=self._settings.context_window,
            ollama_status=self._ollama_status,
            compatible_job_count=compatible_count,
            parent=self.parsezen_workspace,
        )
        dialog.save_requested.connect(
            lambda job_id=job.id, editor=dialog: self._save_job_configuration(job_id, editor)
        )
        dialog.cancel_requested.connect(self._close_configuration_panel)
        dialog.models_requested.connect(self._show_model_manager)
        self.parsezen_workspace.set_configuring(job.id, None)
        self.parsezen_workspace.show_configuration_panel(
            dialog,
            f"Configurar · {job.source.path.name}",
        )
        if (
            self._auto_discover_ai
            and not self._ollama_models
            and not self._is_discovering_models
            and not self._is_recommending_models
            and not self._is_ai_setup_active
        ):
            self._start_model_discovery(automatic=True)

    @Slot()
    def _close_configuration_panel(self) -> None:
        self.parsezen_workspace.set_configuring(None, None)
        self.parsezen_workspace.close_configuration_panel()

    @Slot(object)
    def _close_internal_workflow(self, widget: object) -> None:
        """Route the shared back action through each workflow's safe exit."""

        if isinstance(widget, JobConfigurationDialog):
            if widget.request_close():
                self._close_configuration_panel()
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
        dialog.setModal(False)
        dialog.setWindowFlags(Qt.WindowType.Widget)
        loop = QEventLoop(self)
        dialog.finished.connect(loop.quit)
        self.parsezen_workspace.show_internal_view(dialog, title)
        loop.exec()
        result = dialog.result()
        self.parsezen_workspace.close_internal_view(dialog)
        return result

    def _present_model_manager(self, dialog: QDialog) -> None:
        """Keep model installation and selection inside the Parsezen window."""

        dialog.setModal(False)
        dialog.setWindowFlags(Qt.WindowType.Widget)
        dialog.finished.connect(
            lambda _result, manager=dialog: self.parsezen_workspace.close_internal_view(manager)
        )
        self.parsezen_workspace.show_internal_view(
            dialog,
            "IA local",
            replace_app_header=True,
        )

    @Slot()
    def _show_model_manager(self) -> None:
        manager = self._model_manager
        if manager is None:
            manager = ModelManagerDialog(
                self._ollama_models,
                self._settings.model,
                self._model_recommendations,
                self,
                context_window=self._settings.context_window,
                ollama_status=self._ollama_status,
            )
            manager.install_requested.connect(self._install_recommendation)
            manager.select_requested.connect(self._select_model_from_manager)
            manager.delete_requested.connect(self._confirm_model_delete)
            manager.custom_model_requested.connect(self._install_custom_model)
            manager.library_requested.connect(self._open_ollama_library)
            manager.cancel_requested.connect(self._cancel_ai_setup)
            manager.connection_action_requested.connect(self._handle_ai_primary_action)
            manager.context_window_changed.connect(self._select_context_from_manager)
            manager.finished.connect(self._model_manager_finished)
            self._model_manager = manager
            self._present_model_manager(manager)
            if self._is_discovering_models or self._is_recommending_models:
                manager.set_busy(True)
            elif self._ollama_status not in {
                OllamaStatus.READY,
                OllamaStatus.MISSING_MODEL,
            }:
                manager.set_recommendations_loading()
                self._start_model_discovery(automatic=False)
            elif self._model_recommendations is None:
                self._start_model_recommendations()
        elif self.parsezen_workspace.current_internal_widget is not manager:
            self._present_model_manager(manager)
        manager.set_inherited_job_count(
            sum(
                job.status is JobStatus.QUEUED and not job.configuration.ai.is_custom
                for job in self._job_queue.jobs
            )
        )

    @Slot(int)
    def _model_manager_finished(self, _result: int) -> None:
        self._model_manager = None

    @Slot(str)
    def _confirm_model_delete(self, model_id: str) -> None:
        """Prevent a local model from disappearing under unfinished work."""

        affected = tuple(
            job
            for job in self._job_queue.jobs
            if job.status is not JobStatus.COMPLETED
            and requires_ai(job.configuration)
            and effective_ai_profile(job.configuration).model == model_id
        )
        if not affected:
            model = next(
                (item for item in self._ollama_models if item.model_id == model_id),
                None,
            )
            if model is None or self._is_ai_setup_active or self._is_processing:
                return
            size = (
                f" aproximadamente {model.size_bytes / 1_000_000_000:.1f} GB"
                if model.size_bytes is not None
                else " espacio en disco"
            ).replace(".", ",")
            answer = QMessageBox.question(
                self._model_manager or self,
                "Eliminar modelo",
                (
                    f"¿Eliminar {model.display_name}?\n\n"
                    f"Puede liberar{size}; el espacio real puede ser menor si comparte "
                    "archivos con otros modelos."
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._start_ai_setup(
                    LocalAIAction.DELETE_MODEL,
                    model_id=model.model_id,
                )
            return

        custom_count = sum(effective_ai_profile(job.configuration).is_custom for job in affected)
        inherited_count = len(affected) - custom_count
        examples = ", ".join(job.source.path.name for job in affected[:3])
        if len(affected) > 3:
            examples += f" y {len(affected) - 3} más"
        actions: list[str] = []
        if inherited_count:
            actions.append("elige primero otro modelo predeterminado")
        if custom_count:
            actions.append("cambia la opción IA local de los documentos con un modelo específico")
        guidance = " y ".join(actions)
        QMessageBox.information(
            self._model_manager or self,
            "Modelo utilizado por trabajos pendientes",
            (
                f"No se puede eliminar este modelo porque lo usan "
                f"{len(affected)} trabajo{'s' if len(affected) != 1 else ''} "
                f"sin terminar: {examples}.\n\nPara continuar, {guidance}."
            ),
        )

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
            current = self._source_path.parent if self._source_path is not None else Path.home()
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
        if current == previous:
            return
        for job in self._job_queue.jobs:
            if (
                job.status not in {JobStatus.QUEUED, JobStatus.FAILED, JobStatus.CANCELLED}
                or job.configuration.output.directory_is_custom
            ):
                continue
            try:
                updated = job.with_configuration(
                    replace(
                        job.configuration,
                        output=replace(job.configuration.output, directory=current),
                    )
                )
            except ValueError:
                continue
            self._job_queue.replace(updated)
        self._sync_workspace(force_persist=True)

    @Slot(object)
    def _model_discovery_succeeded(self, connection: OllamaConnection) -> None:
        self._ollama_status = connection.status
        self._ollama_models = tuple(
            model for model in connection.models if not is_reasoning_model_id(model.model_id)
        )
        preferred = choose_ollama_model(self._ollama_models, self._pending_ai_model)
        if preferred is not None and self._settings.model != preferred:
            self._apply_settings(replace(self._settings, model=preferred))
        self._pending_ai_model = None
        self.parsezen_workspace.set_local_ai_status(
            connection.status,
            self._settings.model,
        )
        if self._model_manager is not None:
            self._model_manager.set_installed_models(
                self._ollama_models,
                self._settings.model,
            )
            self._model_manager.set_connection_status(
                connection.status,
                connection.message,
            )
            self._model_manager.finish_operation(
                "Lista de modelos actualizada",
                completed=True,
            )
        panel = self.parsezen_workspace.configuration_layout.itemAt(0)
        editor = panel.widget() if panel is not None else None
        if isinstance(editor, JobConfigurationDialog):
            editor.set_models(
                tuple(
                    (model.model_id, model.display_name)
                    for model in connection.models
                    if not is_reasoning_model_id(model.model_id)
                )
            )
            editor.set_ai_status(connection.status)

    @Slot(str)
    def _select_model_from_manager(self, model_id: str) -> None:
        selected_model = choose_ollama_model(self._ollama_models, model_id)
        if selected_model is None:
            if self._model_manager is not None:
                self._model_manager.set_custom_error(
                    "Ese modelo ya no aparece entre los instalados. Actualiza la lista."
                )
            return
        previous = AIProfileConfiguration(
            model=self._settings.model,
            context_window=self._settings.context_window,
        )
        if self._apply_settings(replace(self._settings, model=selected_model)) is None:
            return
        if self._model_manager is not None:
            self._model_manager.set_installed_models(
                self._ollama_models,
                selected_model,
            )
        self._propagate_global_ai_profile(
            previous,
            AIProfileConfiguration(
                model=self._settings.model,
                context_window=self._settings.context_window,
            ),
        )
        editor = self._active_configuration_editor()
        if editor is not None:
            editor.set_models(
                tuple(
                    (model.model_id, model.display_name)
                    for model in self._ollama_models
                    if not is_reasoning_model_id(model.model_id)
                )
            )
            editor.set_default_ai_profile(
                self._settings.model,
                self._settings.context_window,
            )

    @Slot(object)
    def _select_context_from_manager(self, context_window: object) -> None:
        previous = AIProfileConfiguration(
            model=self._settings.model,
            context_window=self._settings.context_window,
        )
        value = (
            context_window
            if isinstance(context_window, int) and not isinstance(context_window, bool)
            else None
        )
        if self._apply_settings(replace(self._settings, context_window=value)) is None:
            return
        self._propagate_global_ai_profile(
            previous,
            AIProfileConfiguration(
                model=self._settings.model,
                context_window=self._settings.context_window,
            ),
        )
        editor = self._active_configuration_editor()
        if editor is not None:
            editor.set_default_ai_profile(
                self._settings.model,
                self._settings.context_window,
            )

    def _start_model_discovery(self, *, automatic: bool) -> None:
        del automatic
        if (
            self._is_processing
            or self._is_discovering_models
            or self._is_recommending_models
            or self._is_ai_setup_active
        ):
            return
        if not self._local_ai.discover(self._settings.model):
            return
        self._is_discovering_models = True
        if self._model_manager is not None:
            self._model_manager.set_busy(True)
            self._model_manager.set_connection_status(None)
        self._sync_workspace()

    @Slot(str)
    def _model_discovery_failed(self, message: str) -> None:
        self._ollama_status = OllamaStatus.UNAVAILABLE
        self._ollama_models = ()
        self.parsezen_workspace.set_local_ai_status(
            self._ollama_status,
            self._settings.model,
        )
        if self._model_manager is not None:
            self._model_manager.set_installed_models((), None)
            self._model_manager.set_connection_status(OllamaStatus.UNAVAILABLE, message)
            self._model_manager.finish_operation(message)

    @Slot()
    def _model_discovery_finished(self) -> None:
        self._is_discovering_models = False
        if (
            self._model_manager is not None
            and not self._is_processing
            and not self._is_ai_setup_active
            and self._ollama_status in {OllamaStatus.READY, OllamaStatus.MISSING_MODEL}
        ):
            self._start_model_recommendations()
        elif self._model_manager is not None:
            self._model_manager.set_busy(False)
        self._sync_workspace()

    def _start_model_recommendations(self, *, force_refresh: bool = False) -> None:
        if (
            self._is_processing
            or self._is_discovering_models
            or self._is_recommending_models
            or self._is_ai_setup_active
        ):
            return
        if not self._local_ai.recommend(force_refresh=force_refresh):
            return
        self._is_recommending_models = True
        if self._model_manager is not None:
            self._model_manager.set_recommendations_loading()

    @Slot(object)
    def _model_recommendations_succeeded(
        self,
        recommendations: ModelRecommendations,
    ) -> None:
        self._model_recommendations = recommendations
        if self._model_manager is not None:
            self._model_manager.set_recommendations(recommendations)

    @Slot(str)
    def _model_recommendations_failed(self, message: str) -> None:
        self._model_recommendations = None
        if self._model_manager is not None:
            self._model_manager.set_recommendation_error(message)

    @Slot()
    def _model_recommendations_finished(self) -> None:
        self._is_recommending_models = False
        if self._model_manager is not None and not self._is_ai_setup_active:
            self._model_manager.set_busy(False)

    @Slot(object)
    def _install_recommendation(self, recommendation: ModelRecommendation) -> None:
        self._start_model_install(
            recommendation.model_id,
            expected_download_size_bytes=recommendation.download_size_bytes,
        )

    @Slot(str)
    def _install_custom_model(self, model_id: str) -> None:
        try:
            validated = validate_document_model_id(model_id)
        except LocalModelUnavailableError as exc:
            if self._model_manager is not None:
                self._model_manager.set_custom_error(str(exc))
            return
        if self._model_manager is not None:
            self._model_manager.set_custom_error(None)
        self._start_model_install(validated)

    def _start_model_install(
        self,
        model_id: str,
        *,
        expected_download_size_bytes: int | None = None,
    ) -> None:
        self._start_ai_setup(
            LocalAIAction.PULL_MODEL,
            model_id=model_id,
            expected_download_size_bytes=expected_download_size_bytes,
        )

    @Slot()
    def _open_ollama_library(self) -> None:
        if not QDesktopServices.openUrl(QUrl(OLLAMA_LIBRARY_URL)):
            QMessageBox.warning(
                self,
                "No se pudo abrir el catálogo",
                "No se pudo abrir el catálogo local de modelos de Ollama.",
            )

    @Slot()
    def _handle_ai_primary_action(self) -> None:
        actions = {
            OllamaStatus.NOT_INSTALLED: LocalAIAction.INSTALL,
            OllamaStatus.STOPPED: LocalAIAction.START,
            OllamaStatus.LOCAL_ONLY_REQUIRED: LocalAIAction.PROTECT,
        }
        action = actions.get(self._ollama_status) if self._ollama_status is not None else None
        if action is not None:
            self._start_ai_setup(action)
        elif self._ollama_status is OllamaStatus.MISSING_MODEL:
            self._show_model_manager()
        else:
            self._start_model_discovery(automatic=False)

    def _start_ai_setup(
        self,
        action: LocalAIAction,
        *,
        model_id: str | None = None,
        expected_download_size_bytes: int | None = None,
    ) -> None:
        if (
            self._is_processing
            or self._is_discovering_models
            or self._is_recommending_models
            or self._is_ai_setup_active
        ):
            return
        if not self._local_ai.setup(
            action,
            model_id=model_id,
            expected_download_size_bytes=expected_download_size_bytes,
        ):
            return
        self._is_ai_setup_active = True
        self._ai_setup_action = action
        self._ai_setup_succeeded = False
        self._pending_deleted_model = model_id if action is LocalAIAction.DELETE_MODEL else None
        messages = {
            LocalAIAction.INSTALL: "Instalando Ollama de forma segura…",
            LocalAIAction.START: "Iniciando Ollama…",
            LocalAIAction.PROTECT: "Activando el modo privado…",
            LocalAIAction.PULL_MODEL: "Preparando la descarga del modelo…",
            LocalAIAction.DELETE_MODEL: "Eliminando el modelo…",
        }
        if self._model_manager is not None:
            self._model_manager.set_operation(
                messages[action],
                percent=0 if action is LocalAIAction.PULL_MODEL else None,
                cancellable=action is LocalAIAction.PULL_MODEL,
            )

    @Slot(object, str)
    def _ai_setup_progress_changed(self, percent: object, message: str) -> None:
        if self._model_manager is None:
            return
        value = percent if isinstance(percent, int) and not isinstance(percent, bool) else None
        self._model_manager.set_operation(
            message,
            percent=value,
            cancellable=self._ai_setup_action is LocalAIAction.PULL_MODEL,
        )

    @Slot(str)
    def _ai_setup_succeeded_slot(self, model_id: str) -> None:
        self._ai_setup_succeeded = True
        if self._ai_setup_action is LocalAIAction.DELETE_MODEL:
            deleted = self._pending_deleted_model
            if deleted is not None and self._settings.model == deleted:
                self._apply_settings(replace(self._settings, model=None, context_window=None))
            self._pending_ai_model = None
        else:
            self._pending_ai_model = model_id or None
        if self._model_manager is not None:
            self._model_manager.set_operation(
                "Comprobando la lista de modelos…",
                percent=100,
            )

    @Slot(str)
    def _ai_setup_cancelled(self, message: str) -> None:
        self._ai_setup_succeeded = False
        self._pending_deleted_model = None
        if self._model_manager is not None:
            self._model_manager.finish_operation(message)

    @Slot(str)
    def _ai_setup_failed(self, message: str) -> None:
        self._ai_setup_succeeded = False
        self._pending_deleted_model = None
        if self._model_manager is not None:
            self._model_manager.finish_operation(message)

    @Slot()
    def _cancel_ai_setup(self) -> None:
        if not self._local_ai.cancel_setup():
            return
        if self._model_manager is not None:
            self._model_manager.cancel_button.setEnabled(False)
            self._model_manager.operation_label.setText("Cancelando la descarga…")

    @Slot()
    def _ai_setup_finished(self) -> None:
        succeeded = self._ai_setup_succeeded
        self._is_ai_setup_active = False
        self._ai_setup_action = None
        self._ai_setup_succeeded = False
        if succeeded:
            self._start_model_discovery(automatic=False)
        elif self._model_manager is not None:
            self._model_manager.set_busy(False)

    def _active_configuration_editor(self) -> JobConfigurationDialog | None:
        panel = self.parsezen_workspace.configuration_layout.itemAt(0)
        editor = panel.widget() if panel is not None else None
        return editor if isinstance(editor, JobConfigurationDialog) else None

    def _propagate_global_ai_profile(
        self,
        previous: AIProfileConfiguration,
        current: AIProfileConfiguration,
    ) -> None:
        """Refresh only queued jobs that explicitly inherit the global IA profile."""

        if current == previous:
            return
        for job in self._job_queue.jobs:
            if job.status is not JobStatus.QUEUED or job.configuration.ai.is_custom:
                continue
            try:
                updated = job.with_configuration(
                    replace(
                        job.configuration,
                        ai=replace(current, is_custom=False),
                    )
                )
            except ValueError:
                continue
            self._job_queue.replace(updated)
        self._sync_workspace(force_persist=True)

    def _save_job_configuration(
        self,
        job_id: str,
        dialog: JobConfigurationDialog,
    ) -> None:
        entry = self._entry_for_job_id(job_id)
        job = self._job_for_id(job_id)
        if entry is None or job is None:
            self._close_configuration_panel()
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
        if entry.status in {BatchStatus.FAILED, BatchStatus.CANCELLED}:
            entry.status = BatchStatus.PENDING
            entry.error = None
            entry.result = None
        if dialog.apply_compatible.isChecked():
            self._apply_configuration_to_compatible_jobs(
                configured_job,
                configuration,
            )
        self._sync_workspace(force_persist=True)
        self._close_configuration_panel()

    def _apply_configuration_to_compatible_jobs(
        self,
        source_job: DocumentJob,
        configuration: JobConfiguration,
    ) -> None:
        """Apply shared choices without overwriting source-specific PDF ranges."""

        for candidate in self._job_queue.jobs:
            if (
                candidate.id == source_job.id
                or candidate.source.format is not source_job.source.format
                or candidate.status not in {JobStatus.QUEUED, JobStatus.FAILED, JobStatus.CANCELLED}
            ):
                continue
            candidate_configuration = replace(
                configuration,
                page_range=candidate.configuration.page_range,
                force_pdf_ocr=candidate.configuration.force_pdf_ocr,
                output=replace(
                    configuration.output,
                    title=(
                        candidate.source.path.stem
                        if configuration.output.format is DocumentFormat.EPUB
                        else None
                    ),
                ),
            )
            try:
                updated = candidate.with_configuration(candidate_configuration)
                request, _runtime = request_and_settings_from_job(
                    updated,
                    timeout_seconds=self._settings.timeout_seconds,
                    checkpoint_retention_days=self._settings.checkpoint_retention_days,
                )
            except ValueError:
                continue
            self._job_queue.replace(updated)
            entry = self._entry_for_job_id(candidate.id)
            if entry is None:
                continue
            entry.pdf_page_range = request.pdf_page_range
            if entry.status in {BatchStatus.FAILED, BatchStatus.CANCELLED}:
                entry.status = BatchStatus.PENDING
                entry.error = None
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
        try:
            self._phase_reviews.reconcile(job.id, entry.result)
        except (KeyError, StateStoreError, ValueError) as exc:
            QMessageBox.warning(self, "No se pudo recuperar la revisión", str(exc))
            return
        phase_input = self._review_quality_phases(entry, job)
        if phase_input is None:
            return
        reviewed_source, quality_reviews = phase_input
        draft = entry.result.revision_draft
        if (
            draft is None
            and job.configuration.structure.enabled
            and job.configuration.structure.manual_review
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
            self._review_result_card(str(entry.path))
        self._sync_workspace(force_persist=True)

    def _personalize_epub(
        self,
        entry: BatchEntry,
        job: DocumentJob,
        reviewed_text: str,
        reviews: tuple[ReviewSession, ...],
    ) -> None:
        result = entry.result
        if result is None:
            return
        entry.status = BatchStatus.FINALIZING
        self._sync_workspace(force_persist=True)
        try:
            book = self._review_publication.prepare_book(job.id, result, reviewed_text)
        except (ParsezenError, StateStoreError, ValueError, OSError) as exc:
            entry.status = BatchStatus.REVIEW_PENDING
            self._keep_review_pending(entry, result)
            QMessageBox.warning(self, "No se pudo preparar el editor", str(exc))
            return
        try:
            editor = BookEditorDialog(
                book,
                self._artifact_store,
                job_id=job.id,
                destination=result.final_path,
                publish_on_accept=False,
                parent=self,
            )
            accepted = (
                self._exec_internal_dialog(editor, "Editor EPUB") == QDialog.DialogCode.Accepted
            )
        except (ParsezenError, ValueError, OSError) as exc:
            entry.status = BatchStatus.REVIEW_PENDING
            self._keep_review_pending(entry, result)
            QMessageBox.warning(self, "No se pudo abrir el editor", str(exc))
            return
        if not accepted:
            if editor.saved_for_later:
                try:
                    self._review_publication.save_book(job.id, editor.book)
                except (StateStoreError, ValueError, OSError) as exc:
                    QMessageBox.warning(
                        self,
                        "No se pudo guardar el borrador",
                        str(exc),
                    )
            entry.status = BatchStatus.REVIEW_PENDING
            self._keep_review_pending(entry, result)
            return
        try:
            published = self._review_publication.publish_book(
                job.id,
                result,
                reviewed_text,
                editor.book,
                reviews,
            )
        except (ParsezenError, StateStoreError, OSError, ValueError) as exc:
            self._publication_failed(entry, result, exc)
            return
        self._accept_published_review(entry, published)

    def _review_quality_phases(
        self,
        entry: BatchEntry,
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
            saved = tuple(
                review
                for review in self._state_store.load_reviews(job_id=job.id)
                if review.input_version == job.configuration_revision
                and review.status in {ReviewStatus.PENDING, ReviewStatus.APPLIED}
            )
            input_record = self._artifact_store.put_text(
                job_id=job.id,
                text=text,
                media_type="text/markdown; charset=utf-8",
            )
        except (StateStoreError, ValueError, OSError) as exc:
            QMessageBox.warning(self, "No se pudo abrir la revisión", str(exc))
            return None
        completed: list[ReviewSession] = []
        phase_plan = tuple(
            (item.kind, item.unit_count)
            for item in review_workload_for_result(result, job.configuration)
        )
        creators = (
            (
                ReviewKind.OCR,
                lambda: (
                    create_pdf_review(
                        result.pdf_quality_report,
                        entry.path,
                        job_id=job.id,
                        configuration_revision=job.configuration_revision,
                        input_artifact_id=input_record.id,
                        artifacts=self._artifact_store,
                    )
                    if result.pdf_quality_report is not None
                    else None
                ),
            ),
            (
                ReviewKind.TRANSLATION,
                lambda: (
                    create_translation_review(
                        result.translation_quality_report,
                        job_id=job.id,
                        configuration_revision=job.configuration_revision,
                        input_artifact_id=input_record.id,
                        artifacts=self._artifact_store,
                    )
                    if result.translation_quality_report is not None
                    else None
                ),
            ),
        )
        for kind, creator in creators:
            review = next((item for item in saved if item.kind is kind), None)
            try:
                review = review or creator()
            except (ParsezenError, ValueError, OSError) as exc:
                QMessageBox.warning(self, "No se pudo preparar la revisión", str(exc))
                return None
            if review is None:
                continue
            if review.status is ReviewStatus.APPLIED:
                try:
                    text = (
                        apply_pdf_review(text, review, self._artifact_store)
                        if kind is ReviewKind.OCR
                        else apply_translation_review(text, review, self._artifact_store)
                    )
                except (OSError, ValueError) as exc:
                    QMessageBox.warning(self, "No se pudo recuperar la revisión", str(exc))
                    return None
                completed.append(review)
                continue
            try:
                self._phase_reviews.prepare(job.id, result, review)
            except (StateStoreError, ValueError) as exc:
                QMessageBox.warning(self, "No se pudo preparar la revisión", str(exc))
                return None
            dialog = PhaseReviewDialog(
                review,
                self._artifact_store,
                phase_plan=phase_plan,
                parent=self,
            )
            if (
                self._exec_internal_dialog(dialog, "Revisión del documento")
                != QDialog.DialogCode.Accepted
            ):
                try:
                    self._phase_reviews.prepare(job.id, result, dialog.review)
                except (StateStoreError, ValueError) as exc:
                    QMessageBox.warning(self, "No se pudo guardar la revisión", str(exc))
                return None
            review = dialog.review
            try:
                reviewed_text = (
                    apply_pdf_review(text, review, self._artifact_store)
                    if kind is ReviewKind.OCR
                    else apply_translation_review(text, review, self._artifact_store)
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
            text = reviewed_text
            review = progress.applied_review
            completed.append(review)
        return text, tuple(completed)

    def _review_revision_by_phase(
        self,
        entry: BatchEntry,
        job: DocumentJob,
        *,
        draft: RevisionDraft,
        preceding_reviews: tuple[ReviewSession, ...] = (),
    ) -> None:
        result = entry.result
        if result is None:
            return
        try:
            saved = tuple(
                review
                for review in self._state_store.load_reviews(job_id=job.id)
                if review.input_version == job.configuration_revision
                and review.status in {ReviewStatus.PENDING, ReviewStatus.APPLIED}
            )
        except StateStoreError:
            saved = ()
        reviews: list[ReviewSession] = list(preceding_reviews)
        phase_plan = tuple(
            (item.kind, item.unit_count)
            for item in review_workload_for_result(result, job.configuration)
        )
        for revision_kind, review_kind in (
            (RevisionKind.CONTENT, ReviewKind.REFINEMENT),
            (RevisionKind.STRUCTURE, ReviewKind.STRUCTURE),
        ):
            review = next((item for item in saved if item.kind is review_kind), None)
            if review is None:
                review = create_revision_review(
                    draft,
                    revision_kind=revision_kind,
                    job_id=job.id,
                    configuration_revision=job.configuration_revision,
                    artifacts=self._artifact_store,
                )
            if review is None:
                continue
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
                dialog = PhaseReviewDialog(
                    review,
                    self._artifact_store,
                    phase_plan=phase_plan,
                    parent=self,
                )
                if (
                    self._exec_internal_dialog(dialog, "Revisión de correcciones")
                    != QDialog.DialogCode.Accepted
                ):
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

        entry.status = BatchStatus.FINALIZING
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
                entry.status = BatchStatus.REVIEW_PENDING
                self._keep_review_pending(entry, result)
                QMessageBox.warning(self, "No se pudo preparar el editor", str(exc))
                return
            try:
                editor = BookEditorDialog(
                    book,
                    self._artifact_store,
                    job_id=job.id,
                    destination=result.final_path,
                    publish_on_accept=False,
                    parent=self,
                )
                accepted = (
                    self._exec_internal_dialog(editor, "Editor EPUB") == QDialog.DialogCode.Accepted
                )
            except (ParsezenError, ValueError, OSError) as exc:
                entry.status = BatchStatus.REVIEW_PENDING
                self._keep_review_pending(entry, result)
                QMessageBox.warning(self, "No se pudo abrir el editor", str(exc))
                return
            if not accepted:
                if editor.saved_for_later:
                    try:
                        self._review_publication.save_book(job.id, editor.book)
                    except (StateStoreError, ValueError, OSError) as exc:
                        QMessageBox.warning(
                            self,
                            "No se pudo guardar el borrador",
                            str(exc),
                        )
                entry.status = BatchStatus.REVIEW_PENDING
                self._keep_review_pending(entry, result)
                return
            try:
                published = self._review_publication.publish_book(
                    job.id,
                    result,
                    reviewed_text,
                    editor.book,
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
        entry: BatchEntry,
        result: ProcessResult,
        reviewed_text: str,
        reviews: tuple[ReviewSession, ...],
    ) -> None:
        entry.status = BatchStatus.FINALIZING
        self._sync_workspace(force_persist=True)
        job = self._job_queue.for_source(entry.path)
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
        entry: BatchEntry,
        result: ProcessResult,
        error: Exception,
    ) -> None:
        entry.status = BatchStatus.REVIEW_PENDING
        entry.error = str(error)
        self._keep_review_pending(entry, result)
        QMessageBox.warning(
            self,
            "No se pudo finalizar la revisión",
            "El borrador y tus decisiones siguen guardados. Inténtalo de nuevo.",
        )

    def _accept_published_review(
        self,
        entry: BatchEntry,
        published: PublishedReview,
    ) -> None:
        updated = published.result
        finalized = published.finalization
        entry.result = updated
        entry.status = BatchStatus.COMPLETED
        entry.error = None
        current_job = self._job_queue.get(finalized.job.id)
        if current_job is not None:
            summary = self._record_completed_outcome(
                current_job,
                entry,
                updated,
                reviews=finalized.reviews,
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
        entry: BatchEntry,
        result: ProcessResult,
        *,
        reviews: tuple[ReviewSession, ...] = (),
    ) -> OutcomeSummary:
        summary = self._build_outcome_summary(job, entry, reviews=reviews)
        self._outcome_summaries[job.id] = summary
        self._record_recent_job(
            job,
            RecentJobStatus.COMPLETED,
            result_path=result.final_path,
            summary=summary,
        )
        return summary

    def _build_outcome_summary(
        self,
        job: DocumentJob,
        entry: BatchEntry,
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
    ) -> None:
        try:
            append_recent_jobs(
                (
                    RecentJob(
                        source_path=job.source.path,
                        status=status,
                        finished_at=datetime.now(UTC),
                        result_path=result_path,
                        summary=summary,
                    ),
                ),
                path=self._history_path,
            )
        except OSError:
            LOGGER.warning("recent_activity_write_failed")

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
        entry: BatchEntry,
        result: ProcessResult,
    ) -> None:
        """Restore a visible review gate when publication cannot finish."""

        job = self._job_queue.for_source(entry.path)
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
        if entry is None or entry.status is not BatchStatus.COMPLETED or entry.result is None:
            return
        self._open_local_path(entry.result.final_path)

    @Slot(str)
    def _open_job_result_folder(self, job_id: str) -> None:
        entry = self._entry_for_job_id(job_id)
        if entry is None or entry.status is not BatchStatus.COMPLETED or entry.result is None:
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
        entry = next(
            (candidate for candidate in self._batch_entries if str(candidate.path) == key),
            None,
        )
        if entry is None:
            return
        self._result = entry.result
        self._result_source_path = entry.path

    def _open_result_folder(self) -> None:
        result = self._result
        if result is not None:
            self._open_local_path(result.final_path.parent)

    def _review_result_card(self, key: str) -> None:
        entry = next(
            (candidate for candidate in self._batch_entries if str(candidate.path) == key),
            None,
        )
        if entry is not None and entry.result is not None:
            self._open_local_path(entry.result.final_path)

    @Slot(str)
    def _remove_job(self, job_id: str) -> None:
        entry = self._entry_for_job_id(job_id)
        job = self._job_for_id(job_id)
        if entry is None or job is None:
            return
        if entry.status in {BatchStatus.PAUSED, BatchStatus.REVIEW_PENDING}:
            pending_review = entry.status is BatchStatus.REVIEW_PENDING
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
        self._remove_batch_entry(entry.path)
        self._job_queue.remove(job_id)
        self._sync_workspace(force_persist=True)
        try:
            self._artifact_store.remove_job(job_id)
        except OSError:
            pass

    def _remove_batch_entry(self, path: Path) -> None:
        entry = next(
            (candidate for candidate in self._batch_entries if candidate.path == path),
            None,
        )
        if entry is None or entry.status is BatchStatus.PROCESSING:
            return
        self._batch_entries.remove(entry)
        self._source_paths = tuple(candidate.path for candidate in self._batch_entries)
        self._source_path = self._source_paths[0] if self._source_paths else None
        if not self._batch_entries:
            self._batch_settings = None
            self._result = None
            self._result_source_path = None

    @Slot(str, int)
    def _move_job(self, job_id: str, target_row: int) -> None:
        entry = self._entry_for_job_id(job_id)
        if entry is None:
            return
        self._reorder_batch_entry(self._batch_entries.index(entry), target_row)
        self._job_queue.move(job_id, target_row)
        self._sync_workspace(force_persist=True)

    def _reorder_batch_entry(self, source_row: int, target_row: int) -> None:
        if not 0 <= source_row < len(self._batch_entries):
            return
        entry = self._batch_entries[source_row]
        if self._is_processing and entry.status is not BatchStatus.PENDING:
            return
        target_row = max(0, min(target_row, len(self._batch_entries) - 1))
        if target_row == source_row:
            return
        self._batch_entries.pop(source_row)
        self._batch_entries.insert(target_row, entry)
        self._source_paths = tuple(candidate.path for candidate in self._batch_entries)
        if not self._is_processing and self._source_paths:
            self._source_path = self._source_paths[0]

    @Slot()
    def _pause_processing(self) -> None:
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
            entry = next(
                (
                    candidate
                    for candidate in self._batch_entries
                    if candidate.status is BatchStatus.REVIEW_PENDING
                ),
                None,
            )
            if entry is not None:
                job = self._job_queue.for_source(entry.path)
                if job is not None:
                    self._review_job(job.id, None)
            return
        if mode == "open_folder":
            entry = next(
                (candidate for candidate in self._batch_entries if candidate.result is not None),
                None,
            )
            if entry is not None:
                self._select_result_card(str(entry.path))
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
        try:
            self._prepare_independent_requests()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(
                self,
                "No se pudo preparar el procesamiento",
                str(exc),
            )
            self._prepared_run = None
            return
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
        self.parsezen_workspace.set_preparing_jobs(plan.job_ids)
        QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        try:
            self._job_queue.refresh_source(job.id, _source_from_path(job.source.path))
            prepared = prepare_queue_run(
                self._job_queue.jobs,
                timeout_seconds=self._settings.timeout_seconds,
                checkpoint_retention_days=self._settings.checkpoint_retention_days,
                metrics=self._processing_metrics,
                plan=plan,
            )
        except (OSError, ValueError) as exc:
            self.parsezen_workspace.set_preparing_jobs(())
            QMessageBox.warning(
                self,
                "No se pudo preparar el reintento",
                str(exc),
            )
            self._prepared_run = None
            return
        self._prepared_run = prepared
        for item in prepared.items:
            if item.preflight is not None:
                self._latest_forecasts[item.job_id] = item.preflight
            entry = self._entry_for_job_id(item.job_id)
            if entry is not None:
                entry.pdf_page_range = item.request.pdf_page_range
        self._start_prepared_run()

    def _prepare_independent_requests(self) -> None:
        self._ensure_configurations()
        plan = self._job_execution.plan_run()
        self._prepared_run = None
        self.parsezen_workspace.set_preparing_jobs(plan.job_ids)
        QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        try:
            for job_id in plan.job_ids:
                job = self._job_queue.get(job_id)
                if job is not None:
                    self._job_queue.refresh_source(job.id, _source_from_path(job.source.path))
            prepared = prepare_queue_run(
                self._job_queue.jobs,
                timeout_seconds=self._settings.timeout_seconds,
                checkpoint_retention_days=self._settings.checkpoint_retention_days,
                metrics=self._processing_metrics,
            )
        except (OSError, ValueError):
            self.parsezen_workspace.set_preparing_jobs(())
            raise
        self._prepared_run = prepared
        for item in prepared.items:
            if item.preflight is not None:
                self._latest_forecasts[item.job_id] = item.preflight
            entry = self._entry_for_job_id(item.job_id)
            if entry is None:
                continue
            entry.pdf_page_range = item.request.pdf_page_range

    def _refresh_preflight_forecasts(self, jobs: tuple[DocumentJob, ...]) -> None:
        planned_ids = frozenset(self._job_execution.plan_run().job_ids)
        forecasts: dict[str, DocumentPreflight] = {}
        for job in jobs:
            if job.id not in planned_ids or not job.is_configured:
                continue
            try:
                request, settings = request_and_settings_from_job(
                    job,
                    timeout_seconds=self._settings.timeout_seconds,
                    checkpoint_retention_days=self._settings.checkpoint_retention_days,
                )
                key = (
                    job.id,
                    job.configuration_revision,
                    job.source.size_bytes,
                    job.source.modified_ns,
                    settings.model,
                    self._metrics_generation,
                )
                forecast = self._forecast_cache.get(key)
                if forecast is None:
                    forecast, _profile = analyze_preflight(
                        job,
                        request,
                        settings,
                        self._processing_metrics,
                    )
                    self._forecast_cache[key] = forecast
                forecasts[job.id] = forecast
            except (OSError, ParsezenError, ValueError):
                # The authoritative execution preflight will expose the exact
                # validation error when the user starts the queue.
                continue
        queue_preflight = combine_preflights(tuple(forecasts.values())) if forecasts else None
        active_ids = {job.id for job in jobs}
        self._latest_forecasts = {
            job_id: forecast
            for job_id, forecast in self._latest_forecasts.items()
            if job_id in active_ids
        }
        self._latest_forecasts.update(forecasts)
        self.parsezen_workspace.set_preflight(forecasts, queue_preflight)

    def _runtime_estimates(self) -> dict[str, RuntimeEstimate]:
        now = monotonic()
        estimates = {}
        for entry in self._batch_entries:
            if entry.status is not BatchStatus.PROCESSING or entry.started_at is None:
                continue
            job = self._job_queue.for_source(entry.path)
            if job is None:
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
            entry = self._current_batch_entry
            if entry is None:
                return
            try:
                runtime = self._runtime_for_entry(entry)
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

    def _ensure_configurations(self) -> None:
        paths = tuple(entry.path for entry in self._batch_entries)
        self._job_queue.retain_sources(paths)
        for entry in self._batch_entries:
            key = _path_key(entry.path)
            if self._job_queue.for_source(entry.path) is None:
                self._job_queue.add(
                    _source_from_path(entry.path),
                    _default_configuration(
                        entry.path,
                        self._settings,
                    ),
                    job_id=uuid5(NAMESPACE_URL, key).hex,
                )
        self._job_queue.order_sources(paths)

    def _project_jobs(self) -> tuple[DocumentJob, ...]:
        self._ensure_configurations()
        return self._job_queue.jobs

    def _sync_workspace(self, *, force_persist: bool = False) -> bool:
        jobs = self._project_jobs()
        self.parsezen_workspace.set_output_directory(self._settings.output_directory)
        self.parsezen_workspace.set_local_ai_status(
            self._ollama_status,
            self._settings.model,
        )
        integrity_reports = {
            job.id: entry.result.final_integrity_report
            for entry in self._batch_entries
            if entry.result is not None
            and entry.result.final_integrity_report is not None
            and (job := self._job_queue.for_source(entry.path)) is not None
        }
        self.parsezen_workspace.set_integrity_reports(integrity_reports)
        self.parsezen_workspace.set_runtime_estimates(self._runtime_estimates())
        if jobs != self._last_projection:
            self.parsezen_workspace.set_jobs(jobs)
            self._refresh_preflight_forecasts(jobs)
            self._last_projection = jobs
        if self._state_restore_failed:
            self.parsezen_workspace.set_recovery_warning(_RECOVERY_READ_WARNING)
            return False
        now = monotonic()
        should_persist = (
            force_persist
            or (self._state_write_retry_pending or jobs != self._last_persisted_projection)
            and now - self._last_persisted_at >= _PERSIST_INTERVAL_SECONDS
        )
        if should_persist:
            try:
                self._state_store.replace_jobs(jobs)
            except StateStoreError:
                # Processing must not be interrupted because a progress
                # snapshot failed. The next timer tick retries it.
                self._state_write_retry_pending = True
                self._last_persisted_at = now
                self.parsezen_workspace.set_recovery_warning(_RECOVERY_WRITE_WARNING)
                return False
            self._state_write_retry_pending = False
            self._last_persisted_projection = jobs
            self._last_persisted_at = now
            self.parsezen_workspace.set_recovery_warning(self._state_recovery_notice)
        return True

    def _restore_workspace(self) -> frozenset[str] | None:
        try:
            saved_jobs = self._state_store.load_jobs()
        except StateStoreError:
            if self._backup_and_reset_unreadable_state():
                return None
            self._state_restore_failed = True
            return None
        entries: list[BatchEntry] = []
        restored_jobs: list[DocumentJob] = []
        retained_artifact_jobs: set[str] = set()
        reset_paused_jobs: set[str] = set()
        interrupted_jobs: set[str] = set()
        for job in saved_jobs:
            if not job.source.path.is_file():
                continue
            restored_jobs.append(job)
            request = None
            if job.is_configured:
                request, _runtime_settings = request_and_settings_from_job(
                    job,
                    timeout_seconds=self._settings.timeout_seconds,
                    checkpoint_retention_days=self._settings.checkpoint_retention_days,
                )
            source_unchanged = _source_is_unchanged(job.source)
            status = BatchStatus.PENDING
            result = None
            if job.status is JobStatus.COMPLETED and job.result_path and job.result_path.is_file():
                status = BatchStatus.COMPLETED
                result = ProcessResult(final_path=job.result_path)
            elif job.status is JobStatus.WAITING_REVIEW and source_unchanged:
                try:
                    result = self._result_snapshots.load(job.id)
                except (StateStoreError, OSError, ValueError):
                    result = None
                status = (
                    BatchStatus.REVIEW_PENDING
                    if result is not None and result.final_path.is_file()
                    else BatchStatus.PAUSED
                )
                if status is BatchStatus.REVIEW_PENDING:
                    retained_artifact_jobs.add(job.id)
                else:
                    reset_paused_jobs.add(job.id)
            elif job.status is JobStatus.WAITING_REVIEW:
                status = BatchStatus.PAUSED
                reset_paused_jobs.add(job.id)
            elif job.status is JobStatus.FAILED:
                status = BatchStatus.FAILED
            elif job.status is JobStatus.CANCELLED:
                status = BatchStatus.CANCELLED
            elif job.status is not JobStatus.QUEUED:
                # Running/review/paused work resumes from the processor's safe
                # checkpoint rather than pretending an incomplete output is final.
                status = BatchStatus.PAUSED
                if job.status is JobStatus.RUNNING:
                    interrupted_jobs.add(job.id)
            entries.append(
                BatchEntry(
                    job.source.path,
                    status=status,
                    pdf_page_range=request.pdf_page_range if request is not None else None,
                    result=result,
                    error=(
                        (
                            (
                                _SOURCE_CHANGED_MESSAGE
                                if not source_unchanged
                                else "No se pudo recuperar la revisión completa; el trabajo "
                                "continuará desde el último punto seguro."
                            )
                            if job.status is JobStatus.WAITING_REVIEW
                            else "Trabajo recuperado desde el último punto seguro."
                        )
                        if status is BatchStatus.PAUSED
                        else None
                    ),
                )
            )
        if entries:
            self._job_queue.restore(tuple(restored_jobs))
            for job_id in reset_paused_jobs:
                self._job_execution.reset_paused(job_id)
            for job_id in interrupted_jobs:
                self._job_execution.recover_interrupted(job_id)
            self._batch_entries = BatchQueue(entries)
            self._source_paths = tuple(entry.path for entry in entries)
            self._source_path = self._source_paths[0]
            self._batch_settings = self._settings
        return frozenset(retained_artifact_jobs)

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
        entry: BatchEntry,
        job: DocumentJob,
    ) -> None:
        entry.status = BatchStatus.PAUSED
        entry.result = None
        entry.error = _SOURCE_CHANGED_MESSAGE
        self._job_execution.reset_paused(job.id)
        try:
            self._state_store.delete_review_material(job.id)
            self._artifact_store.remove_job(job.id)
        except (StateStoreError, OSError, ValueError):
            LOGGER.warning("changed_source_review_cleanup_failed")

    def _has_unfinished_jobs(self) -> bool:
        return any(
            entry.status not in {BatchStatus.COMPLETED, BatchStatus.CANCELLED}
            for entry in self._batch_entries
        )

    def _prune_orphaned_review_artifacts(self, retained_job_ids: frozenset[str]) -> None:
        try:
            removed = self._artifact_store.prune_orphaned_jobs(retained_job_ids)
        except (OSError, ValueError):
            LOGGER.warning("orphaned_review_artifact_cleanup_failed")
            return
        if removed:
            LOGGER.info("orphaned_review_artifacts_removed count=%d", len(removed))

    def _entry_for_job_id(self, job_id: str) -> BatchEntry | None:
        job = self._job_queue.get(job_id)
        if job is None:
            return None
        return next(
            (
                entry
                for entry in self._batch_entries
                if _path_key(entry.path) == _path_key(job.source.path)
            ),
            None,
        )

    def _job_for_id(self, job_id: str) -> DocumentJob | None:
        return self._job_queue.get(job_id)


def _source_from_path(path: Path) -> DocumentSource:
    try:
        return DocumentSource.inspect(path)
    except OSError:
        return DocumentSource(path, DocumentFormat.from_path(path), 0, 0)


def _source_is_unchanged(source: DocumentSource) -> bool:
    try:
        statistics = source.path.stat()
    except OSError:
        return False
    return statistics.st_size == source.size_bytes and statistics.st_mtime_ns == source.modified_ns


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
        DocumentFormat.MARKDOWN if source_format is DocumentFormat.PDF else source_format
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
            is_custom=False,
        ),
        translation=TranslationConfiguration(),
        refinement=RefinementConfiguration(),
        structure=StructureConfiguration(enabled=False, manual_review=True),
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
