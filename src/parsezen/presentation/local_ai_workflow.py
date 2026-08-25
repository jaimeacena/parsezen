"""Explicit owner for local discovery, component readiness and setup."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import QDialog, QMessageBox, QWidget

from parsezen.component_catalog import PRODUCT_COMPONENT_CATALOG, REVIEW_REQUIRED_NOTICES
from parsezen.component_readiness import ComponentReadiness, ReadinessStatus
from parsezen.domain.jobs import LocalAIComponentSnapshot, LocalAIPolicySnapshot
from parsezen.local_ai_policy import ComponentCapability
from parsezen.local_models import (
    OllamaConnection,
    OllamaModel,
    OllamaStatus,
)
from parsezen.presentation.component_setup import ComponentSetupDialog
from parsezen.presentation.job_configuration_dialog import JobConfigurationDialog
from parsezen.presentation.local_ai_controller import LocalAIAction, LocalAIController
from parsezen.presentation.workspace import ParsezenWorkspace
from parsezen.settings import AppSettings


class LocalAIWorkflow(QObject):
    """Own local-AI presentation state and coordinate its explicit dependencies."""

    state_changed = Signal(bool)
    component_download_requested = Signal(object)

    def __init__(
        self,
        controller: LocalAIController,
        workspace: ParsezenWorkspace,
        *,
        settings: Callable[[], AppSettings],
        processing_active: Callable[[], bool],
        active_editor: Callable[[], JobConfigurationDialog | None],
        propagate_ai_policy: Callable[[LocalAIPolicySnapshot], object] | None = None,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._workspace = workspace
        self._settings = settings
        self._processing_active = processing_active
        self._active_editor = active_editor
        self._propagate_ai_policy = propagate_ai_policy
        self._parent_widget = parent
        self._status: OllamaStatus | None = None
        self._models: tuple[OllamaModel, ...] = ()
        self._component_readiness: dict[ComponentCapability, ComponentReadiness] = {}
        self._component_setup: ComponentSetupDialog | None = None
        self._setup_action: LocalAIAction | None = None
        self._setup_succeeded = False
        self._download_capability: ComponentCapability | None = None
        self._download_succeeded = False

        controller.discovery_succeeded.connect(self.model_discovery_succeeded)
        controller.discovery_failed.connect(self.model_discovery_failed)
        controller.discovery_finished.connect(self.model_discovery_finished)
        controller.component_readiness.connect(self.set_component_readiness)
        controller.component_progress.connect(self.component_download_progress)
        controller.component_succeeded.connect(self.component_download_succeeded)
        controller.component_cancelled.connect(self.component_download_cancelled)
        controller.component_failed.connect(self.component_download_failed)
        controller.component_finished.connect(self.component_download_finished)
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
    def component_readiness(self) -> Mapping[ComponentCapability, ComponentReadiness]:
        """Return the explicit, content-free component states shown in the UI."""

        return self._component_readiness.copy()

    @property
    def component_setup(self) -> ComponentSetupDialog | None:
        return self._component_setup

    @property
    def discovering(self) -> bool:
        return self._controller.discovering

    @property
    def setting_up(self) -> bool:
        return self._controller.setting_up

    @property
    def busy(self) -> bool:
        return self.discovering or self.setting_up or self._controller.installing_component

    @property
    def setup_action(self) -> LocalAIAction | None:
        return self._setup_action

    def _present_component_setup(self, dialog: QDialog) -> None:
        dialog.setModal(False)
        dialog.setWindowFlags(Qt.WindowType.Widget)
        dialog.finished.connect(
            lambda _result, setup=dialog: self._workspace.close_internal_view(setup)
        )
        self._workspace.show_internal_view(dialog, "IA local", replace_app_header=True)

    @Slot()
    def show_component_setup(self) -> None:
        """Open the fixed local-AI component view."""

        setup = self._component_setup
        if setup is None:
            setup = ComponentSetupDialog(
                readiness=self._component_readiness,
                parent=self._parent_widget,
            )
            setup.download_requested.connect(self._download_component_requested)
            setup.cancel_requested.connect(self.cancel_component_download)
            setup.refresh_requested.connect(lambda: self.start_model_discovery(automatic=False))
            setup.finished.connect(self.component_setup_finished)
            self._component_setup = setup
            self._present_component_setup(setup)
        elif self._workspace.current_internal_widget is not setup:
            self._present_component_setup(setup)
        if not self.busy:
            self.start_model_discovery(automatic=False)

    def set_component_readiness(
        self,
        states: Mapping[object, ComponentReadiness | ReadinessStatus],
    ) -> None:
        """Inject an evaluated snapshot without letting the view discover models."""

        normalized: dict[ComponentCapability, ComponentReadiness] = {}
        for component in (ComponentCapability.TRANSLATION, ComponentCapability.REVIEW):
            value = next(
                (
                    candidate
                    for key, candidate in states.items()
                    if key is component or key == component or key == component.value
                ),
                None,
            )
            if isinstance(value, ComponentReadiness) and value.component is component:
                normalized[component] = value
            elif isinstance(value, ReadinessStatus):
                normalized[component] = ComponentReadiness(component, value)
        self._component_readiness = normalized
        if self._component_setup is not None:
            self._component_setup.set_readiness(normalized)
        self._propagate_prepared_policy(normalized)

    @Slot(int)
    def component_setup_finished(self, _result: int) -> None:
        self._component_setup = None

    @Slot(object)
    def model_discovery_succeeded(self, value: object) -> None:
        if not isinstance(value, OllamaConnection):
            return
        self._status = value.status
        self._models = tuple(value.models)
        settings = self._settings()
        self._workspace.set_local_ai_status(value.status, settings.model)
        editor = self._active_editor()
        if editor is not None:
            editor.set_ai_status(value.status)

    @Slot(object)
    def _download_component_requested(self, capability: object) -> None:
        if not isinstance(capability, ComponentCapability):
            return
        if capability is ComponentCapability.REVIEW and not self._confirm_review_license_gate():
            return
        if self._processing_active() or self.busy:
            return
        self._download_capability = capability
        self._download_succeeded = False
        if self._component_setup is not None:
            self._component_setup.set_download_busy(capability, True)
        if not self._controller.install_component(capability):
            if self._component_setup is not None:
                self._component_setup.set_download_busy(capability, False)
            self._download_capability = None
            return
        self.state_changed.emit(False)

    @Slot(object)
    def cancel_component_download(self, capability: object) -> None:
        if capability == self._download_capability:
            self._controller.cancel_component()

    @Slot(object, object, str)
    def component_download_progress(
        self,
        capability: object,
        percent: object,
        message: str,
    ) -> None:
        if (
            isinstance(capability, ComponentCapability)
            and capability == self._download_capability
            and self._component_setup is not None
        ):
            self._component_setup.set_download_progress(capability, percent, message)

    @Slot(object)
    def component_download_succeeded(self, capability: object) -> None:
        if capability == self._download_capability:
            self._download_succeeded = True

    @Slot(object, str)
    def component_download_cancelled(self, capability: object, message: str) -> None:
        del message
        if (
            isinstance(capability, ComponentCapability)
            and capability == self._download_capability
            and self._component_setup is not None
        ):
            self._component_setup.set_download_busy(capability, False)

    @Slot(object, str)
    def component_download_failed(self, capability: object, message: str) -> None:
        if (
            isinstance(capability, ComponentCapability)
            and capability == self._download_capability
            and self._component_setup is not None
        ):
            self._component_setup.set_download_progress(capability, None, message)

    @Slot(object)
    def component_download_finished(self, capability: object) -> None:
        if (
            not isinstance(capability, ComponentCapability)
            or capability != self._download_capability
        ):
            return
        if self._component_setup is not None:
            self._component_setup.set_download_busy(capability, False)
        succeeded = self._download_succeeded
        self._download_capability = None
        self._download_succeeded = False
        self.state_changed.emit(False)
        if succeeded:
            self.start_model_discovery(automatic=False)

    def _confirm_review_license_gate(self) -> bool:
        notices = "\n".join(f"• {notice}" for notice in REVIEW_REQUIRED_NOTICES)
        answer = QMessageBox.question(
            self._parent_widget,
            "Licencia del componente de revisión",
            "Antes de descargar el componente LFM de revisión, confirma que has "
            "leído sus condiciones:\n\n" + notices,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer is QMessageBox.StandardButton.Yes

    def _propagate_prepared_policy(
        self,
        states: Mapping[ComponentCapability, ComponentReadiness],
    ) -> None:
        if self._propagate_ai_policy is None:
            return
        snapshots: dict[ComponentCapability, LocalAIComponentSnapshot | None] = {
            ComponentCapability.TRANSLATION: None,
            ComponentCapability.REVIEW: None,
        }
        for capability in snapshots:
            readiness = states.get(capability)
            if readiness is None or readiness.status is not ReadinessStatus.PREPARED:
                continue
            manifest = PRODUCT_COMPONENT_CATALOG[capability].manifest
            try:
                snapshots[capability] = LocalAIComponentSnapshot(
                    policy_version=manifest.policy_version,
                    model=manifest.model_name,
                    digest=manifest.ollama_digest,
                    context_window=manifest.context_window,
                )
            except (TypeError, ValueError):
                snapshots[capability] = None
        self._propagate_ai_policy(
            LocalAIPolicySnapshot(
                translation=snapshots[ComponentCapability.TRANSLATION],
                review=snapshots[ComponentCapability.REVIEW],
            )
        )

    def start_model_discovery(self, *, automatic: bool) -> None:
        del automatic
        if self._processing_active() or self.busy:
            return
        if not self._controller.discover(self._settings().model):
            return
        self.state_changed.emit(False)

    @Slot(str)
    def model_discovery_failed(self, message: str) -> None:
        del message
        self._status = OllamaStatus.UNAVAILABLE
        self._models = ()
        self.set_component_readiness({})
        self._workspace.set_local_ai_status(self._status, self._settings().model)

    @Slot()
    def model_discovery_finished(self) -> None:
        self.state_changed.emit(False)

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
            self.show_component_setup()
        else:
            self.start_model_discovery(automatic=False)

    def start_ai_setup(
        self,
        action: LocalAIAction,
    ) -> None:
        if self._processing_active() or self.busy:
            return
        if not self._controller.setup(action):
            return
        self._setup_action = action
        self._setup_succeeded = False

    @Slot(object, str)
    def ai_setup_progress_changed(self, percent: object, message: str) -> None:
        del percent, message

    @Slot(str)
    def ai_setup_succeeded(self, model_id: str) -> None:
        self._setup_succeeded = True
        del model_id

    @Slot(str)
    def ai_setup_cancelled(self, message: str) -> None:
        self._setup_succeeded = False
        del message

    @Slot(str)
    def ai_setup_failed(self, message: str) -> None:
        self._setup_succeeded = False
        del message

    @Slot()
    def cancel_setup(self) -> None:
        self._controller.cancel_setup()

    @Slot()
    def ai_setup_finished(self) -> None:
        succeeded = self._setup_succeeded
        self._setup_action = None
        self._setup_succeeded = False
        if succeeded:
            self.start_model_discovery(automatic=False)


__all__ = ["LocalAIWorkflow"]
