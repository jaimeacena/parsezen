"""Explicit owner for discovery, selection and setup of local AI."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from PySide6.QtCore import QObject, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QMessageBox, QWidget

from parsezen.application.configuration_rules import requires_ai
from parsezen.domain.jobs import AIProfileConfiguration, DocumentJob, JobStatus
from parsezen.errors import LocalModelUnavailableError
from parsezen.local_models import (
    OLLAMA_LIBRARY_URL,
    OllamaConnection,
    OllamaModel,
    OllamaStatus,
    choose_ollama_model,
    is_reasoning_model_id,
    validate_document_model_id,
)
from parsezen.model_recommendations import ModelRecommendation, ModelRecommendations
from parsezen.presentation.job_configuration_dialog import JobConfigurationDialog
from parsezen.presentation.local_ai_controller import LocalAIAction, LocalAIController
from parsezen.presentation.model_manager import ModelManagerDialog
from parsezen.presentation.workspace import ParsezenWorkspace
from parsezen.settings import AppSettings


class LocalAIWorkflow(QObject):
    """Own local-AI presentation state and coordinate its explicit dependencies."""

    state_changed = Signal(bool)

    def __init__(
        self,
        controller: LocalAIController,
        workspace: ParsezenWorkspace,
        *,
        settings: Callable[[], AppSettings],
        apply_settings: Callable[[AppSettings], AppSettings | None],
        jobs: Callable[[], tuple[DocumentJob, ...]],
        processing_active: Callable[[], bool],
        active_editor: Callable[[], JobConfigurationDialog | None],
        propagate_ai_profile: Callable[[AIProfileConfiguration, AIProfileConfiguration], object],
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._workspace = workspace
        self._settings = settings
        self._apply_settings = apply_settings
        self._jobs = jobs
        self._processing_active = processing_active
        self._active_editor = active_editor
        self._propagate_ai_profile = propagate_ai_profile
        self._parent_widget = parent
        self._status: OllamaStatus | None = None
        self._models: tuple[OllamaModel, ...] = ()
        self._recommendations: ModelRecommendations | None = None
        self._manager: ModelManagerDialog | None = None
        self._setup_action: LocalAIAction | None = None
        self._pending_ai_model: str | None = None
        self._pending_deleted_model: str | None = None
        self._setup_succeeded = False

        controller.discovery_succeeded.connect(self.model_discovery_succeeded)
        controller.discovery_failed.connect(self.model_discovery_failed)
        controller.discovery_finished.connect(self.model_discovery_finished)
        controller.recommendations_succeeded.connect(self.model_recommendations_succeeded)
        controller.recommendations_failed.connect(self.model_recommendations_failed)
        controller.recommendations_finished.connect(self.model_recommendations_finished)
        controller.setup_progress.connect(self.ai_setup_progress_changed)
        controller.setup_succeeded.connect(self.ai_setup_succeeded)
        controller.setup_cancelled.connect(self.ai_setup_cancelled)
        controller.setup_failed.connect(self.ai_setup_failed)
        controller.setup_finished.connect(self.ai_setup_finished)

    @property
    def status(self) -> OllamaStatus | None:
        return self._status

    @property
    def models(self) -> tuple[OllamaModel, ...]:
        return self._models

    @property
    def recommendations(self) -> ModelRecommendations | None:
        return self._recommendations

    @property
    def manager(self) -> ModelManagerDialog | None:
        return self._manager

    @property
    def discovering(self) -> bool:
        return self._controller.discovering

    @property
    def recommending(self) -> bool:
        return self._controller.recommending

    @property
    def setting_up(self) -> bool:
        return self._controller.setting_up

    @property
    def busy(self) -> bool:
        return self.discovering or self.recommending or self.setting_up

    @property
    def setup_action(self) -> LocalAIAction | None:
        return self._setup_action

    @property
    def pending_ai_model(self) -> str | None:
        return self._pending_ai_model

    @property
    def pending_deleted_model(self) -> str | None:
        return self._pending_deleted_model

    def _present_model_manager(self, dialog: QDialog) -> None:
        dialog.setModal(False)
        dialog.setWindowFlags(Qt.WindowType.Widget)
        dialog.finished.connect(
            lambda _result, manager=dialog: self._workspace.close_internal_view(manager)
        )
        self._workspace.show_internal_view(dialog, "IA local", replace_app_header=True)

    @Slot()
    def show_model_manager(self) -> None:
        settings = self._settings()
        manager = self._manager
        if manager is None:
            manager = ModelManagerDialog(
                self._models,
                settings.model,
                self._recommendations,
                self._parent_widget,
                context_window=settings.context_window,
                ollama_status=self._status,
            )
            manager.install_requested.connect(self.install_recommendation)
            manager.select_requested.connect(self.select_model)
            manager.delete_requested.connect(self.confirm_model_delete)
            manager.custom_model_requested.connect(self.install_custom_model)
            manager.library_requested.connect(self.open_ollama_library)
            manager.cancel_requested.connect(self.cancel_setup)
            manager.connection_action_requested.connect(self.handle_primary_action)
            manager.context_window_changed.connect(self.select_context_window)
            manager.finished.connect(self.model_manager_finished)
            self._manager = manager
            self._present_model_manager(manager)
            if self.discovering or self.recommending:
                manager.set_busy(True)
            elif self._status not in {OllamaStatus.READY, OllamaStatus.MISSING_MODEL}:
                manager.set_recommendations_loading()
                self.start_model_discovery(automatic=False)
            elif self._recommendations is None:
                self.start_model_recommendations()
        elif self._workspace.current_internal_widget is not manager:
            self._present_model_manager(manager)
        manager.set_inherited_job_count(
            sum(
                job.status is JobStatus.QUEUED and requires_ai(job.configuration)
                for job in self._jobs()
            )
        )

    @Slot(int)
    def model_manager_finished(self, _result: int) -> None:
        self._manager = None

    @Slot(str)
    def confirm_model_delete(self, model_id: str) -> None:
        affected = tuple(
            job
            for job in self._jobs()
            if job.status is not JobStatus.COMPLETED
            and requires_ai(job.configuration)
            and job.configuration.ai.model == model_id
        )
        if not affected:
            model = next((item for item in self._models if item.model_id == model_id), None)
            if model is None or self.setting_up or self._processing_active():
                return
            size = (
                f" aproximadamente {model.size_bytes / 1_000_000_000:.1f} GB"
                if model.size_bytes is not None
                else " espacio en disco"
            ).replace(".", ",")
            answer = QMessageBox.question(
                self._manager or self._parent_widget,
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
                self.start_ai_setup(LocalAIAction.DELETE_MODEL, model_id=model.model_id)
            return

        examples = ", ".join(job.source.path.name for job in affected[:3])
        if len(affected) > 3:
            examples += f" y {len(affected) - 3} más"
        QMessageBox.information(
            self._manager or self._parent_widget,
            "Modelo utilizado por trabajos pendientes",
            (
                f"No se puede eliminar este modelo porque lo usan "
                f"{len(affected)} trabajo{'s' if len(affected) != 1 else ''} "
                f"sin terminar: {examples}.\n\nPara continuar, elige primero otro modelo "
                "predeterminado."
            ),
        )

    @Slot(object)
    def model_discovery_succeeded(self, value: object) -> None:
        if not isinstance(value, OllamaConnection):
            return
        self._status = value.status
        self._models = tuple(
            model for model in value.models if not is_reasoning_model_id(model.model_id)
        )
        settings = self._settings()
        preferred = choose_ollama_model(self._models, self._pending_ai_model)
        if preferred is not None and settings.model != preferred:
            self._apply_settings(replace(settings, model=preferred))
        self._pending_ai_model = None
        settings = self._settings()
        self._workspace.set_local_ai_status(value.status, settings.model)
        if self._manager is not None:
            self._manager.set_installed_models(self._models, settings.model)
            self._manager.set_connection_status(value.status, value.message)
            self._manager.finish_operation("Lista de modelos actualizada", completed=True)
        editor = self._active_editor()
        if editor is not None:
            editor.set_models(tuple((model.model_id, model.display_name) for model in self._models))
            editor.set_ai_status(value.status)

    @Slot(str)
    def select_model(self, model_id: str) -> None:
        selected_model = choose_ollama_model(self._models, model_id)
        if selected_model is None:
            if self._manager is not None:
                self._manager.set_custom_error(
                    "Ese modelo ya no aparece entre los instalados. Actualiza la lista."
                )
            return
        settings = self._settings()
        previous = AIProfileConfiguration(
            model=settings.model,
            context_window=settings.context_window,
        )
        if self._apply_settings(replace(settings, model=selected_model)) is None:
            return
        settings = self._settings()
        if self._manager is not None:
            self._manager.set_installed_models(self._models, selected_model)
        self._propagate_profile(
            previous,
            AIProfileConfiguration(
                model=settings.model,
                context_window=settings.context_window,
            ),
        )
        editor = self._active_editor()
        if editor is not None:
            editor.set_models(tuple((model.model_id, model.display_name) for model in self._models))
            editor.set_default_ai_profile(settings.model, settings.context_window)

    @Slot(object)
    def select_context_window(self, context_window: object) -> None:
        settings = self._settings()
        previous = AIProfileConfiguration(
            model=settings.model,
            context_window=settings.context_window,
        )
        value = (
            context_window
            if isinstance(context_window, int) and not isinstance(context_window, bool)
            else None
        )
        if self._apply_settings(replace(settings, context_window=value)) is None:
            return
        settings = self._settings()
        self._propagate_profile(
            previous,
            AIProfileConfiguration(
                model=settings.model,
                context_window=settings.context_window,
            ),
        )
        editor = self._active_editor()
        if editor is not None:
            editor.set_default_ai_profile(settings.model, settings.context_window)

    def start_model_discovery(self, *, automatic: bool) -> None:
        del automatic
        if self._processing_active() or self.busy:
            return
        if not self._controller.discover(self._settings().model):
            return
        if self._manager is not None:
            self._manager.set_busy(True)
            self._manager.set_connection_status(None)
        self.state_changed.emit(False)

    @Slot(str)
    def model_discovery_failed(self, message: str) -> None:
        self._status = OllamaStatus.UNAVAILABLE
        self._models = ()
        self._workspace.set_local_ai_status(self._status, self._settings().model)
        if self._manager is not None:
            self._manager.set_installed_models((), None)
            self._manager.set_connection_status(OllamaStatus.UNAVAILABLE, message)
            self._manager.finish_operation(message)

    @Slot()
    def model_discovery_finished(self) -> None:
        if (
            self._manager is not None
            and not self._processing_active()
            and not self.setting_up
            and self._status in {OllamaStatus.READY, OllamaStatus.MISSING_MODEL}
        ):
            self.start_model_recommendations()
        elif self._manager is not None:
            self._manager.set_busy(False)
        self.state_changed.emit(False)

    def start_model_recommendations(self, *, force_refresh: bool = False) -> None:
        if self._processing_active() or self.busy:
            return
        if not self._controller.recommend(force_refresh=force_refresh):
            return
        if self._manager is not None:
            self._manager.set_recommendations_loading()

    @Slot(object)
    def model_recommendations_succeeded(self, value: object) -> None:
        if not isinstance(value, ModelRecommendations):
            return
        self._recommendations = value
        if self._manager is not None:
            self._manager.set_recommendations(value)

    @Slot(str)
    def model_recommendations_failed(self, message: str) -> None:
        self._recommendations = None
        if self._manager is not None:
            self._manager.set_recommendation_error(message)

    @Slot()
    def model_recommendations_finished(self) -> None:
        if self._manager is not None and not self.setting_up:
            self._manager.set_busy(False)

    @Slot(object)
    def install_recommendation(self, value: object) -> None:
        if isinstance(value, ModelRecommendation):
            self.start_model_install(
                value.model_id,
                expected_download_size_bytes=value.download_size_bytes,
            )

    @Slot(str)
    def install_custom_model(self, model_id: str) -> None:
        try:
            validated = validate_document_model_id(model_id)
        except LocalModelUnavailableError as exc:
            if self._manager is not None:
                self._manager.set_custom_error(str(exc))
            return
        if self._manager is not None:
            self._manager.set_custom_error(None)
        self.start_model_install(validated)

    def start_model_install(
        self,
        model_id: str,
        *,
        expected_download_size_bytes: int | None = None,
    ) -> None:
        self.start_ai_setup(
            LocalAIAction.PULL_MODEL,
            model_id=model_id,
            expected_download_size_bytes=expected_download_size_bytes,
        )

    @Slot()
    def open_ollama_library(self) -> None:
        if not QDesktopServices.openUrl(QUrl(OLLAMA_LIBRARY_URL)):
            QMessageBox.warning(
                self._parent_widget,
                "No se pudo abrir el catálogo",
                "No se pudo abrir el catálogo local de modelos de Ollama.",
            )

    @Slot()
    def handle_primary_action(self) -> None:
        actions = {
            OllamaStatus.NOT_INSTALLED: LocalAIAction.INSTALL,
            OllamaStatus.STOPPED: LocalAIAction.START,
            OllamaStatus.LOCAL_ONLY_REQUIRED: LocalAIAction.PROTECT,
        }
        action = actions.get(self._status) if self._status is not None else None
        if action is not None:
            self.start_ai_setup(action)
        elif self._status is OllamaStatus.MISSING_MODEL:
            self.show_model_manager()
        else:
            self.start_model_discovery(automatic=False)

    def start_ai_setup(
        self,
        action: LocalAIAction,
        *,
        model_id: str | None = None,
        expected_download_size_bytes: int | None = None,
    ) -> None:
        if self._processing_active() or self.busy:
            return
        if not self._controller.setup(
            action,
            model_id=model_id,
            expected_download_size_bytes=expected_download_size_bytes,
        ):
            return
        self._setup_action = action
        self._setup_succeeded = False
        self._pending_deleted_model = model_id if action is LocalAIAction.DELETE_MODEL else None
        messages = {
            LocalAIAction.INSTALL: "Instalando Ollama de forma segura…",
            LocalAIAction.START: "Iniciando Ollama…",
            LocalAIAction.PROTECT: "Activando el modo privado…",
            LocalAIAction.PULL_MODEL: "Preparando la descarga del modelo…",
            LocalAIAction.DELETE_MODEL: "Eliminando el modelo…",
        }
        if self._manager is not None:
            self._manager.set_operation(
                messages[action],
                percent=0 if action is LocalAIAction.PULL_MODEL else None,
                cancellable=action is LocalAIAction.PULL_MODEL,
            )

    @Slot(object, str)
    def ai_setup_progress_changed(self, percent: object, message: str) -> None:
        if self._manager is None:
            return
        value = percent if isinstance(percent, int) and not isinstance(percent, bool) else None
        self._manager.set_operation(
            message,
            percent=value,
            cancellable=self._setup_action is LocalAIAction.PULL_MODEL,
        )

    @Slot(str)
    def ai_setup_succeeded(self, model_id: str) -> None:
        self._setup_succeeded = True
        if self._setup_action is LocalAIAction.DELETE_MODEL:
            deleted = self._pending_deleted_model
            settings = self._settings()
            if deleted is not None and settings.model == deleted:
                self._apply_settings(replace(settings, model=None, context_window=None))
            self._pending_ai_model = None
        else:
            self._pending_ai_model = model_id or None
        if self._manager is not None:
            self._manager.set_operation("Comprobando la lista de modelos…", percent=100)

    @Slot(str)
    def ai_setup_cancelled(self, message: str) -> None:
        self._setup_succeeded = False
        self._pending_deleted_model = None
        if self._manager is not None:
            self._manager.finish_operation(message)

    @Slot(str)
    def ai_setup_failed(self, message: str) -> None:
        self._setup_succeeded = False
        self._pending_deleted_model = None
        if self._manager is not None:
            self._manager.finish_operation(message)

    @Slot()
    def cancel_setup(self) -> None:
        if not self._controller.cancel_setup():
            return
        if self._manager is not None:
            self._manager.cancel_button.setEnabled(False)
            self._manager.operation_label.setText("Cancelando la descarga…")

    @Slot()
    def ai_setup_finished(self) -> None:
        succeeded = self._setup_succeeded
        self._setup_action = None
        self._setup_succeeded = False
        if succeeded:
            self.start_model_discovery(automatic=False)
        elif self._manager is not None:
            self._manager.set_busy(False)

    def _propagate_profile(
        self,
        previous: AIProfileConfiguration,
        current: AIProfileConfiguration,
    ) -> None:
        self._propagate_ai_profile(previous, current)
        self.state_changed.emit(True)


__all__ = ["LocalAIWorkflow"]
