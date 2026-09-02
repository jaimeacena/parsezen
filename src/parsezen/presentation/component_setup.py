"""Small fixed-component setup view for the local AI capabilities.

The view deliberately does not know how to install an Ollama model.  A caller
can inject either the already evaluated readiness states or an explicit catalog
plus the pure inputs required by :mod:`parsezen.component_readiness`.  The only
outgoing setup action is a capability identifier, never an arbitrary model tag,
URL, or endpoint.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from parsezen.component_readiness import (
    ComponentCatalogEntry,
    ComponentReadiness,
    ReadinessStatus,
    evaluate_component_catalog,
)
from parsezen.local_ai_policy import ComponentCapability, ComponentVerification
from parsezen.local_models import HardwareComponent, LocalHardware
from parsezen.presentation.design_system import BREAKPOINTS, SPACING

_VISIBLE_COMPONENTS: Final[tuple[ComponentCapability, ...]] = (
    ComponentCapability.TRANSLATION,
    ComponentCapability.REVIEW,
)

_CatalogInput = Mapping[
    ComponentCapability | HardwareComponent | str,
    ComponentCatalogEntry,
]
_StateInput = Mapping[Any, ComponentReadiness | ReadinessStatus]
_VerificationInput = Mapping[
    ComponentCapability | HardwareComponent | str,
    ComponentVerification | None,
]

_COMPONENT_TITLES: Final[dict[ComponentCapability, str]] = {
    ComponentCapability.TRANSLATION: "Traducción IA",
    ComponentCapability.REVIEW: "Revisión IA",
}

_COMPONENT_DESCRIPTIONS: Final[dict[ComponentCapability, str]] = {
    ComponentCapability.TRANSLATION: "Traducción local de documentos.",
    ComponentCapability.REVIEW: "Revisión local del resultado.",
}

_STATUS_TITLES: Final[dict[ReadinessStatus, str]] = {
    ReadinessStatus.PREPARED: "Preparado",
    ReadinessStatus.DOWNLOADABLE: "Descargable",
    ReadinessStatus.INSUFFICIENT: "Equipo insuficiente",
}

_STATUS_DESCRIPTIONS: Final[dict[ReadinessStatus, str]] = {
    ReadinessStatus.PREPARED: "Listo para usar.",
    ReadinessStatus.DOWNLOADABLE: "Disponible para preparar en este equipo.",
    ReadinessStatus.INSUFFICIENT: "Este equipo no cumple los requisitos.",
}


class ComponentReadinessCard(QFrame):
    """Accessible, fixed-capability card with no model-selection controls."""

    download_requested = Signal(object)
    cancel_requested = Signal(object)

    def __init__(
        self,
        component: ComponentCapability,
        *,
        parent: QWidget | None = None,
    ) -> None:
        if component not in _VISIBLE_COMPONENTS:
            raise ValueError("La interfaz solo admite Traducción IA y Revisión IA.")
        super().__init__(parent)
        self.component = component
        self._busy = False
        self.setObjectName("componentSetupRow")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)
        self.setMinimumHeight(96)

        layout = QGridLayout(self)
        self.content_layout = layout
        layout.setContentsMargins(SPACING.md, SPACING.md, SPACING.md, SPACING.md)
        layout.setHorizontalSpacing(SPACING.md)
        layout.setVerticalSpacing(SPACING.xs)

        self.title_label = QLabel(_COMPONENT_TITLES[component], self)
        self.title_label.setObjectName("componentTitle")
        self.title_label.setWordWrap(True)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.title_label, 0, 0)

        self.status_label = QLabel(self)
        self.status_label.setObjectName("componentStatus")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumWidth(0)
        self.status_label.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Preferred,
        )
        layout.addWidget(self.status_label, 0, 1)

        self.description_label = QLabel(_COMPONENT_DESCRIPTIONS[component], self)
        self.description_label.setObjectName("componentDescription")
        self.description_label.setWordWrap(True)
        self.description_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        layout.addWidget(self.description_label, 1, 0, 1, 2)

        self.detail_label = QLabel(self)
        self.detail_label.setObjectName("componentDetail")
        self.detail_label.setWordWrap(True)
        self.detail_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.detail_label, 2, 0)

        self.download_button = QPushButton("Descargar componente", self)
        self.download_button.setObjectName("componentDownloadButton")
        self.download_button.setAccessibleName(
            f"Descargar componente de {_COMPONENT_TITLES[component]}"
        )
        self.download_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.download_button.clicked.connect(self._handle_download_click)
        layout.addWidget(self.download_button, 2, 1)
        layout.setColumnStretch(0, 1)

        self.set_readiness(
            ComponentReadiness(component, ReadinessStatus.INSUFFICIENT, ("readiness_unavailable",))
        )

    def set_compact_mode(self, compact: bool) -> None:
        """Stack status and actions when two columns would clip at 320 px."""

        layout = self.content_layout
        for widget in (
            self.title_label,
            self.status_label,
            self.description_label,
            self.detail_label,
            self.download_button,
        ):
            layout.removeWidget(widget)
        if compact:
            self.status_label.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
            layout.addWidget(self.title_label, 0, 0, 1, 2)
            layout.addWidget(self.status_label, 1, 0, 1, 2)
            layout.addWidget(self.description_label, 2, 0, 1, 2)
            layout.addWidget(self.detail_label, 3, 0, 1, 2)
            layout.addWidget(
                self.download_button,
                4,
                0,
                1,
                2,
                Qt.AlignmentFlag.AlignLeft,
            )
            return
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.title_label, 0, 0)
        layout.addWidget(self.status_label, 0, 1)
        layout.addWidget(self.description_label, 1, 0, 1, 2)
        layout.addWidget(self.detail_label, 2, 0)
        layout.addWidget(self.download_button, 2, 1)

    def set_readiness(self, readiness: ComponentReadiness) -> None:
        """Render one validated state without exposing its model identity."""

        if readiness.component is not self.component:
            raise ValueError("El estado no corresponde a esta capacidad.")
        status = readiness.status
        self.status_label.setText(_STATUS_TITLES[status])
        self.status_label.setProperty("status", status.value)
        self.status_label.setAccessibleName(
            f"{_COMPONENT_TITLES[self.component]}: {_STATUS_TITLES[status]}"
        )
        self.detail_label.setText(_STATUS_DESCRIPTIONS[status])
        downloadable = status is ReadinessStatus.DOWNLOADABLE
        self.download_button.setVisible(downloadable or self._busy)
        self.download_button.setEnabled(downloadable or self._busy)
        self.setProperty("status", status.value)
        self.style().unpolish(self)
        self.style().polish(self)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def set_busy(self, busy: bool, *, message: str | None = None) -> None:
        """Show bounded install progress without exposing a model selector."""

        self._busy = busy
        if busy:
            self.status_label.setText(message or "Preparando…")
            self.status_label.setProperty("status", "busy")
            self.download_button.setText("Cancelar descarga")
            self.download_button.setAccessibleName(
                f"Cancelar descarga de {_COMPONENT_TITLES[self.component]}"
            )
            self.download_button.setVisible(True)
            self.download_button.setEnabled(True)
        else:
            self.download_button.setText("Descargar componente")
            self.download_button.setAccessibleName(
                f"Descargar componente de {_COMPONENT_TITLES[self.component]}"
            )
        self.style().unpolish(self)
        self.style().polish(self)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def set_progress(self, percent: object, message: str) -> None:
        if not self._busy:
            return
        suffix = (
            f" ({percent} %)" if isinstance(percent, int) and not isinstance(percent, bool) else ""
        )
        self.status_label.setText(f"{message}{suffix}")

    def _handle_download_click(self, _checked: bool = False) -> None:
        if self._busy:
            self.cancel_requested.emit(self.component)
        else:
            self.download_requested.emit(self.component)


class ComponentSetupDialog(QDialog):
    """Present exactly the two approved local AI components.

    ``readiness``/``states`` are preferred for callers that already own a
    snapshot.  ``catalog`` is accepted only with explicit hardware and
    verification maps and is evaluated through the pure readiness layer; it
    never performs local I/O.  The dialog emits ``download_requested`` but does
    not perform the requested operation.
    """

    download_requested = Signal(object)
    cancel_requested = Signal(object)
    refresh_requested = Signal()

    def __init__(
        self,
        *,
        catalog: _CatalogInput | None = None,
        readiness: _StateInput | None = None,
        states: _StateInput | None = None,
        hardware: LocalHardware | None = None,
        verifications: _VerificationInput | None = None,
        local_only_configured: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if readiness is not None and states is not None:
            raise ValueError("Usa readiness o states, no ambos.")
        injected = readiness if readiness is not None else states
        if injected is not None and catalog is not None:
            raise ValueError("Inyecta un catálogo o estados explícitos, no ambos.")
        self.setObjectName("componentSetupDialog")
        self.setWindowTitle("Componentes de IA local — Parsezen")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumSize(0, 420)
        self.resize(720, 520)
        self._compact = False
        self._readiness = self._states_from_inputs(
            catalog=catalog,
            injected=injected,
            hardware=hardware,
            verifications=verifications,
            local_only_configured=local_only_configured,
        )
        self.cards: dict[ComponentCapability, ComponentReadinessCard] = {}
        self._build_ui()
        self.set_readiness(self._readiness)

    @property
    def readiness(self) -> Mapping[ComponentCapability, ComponentReadiness]:
        """Return the fixed-capability state currently rendered by the view."""

        return self._readiness.copy()

    @property
    def states(self) -> Mapping[ComponentCapability, ComponentReadiness]:
        """Alias for callers that use the shorter UI vocabulary."""

        return self.readiness

    def set_readiness(
        self,
        states: _StateInput,
    ) -> None:
        """Replace the snapshot and refresh only the two fixed cards."""

        normalized = self._normalize_states(states)
        self._readiness = normalized
        for component, card in self.cards.items():
            card.set_readiness(normalized[component])

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self.width() <= BREAKPOINTS.compact and not self._compact:
            self._compact = True
            self._apply_compact_layout(True)
        elif self.width() > BREAKPOINTS.compact and self._compact:
            self._compact = False
            self._apply_compact_layout(False)
        margins = self.root_layout.contentsMargins()
        self.content_host.setFixedWidth(
            min(
                720,
                max(1, event.size().width() - margins.left() - margins.right()),
            )
        )

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(SPACING.lg, SPACING.lg, SPACING.lg, SPACING.lg)
        root.setSpacing(0)
        self.root_layout = root

        self.content_host = QWidget(self)
        self.content_host.setMaximumWidth(720)
        self.content_host.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        content = QVBoxLayout(self.content_host)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(SPACING.md)

        title = QLabel("Componentes disponibles", self.content_host)
        title.setObjectName("dialogTitle")
        title.setWordWrap(True)
        title.setMinimumWidth(0)
        content.addWidget(title)
        help_label = QLabel(
            "Se comprueban automáticamente al abrir esta vista. "
            "La preparación y el uso permanecen en este equipo.",
            self.content_host,
        )
        help_label.setObjectName("componentSetupHelp")
        help_label.setWordWrap(True)
        help_label.setMinimumWidth(0)
        content.addWidget(help_label)

        self.cards_host = QWidget(self.content_host)
        self.cards_layout = QGridLayout(self.cards_host)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setHorizontalSpacing(0)
        self.cards_layout.setVerticalSpacing(SPACING.sm)
        for row, component in enumerate(_VISIBLE_COMPONENTS):
            card = ComponentReadinessCard(component, parent=self.cards_host)
            card.download_requested.connect(self.download_requested)
            card.cancel_requested.connect(self.cancel_requested)
            self.cards[component] = card
            self.cards_layout.addWidget(card, row, 0)
        self.component_cards = self.cards
        self.status_labels = {
            component: card.status_label for component, card in self.cards.items()
        }
        self.download_buttons = {
            component: card.download_button for component, card in self.cards.items()
        }
        self.cards_layout.setColumnStretch(0, 1)
        content.addWidget(self.cards_host)

        self.refresh_button = QPushButton("Comprobar de nuevo", self.content_host)
        self.refresh_button.setObjectName("componentRefreshButton")
        self.refresh_button.setAccessibleName("Actualizar estados de los componentes de IA")
        self.refresh_button.setFlat(True)
        self.refresh_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_button.clicked.connect(self.refresh_requested)
        content.addWidget(self.refresh_button, 0, Qt.AlignmentFlag.AlignRight)
        root.addWidget(
            self.content_host,
            0,
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter,
        )
        root.addStretch(1)

    def set_download_busy(self, component: ComponentCapability, busy: bool) -> None:
        """Set the visible operation state for one fixed capability."""

        card = self.cards.get(component)
        if card is not None:
            card.set_busy(busy)

    def set_download_progress(
        self,
        component: ComponentCapability,
        percent: object,
        message: str,
    ) -> None:
        card = self.cards.get(component)
        if card is not None:
            card.set_progress(percent, message)

    def _apply_compact_layout(self, compact: bool) -> None:
        if compact:
            self.root_layout.setContentsMargins(
                SPACING.sm,
                SPACING.md,
                SPACING.sm,
                SPACING.md,
            )
            for card in self.cards.values():
                card.set_compact_mode(True)
                card.setMinimumHeight(148)
        else:
            self.root_layout.setContentsMargins(
                SPACING.lg,
                SPACING.lg,
                SPACING.lg,
                SPACING.lg,
            )
            for card in self.cards.values():
                card.set_compact_mode(False)
                card.setMinimumHeight(96)

    @staticmethod
    def _states_from_inputs(
        *,
        catalog: _CatalogInput | None,
        injected: _StateInput | None,
        hardware: LocalHardware | None,
        verifications: _VerificationInput | None,
        local_only_configured: bool,
    ) -> dict[ComponentCapability, ComponentReadiness]:
        if injected is not None:
            return ComponentSetupDialog._normalize_states(injected)
        if catalog is not None and hardware is not None and verifications is not None:
            evaluated = evaluate_component_catalog(
                catalog,
                hardware,
                verifications,
                local_only_configured=local_only_configured,
            )
            return ComponentSetupDialog._normalize_states(evaluated)
        return ComponentSetupDialog._normalize_states({})

    @staticmethod
    def _normalize_states(
        states: _StateInput,
    ) -> dict[ComponentCapability, ComponentReadiness]:
        if not isinstance(states, Mapping):
            raise TypeError("Los estados de componentes deben ser un mapa explícito.")
        result: dict[ComponentCapability, ComponentReadiness] = {}
        for component in _VISIBLE_COMPONENTS:
            value = _lookup_state(states, component)
            if isinstance(value, ComponentReadiness):
                if value.component is component and value.status in _STATUS_TITLES:
                    result[component] = value
                    continue
            status = _coerce_status(value)
            if status is not None:
                result[component] = ComponentReadiness(component, status)
                continue
            result[component] = ComponentReadiness(
                component,
                ReadinessStatus.INSUFFICIENT,
                ("readiness_unavailable",),
            )
        return result


def _lookup_state(
    states: _StateInput,
    component: ComponentCapability,
) -> ComponentReadiness | ReadinessStatus | None:
    for key, value in states.items():
        if key is component or key == component or key == component.value:
            return value
    return None


def _coerce_status(value: object) -> ReadinessStatus | None:
    if isinstance(value, ReadinessStatus):
        return value
    if isinstance(value, str):
        try:
            return ReadinessStatus(value)
        except ValueError:
            return None
    return None


__all__ = ["ComponentReadinessCard", "ComponentSetupDialog"]
