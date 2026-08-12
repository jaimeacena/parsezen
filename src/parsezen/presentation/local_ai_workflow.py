"""In-window orchestration for discovery, selection and setup of local AI."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from PySide6.QtCore import Qt, QUrl, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QMessageBox

from parsezen.application.configuration_rules import requires_ai
from parsezen.domain.jobs import AIProfileConfiguration, JobStatus
from parsezen.errors import LocalModelUnavailableError
from parsezen.local_models import (
    OLLAMA_LIBRARY_URL,
    OllamaConnection,
    OllamaStatus,
    choose_ollama_model,
    is_reasoning_model_id,
    validate_document_model_id,
)
from parsezen.model_recommendations import ModelRecommendation, ModelRecommendations
from parsezen.presentation.job_configuration_dialog import JobConfigurationDialog
from parsezen.presentation.local_ai_controller import LocalAIAction
from parsezen.presentation.model_manager import ModelManagerDialog


class LocalAIWorkflow:
    """Coordinate local-model UI actions while the window owns all mutable state."""

    def __init__(self, window: Any) -> None:
        self._window = window

    def _present_model_manager(self, dialog: QDialog) -> None:
        """Keep model installation and selection inside the Parsezen window."""

        dialog.setModal(False)
        dialog.setWindowFlags(Qt.WindowType.Widget)
        dialog.finished.connect(
            lambda _result, manager=dialog: self._window.parsezen_workspace.close_internal_view(
                manager
            )
        )
        self._window.parsezen_workspace.show_internal_view(
            dialog,
            "IA local",
            replace_app_header=True,
        )

    @Slot()
    def _show_model_manager(self) -> None:
        manager = self._window._model_manager
        if manager is None:
            manager = ModelManagerDialog(
                self._window._ollama_models,
                self._window._settings.model,
                self._window._model_recommendations,
                self._window,
                context_window=self._window._settings.context_window,
                ollama_status=self._window._ollama_status,
            )
            manager.install_requested.connect(self._install_recommendation)
            manager.select_requested.connect(self._select_model_from_manager)
            manager.delete_requested.connect(self._confirm_model_delete)
            manager.custom_model_requested.connect(self._install_custom_model)
            manager.library_requested.connect(self._open_ollama_library)
            manager.cancel_requested.connect(self._cancel_ai_setup)
            manager.connection_action_requested.connect(self._handle_ai_primary_action)
            manager.context_window_changed.connect(self._select_context_from_manager)
            manager.finished.connect(self._window._model_manager_finished)
            self._window._model_manager = manager
            self._window._present_model_manager(manager)
            if self._window._is_discovering_models or self._window._is_recommending_models:
                manager.set_busy(True)
            elif self._window._ollama_status not in {
                OllamaStatus.READY,
                OllamaStatus.MISSING_MODEL,
            }:
                manager.set_recommendations_loading()
                self._window._start_model_discovery(automatic=False)
            elif self._window._model_recommendations is None:
                self._window._start_model_recommendations()
        elif self._window.parsezen_workspace.current_internal_widget is not manager:
            self._window._present_model_manager(manager)
        manager.set_inherited_job_count(
            sum(
                job.status is JobStatus.QUEUED and requires_ai(job.configuration)
                for job in self._window._job_queue.jobs
            )
        )

    @Slot(int)
    def _model_manager_finished(self, _result: int) -> None:
        self._window._model_manager = None

    @Slot(str)
    def _confirm_model_delete(self, model_id: str) -> None:
        """Prevent a local model from disappearing under unfinished work."""

        affected = tuple(
            job
            for job in self._window._job_queue.jobs
            if job.status is not JobStatus.COMPLETED
            and requires_ai(job.configuration)
            and job.configuration.ai.model == model_id
        )
        if not affected:
            model = next(
                (item for item in self._window._ollama_models if item.model_id == model_id),
                None,
            )
            if model is None or self._window._is_ai_setup_active or self._window._is_processing:
                return
            size = (
                f" aproximadamente {model.size_bytes / 1_000_000_000:.1f} GB"
                if model.size_bytes is not None
                else " espacio en disco"
            ).replace(".", ",")
            answer = QMessageBox.question(
                self._window._model_manager or self._window,
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
                self._window._start_ai_setup(
                    LocalAIAction.DELETE_MODEL,
                    model_id=model.model_id,
                )
            return

        inherited_count = len(affected)
        custom_count = 0
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
            self._window._model_manager or self._window,
            "Modelo utilizado por trabajos pendientes",
            (
                f"No se puede eliminar este modelo porque lo usan "
                f"{len(affected)} trabajo{'s' if len(affected) != 1 else ''} "
                f"sin terminar: {examples}.\n\nPara continuar, {guidance}."
            ),
        )

    @Slot(object)
    def _model_discovery_succeeded(self, connection: OllamaConnection) -> None:
        self._window._ollama_status = connection.status
        self._window._ollama_models = tuple(
            model for model in connection.models if not is_reasoning_model_id(model.model_id)
        )
        preferred = choose_ollama_model(self._window._ollama_models, self._window._pending_ai_model)
        if preferred is not None and self._window._settings.model != preferred:
            self._window._apply_settings(replace(self._window._settings, model=preferred))
        self._window._pending_ai_model = None
        self._window.parsezen_workspace.set_local_ai_status(
            connection.status,
            self._window._settings.model,
        )
        if self._window._model_manager is not None:
            self._window._model_manager.set_installed_models(
                self._window._ollama_models,
                self._window._settings.model,
            )
            self._window._model_manager.set_connection_status(
                connection.status,
                connection.message,
            )
            self._window._model_manager.finish_operation(
                "Lista de modelos actualizada",
                completed=True,
            )
        editor = self._active_configuration_editor()
        if editor is not None:
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
        selected_model = choose_ollama_model(self._window._ollama_models, model_id)
        if selected_model is None:
            if self._window._model_manager is not None:
                self._window._model_manager.set_custom_error(
                    "Ese modelo ya no aparece entre los instalados. Actualiza la lista."
                )
            return
        previous = AIProfileConfiguration(
            model=self._window._settings.model,
            context_window=self._window._settings.context_window,
        )
        if (
            self._window._apply_settings(replace(self._window._settings, model=selected_model))
            is None
        ):
            return
        if self._window._model_manager is not None:
            self._window._model_manager.set_installed_models(
                self._window._ollama_models,
                selected_model,
            )
        self._window._propagate_global_ai_profile(
            previous,
            AIProfileConfiguration(
                model=self._window._settings.model,
                context_window=self._window._settings.context_window,
            ),
        )
        editor = self._window._active_configuration_editor()
        if editor is not None:
            editor.set_models(
                tuple(
                    (model.model_id, model.display_name)
                    for model in self._window._ollama_models
                    if not is_reasoning_model_id(model.model_id)
                )
            )
            editor.set_default_ai_profile(
                self._window._settings.model,
                self._window._settings.context_window,
            )

    @Slot(object)
    def _select_context_from_manager(self, context_window: object) -> None:
        previous = AIProfileConfiguration(
            model=self._window._settings.model,
            context_window=self._window._settings.context_window,
        )
        value = (
            context_window
            if isinstance(context_window, int) and not isinstance(context_window, bool)
            else None
        )
        if (
            self._window._apply_settings(replace(self._window._settings, context_window=value))
            is None
        ):
            return
        self._window._propagate_global_ai_profile(
            previous,
            AIProfileConfiguration(
                model=self._window._settings.model,
                context_window=self._window._settings.context_window,
            ),
        )
        editor = self._window._active_configuration_editor()
        if editor is not None:
            editor.set_default_ai_profile(
                self._window._settings.model,
                self._window._settings.context_window,
            )

    def _start_model_discovery(self, *, automatic: bool) -> None:
        del automatic
        if (
            self._window._is_processing
            or self._window._is_discovering_models
            or self._window._is_recommending_models
            or self._window._is_ai_setup_active
        ):
            return
        if not self._window._local_ai.discover(self._window._settings.model):
            return
        self._window._is_discovering_models = True
        if self._window._model_manager is not None:
            self._window._model_manager.set_busy(True)
            self._window._model_manager.set_connection_status(None)
        self._window._sync_workspace()

    @Slot(str)
    def _model_discovery_failed(self, message: str) -> None:
        self._window._ollama_status = OllamaStatus.UNAVAILABLE
        self._window._ollama_models = ()
        self._window.parsezen_workspace.set_local_ai_status(
            self._window._ollama_status,
            self._window._settings.model,
        )
        if self._window._model_manager is not None:
            self._window._model_manager.set_installed_models((), None)
            self._window._model_manager.set_connection_status(OllamaStatus.UNAVAILABLE, message)
            self._window._model_manager.finish_operation(message)

    @Slot()
    def _model_discovery_finished(self) -> None:
        self._window._is_discovering_models = False
        if (
            self._window._model_manager is not None
            and not self._window._is_processing
            and not self._window._is_ai_setup_active
            and self._window._ollama_status in {OllamaStatus.READY, OllamaStatus.MISSING_MODEL}
        ):
            self._window._start_model_recommendations()
        elif self._window._model_manager is not None:
            self._window._model_manager.set_busy(False)
        self._window._sync_workspace()

    def _start_model_recommendations(self, *, force_refresh: bool = False) -> None:
        if (
            self._window._is_processing
            or self._window._is_discovering_models
            or self._window._is_recommending_models
            or self._window._is_ai_setup_active
        ):
            return
        if not self._window._local_ai.recommend(force_refresh=force_refresh):
            return
        self._window._is_recommending_models = True
        if self._window._model_manager is not None:
            self._window._model_manager.set_recommendations_loading()

    @Slot(object)
    def _model_recommendations_succeeded(
        self,
        recommendations: ModelRecommendations,
    ) -> None:
        self._window._model_recommendations = recommendations
        if self._window._model_manager is not None:
            self._window._model_manager.set_recommendations(recommendations)

    @Slot(str)
    def _model_recommendations_failed(self, message: str) -> None:
        self._window._model_recommendations = None
        if self._window._model_manager is not None:
            self._window._model_manager.set_recommendation_error(message)

    @Slot()
    def _model_recommendations_finished(self) -> None:
        self._window._is_recommending_models = False
        if self._window._model_manager is not None and not self._window._is_ai_setup_active:
            self._window._model_manager.set_busy(False)

    @Slot(object)
    def _install_recommendation(self, recommendation: ModelRecommendation) -> None:
        self._window._start_model_install(
            recommendation.model_id,
            expected_download_size_bytes=recommendation.download_size_bytes,
        )

    @Slot(str)
    def _install_custom_model(self, model_id: str) -> None:
        try:
            validated = validate_document_model_id(model_id)
        except LocalModelUnavailableError as exc:
            if self._window._model_manager is not None:
                self._window._model_manager.set_custom_error(str(exc))
            return
        if self._window._model_manager is not None:
            self._window._model_manager.set_custom_error(None)
        self._window._start_model_install(validated)

    def _start_model_install(
        self,
        model_id: str,
        *,
        expected_download_size_bytes: int | None = None,
    ) -> None:
        self._window._start_ai_setup(
            LocalAIAction.PULL_MODEL,
            model_id=model_id,
            expected_download_size_bytes=expected_download_size_bytes,
        )

    @Slot()
    def _open_ollama_library(self) -> None:
        if not QDesktopServices.openUrl(QUrl(OLLAMA_LIBRARY_URL)):
            QMessageBox.warning(
                self._window,
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
        action = (
            actions.get(self._window._ollama_status)
            if self._window._ollama_status is not None
            else None
        )
        if action is not None:
            self._window._start_ai_setup(action)
        elif self._window._ollama_status is OllamaStatus.MISSING_MODEL:
            self._window._show_model_manager()
        else:
            self._window._start_model_discovery(automatic=False)

    def _start_ai_setup(
        self,
        action: LocalAIAction,
        *,
        model_id: str | None = None,
        expected_download_size_bytes: int | None = None,
    ) -> None:
        if (
            self._window._is_processing
            or self._window._is_discovering_models
            or self._window._is_recommending_models
            or self._window._is_ai_setup_active
        ):
            return
        if not self._window._local_ai.setup(
            action,
            model_id=model_id,
            expected_download_size_bytes=expected_download_size_bytes,
        ):
            return
        self._window._is_ai_setup_active = True
        self._window._ai_setup_action = action
        self._window._ai_setup_succeeded = False
        self._window._pending_deleted_model = (
            model_id if action is LocalAIAction.DELETE_MODEL else None
        )
        messages = {
            LocalAIAction.INSTALL: "Instalando Ollama de forma segura…",
            LocalAIAction.START: "Iniciando Ollama…",
            LocalAIAction.PROTECT: "Activando el modo privado…",
            LocalAIAction.PULL_MODEL: "Preparando la descarga del modelo…",
            LocalAIAction.DELETE_MODEL: "Eliminando el modelo…",
        }
        if self._window._model_manager is not None:
            self._window._model_manager.set_operation(
                messages[action],
                percent=0 if action is LocalAIAction.PULL_MODEL else None,
                cancellable=action is LocalAIAction.PULL_MODEL,
            )

    @Slot(object, str)
    def _ai_setup_progress_changed(self, percent: object, message: str) -> None:
        if self._window._model_manager is None:
            return
        value = percent if isinstance(percent, int) and not isinstance(percent, bool) else None
        self._window._model_manager.set_operation(
            message,
            percent=value,
            cancellable=self._window._ai_setup_action is LocalAIAction.PULL_MODEL,
        )

    @Slot(str)
    def _ai_setup_succeeded_slot(self, model_id: str) -> None:
        self._window._ai_setup_succeeded = True
        if self._window._ai_setup_action is LocalAIAction.DELETE_MODEL:
            deleted = self._window._pending_deleted_model
            if deleted is not None and self._window._settings.model == deleted:
                self._window._apply_settings(
                    replace(self._window._settings, model=None, context_window=None)
                )
            self._window._pending_ai_model = None
        else:
            self._window._pending_ai_model = model_id or None
        if self._window._model_manager is not None:
            self._window._model_manager.set_operation(
                "Comprobando la lista de modelos…",
                percent=100,
            )

    @Slot(str)
    def _ai_setup_cancelled(self, message: str) -> None:
        self._window._ai_setup_succeeded = False
        self._window._pending_deleted_model = None
        if self._window._model_manager is not None:
            self._window._model_manager.finish_operation(message)

    @Slot(str)
    def _ai_setup_failed(self, message: str) -> None:
        self._window._ai_setup_succeeded = False
        self._window._pending_deleted_model = None
        if self._window._model_manager is not None:
            self._window._model_manager.finish_operation(message)

    @Slot()
    def _cancel_ai_setup(self) -> None:
        if not self._window._local_ai.cancel_setup():
            return
        if self._window._model_manager is not None:
            self._window._model_manager.cancel_button.setEnabled(False)
            self._window._model_manager.operation_label.setText("Cancelando la descarga…")

    @Slot()
    def _ai_setup_finished(self) -> None:
        succeeded = self._window._ai_setup_succeeded
        self._window._is_ai_setup_active = False
        self._window._ai_setup_action = None
        self._window._ai_setup_succeeded = False
        if succeeded:
            self._window._start_model_discovery(automatic=False)
        elif self._window._model_manager is not None:
            self._window._model_manager.set_busy(False)

    def _active_configuration_editor(self) -> JobConfigurationDialog | None:
        active = self._window._active_configuration_dialog
        return active if isinstance(active, JobConfigurationDialog) else None

    def _propagate_global_ai_profile(
        self,
        previous: AIProfileConfiguration,
        current: AIProfileConfiguration,
    ) -> None:
        """Refresh only queued jobs that explicitly inherit the global IA profile."""

        self._window._queue_configuration.propagate_ai_profile(previous, current)
        self._window._sync_workspace(force_persist=True)


LOCAL_AI_WORKFLOW_METHODS = frozenset(
    name
    for name, value in vars(LocalAIWorkflow).items()
    if name.startswith("_") and not name.startswith("__") and callable(value)
)
