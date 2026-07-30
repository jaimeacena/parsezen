"""Focused two-pane review for one document phase."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QKeySequence,
    QPainter,
    QPaintEvent,
    QPixmap,
    QResizeEvent,
    QShortcut,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFrame,
    QGridLayout,
    QLabel,
    QLayout,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from parsezen.domain.reviews import (
    ReviewChoice,
    ReviewKind,
    ReviewSession,
    ReviewSeverity,
    ReviewUnit,
)
from parsezen.infrastructure.artifact_store import ArtifactStore
from parsezen.presentation.design_system import BREAKPOINTS, COLORS, SPACING, back_icon

_PHASE_TITLES = {
    ReviewKind.OCR: "Revisar reconocimiento de página",
    ReviewKind.CONVERSION_WARNING: "Revisar conversión",
    ReviewKind.TRANSLATION: "Revisar traducción",
    ReviewKind.REFINEMENT: "Revisar correcciones",
    ReviewKind.STRUCTURE: "Revisar estructura",
}
_PHASE_LABELS = {
    ReviewKind.OCR: "OCR",
    ReviewKind.CONVERSION_WARNING: "Conversión",
    ReviewKind.TRANSLATION: "Traducción",
    ReviewKind.REFINEMENT: "Corrección",
    ReviewKind.STRUCTURE: "Estructura",
}


def _phase_color(kind: ReviewKind) -> str:
    return {
        ReviewKind.OCR: COLORS.phase_ocr,
        ReviewKind.CONVERSION_WARNING: COLORS.warning,
        ReviewKind.TRANSLATION: COLORS.phase_translation,
        ReviewKind.REFINEMENT: COLORS.phase_correction,
        ReviewKind.STRUCTURE: COLORS.phase_structure,
    }[kind]


_SEVERITY_LABELS = {
    ReviewSeverity.CRITICAL: "Prioridad crítica",
    ReviewSeverity.HIGH: "Prioridad alta",
    ReviewSeverity.MEDIUM: "Prioridad media",
    ReviewSeverity.LOW: "Prioridad baja",
}


@dataclass(frozen=True, slots=True)
class _PhaseWeight:
    kind: ReviewKind
    unit_count: int


class PhaseReviewDialog(QDialog):
    """Review only the units belonging to one phase and persist manual edits."""

    def __init__(
        self,
        review: ReviewSession,
        artifacts: ArtifactStore,
        *,
        phase_plan: tuple[ReviewKind | tuple[ReviewKind, int], ...] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if not review.units:
            raise ValueError("A review must contain at least one unit.")
        self._review = review
        self._artifacts = artifacts
        self._phase_plan = _normalize_phase_plan(
            phase_plan or (review.kind,),
            current_kind=review.kind,
            current_count=len(review.units),
        )
        self._index = review.first_unresolved_index() or 0
        self._manual_navigation = False
        self.setWindowTitle(f"{_PHASE_TITLES[review.kind]} · Parsezen")
        self.resize(1240, 780)

        layout = QVBoxLayout(self)
        self.root_layout = layout
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        self.progress_indicator = _ReviewProgressIndicator(
            self._phase_plan,
            review.kind,
            self,
        )
        layout.addWidget(self.progress_indicator)
        self.priority_summary = QLabel("", self)
        self.priority_summary.setObjectName("reviewPrioritySummary")
        self.priority_summary.setWordWrap(True)
        layout.addWidget(self.priority_summary)
        self.unit_summary = QLabel("", self)
        self.unit_summary.setObjectName("reviewUnitSummary")
        self.unit_summary.setWordWrap(True)
        layout.addWidget(self.unit_summary)
        self.unit_warning = QLabel("", self)
        self.unit_warning.setObjectName("reviewWarning")
        self.unit_warning.setWordWrap(True)
        self.unit_warning.hide()
        layout.addWidget(self.unit_warning)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter = splitter
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(1)
        self.original_pane = _ReviewPane(
            "Original",
            editable=False,
            selection_text="Conservar original",
            parent=splitter,
        )
        self.proposed_pane = _ReviewPane(
            "Corrección propuesta",
            editable=True,
            selection_text="Usar esta versión",
            parent=splitter,
        )
        splitter.addWidget(self.original_pane)
        splitter.addWidget(self.proposed_pane)
        splitter.setSizes([600, 600])
        layout.addWidget(splitter, 1)

        self.selection_group = QButtonGroup(self)
        self.selection_group.setExclusive(True)
        self.selection_group.addButton(self.original_pane.selector)
        self.selection_group.addButton(self.proposed_pane.selector)
        self.proposed_pane.editor.textChanged.connect(self._proposal_edited)

        footer = QGridLayout()
        self.footer_layout = footer
        self.previous_button = QPushButton("Anterior", self)
        self.previous_button.setIcon(back_icon())
        self.previous_button.setToolTip("Corrección anterior")
        self.previous_button.setAccessibleName("Corrección anterior")
        self.previous_button.clicked.connect(self._previous)
        footer.addWidget(self.previous_button, 0, 0)
        footer.setColumnStretch(1, 1)
        self.approve_all_button = QPushButton("Aprobar todas las propuestas", self)
        self.approve_all_button.setAccessibleName("Aprobar todas las propuestas seguras")
        self.approve_all_button.setToolTip(
            "Usar todas las correcciones propuestas sin revisarlas una por una"
        )
        self.approve_all_button.clicked.connect(self._approve_all)
        if review.kind is ReviewKind.TRANSLATION:
            self.approve_all_button.setText("Aprobar todas las traducciones")
        elif review.kind is ReviewKind.REFINEMENT:
            if any(unit.recommended_choice is ReviewChoice.ORIGINAL for unit in review.units):
                self.approve_all_button.setText("Aplicar correcciones seguras")
                self.approve_all_button.setToolTip(
                    "Usar las propuestas conservadoras y mantener el original en los cambios "
                    "marcados para revisión especial"
                )
            else:
                self.approve_all_button.setText("Aprobar todas las correcciones")
        else:
            self.approve_all_button.hide()
        footer.addWidget(self.approve_all_button, 0, 2)
        self.save_later_button = QPushButton("Guardar y continuar después", self)
        self.save_later_button.setAccessibleName("Guardar y continuar después")
        self.save_later_button.clicked.connect(self.reject)
        footer.addWidget(self.save_later_button, 0, 3)
        self.next_button = QPushButton("Siguiente", self)
        self.next_button.setAccessibleName("Siguiente revisión o aplicar todos los cambios")
        self.next_button.setObjectName("primaryAction")
        self.next_button.clicked.connect(self._next)
        footer.addWidget(self.next_button, 0, 4)
        layout.addLayout(footer)

        self._shortcuts = (
            self._shortcut("Alt+O", self._choose_original),
            self._shortcut("Alt+P", self._choose_proposed),
            self._shortcut("Ctrl+Return", self._next),
        )
        self.original_pane.selector.setToolTip("Conservar original (Alt+O)")
        self.proposed_pane.selector.setToolTip("Usar esta versión (Alt+P)")
        self.next_button.setToolTip("Guardar esta decisión y continuar (Ctrl+Intro)")

        self._compact = False
        self._load_unit()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.set_compact_mode(event.size().width() <= BREAKPOINTS.compact)

    def set_compact_mode(self, compact: bool) -> None:
        if compact == self._compact:
            return
        self._compact = compact
        for widget in (
            self.previous_button,
            self.approve_all_button,
            self.save_later_button,
            self.next_button,
        ):
            self.footer_layout.removeWidget(widget)
        if compact:
            self.root_layout.setContentsMargins(
                SPACING.md,
                SPACING.md,
                SPACING.md,
                SPACING.md,
            )
            self.splitter.setOrientation(Qt.Orientation.Vertical)
            self.splitter.setSizes([280, 280])
            self.footer_layout.addWidget(self.previous_button, 0, 0)
            self.footer_layout.addWidget(self.next_button, 0, 1)
            self.footer_layout.addWidget(self.approve_all_button, 1, 0, 1, 2)
            self.footer_layout.addWidget(self.save_later_button, 2, 0, 1, 2)
            self.original_pane.set_compact_mode(True)
            self.proposed_pane.set_compact_mode(True)
        else:
            self.root_layout.setContentsMargins(24, 20, 24, 20)
            self.splitter.setOrientation(Qt.Orientation.Horizontal)
            self.splitter.setSizes([600, 600])
            self.footer_layout.addWidget(self.previous_button, 0, 0)
            self.footer_layout.setColumnStretch(1, 1)
            self.footer_layout.addWidget(self.approve_all_button, 0, 2)
            self.footer_layout.addWidget(self.save_later_button, 0, 3)
            self.footer_layout.addWidget(self.next_button, 0, 4)
            self.original_pane.set_compact_mode(False)
            self.proposed_pane.set_compact_mode(False)
        self._refresh_compact_labels()

    def _refresh_compact_labels(self) -> None:
        if not self._compact:
            return
        if not self.approve_all_button.isHidden():
            self.approve_all_button.setText("Aplicar seguras")
        self.save_later_button.setText("Guardar para después")
        if self._review.remaining_count <= 1:
            self.next_button.setText("Aplicar")

    @property
    def review(self) -> ReviewSession:
        return self._review

    def _unit(self) -> ReviewUnit:
        return self._review.units[self._index]

    def _load_unit(self) -> None:
        unit = self._unit()
        summary_parts = [
            f"Caso {self._index + 1} de {len(self._review.units)}",
            _SEVERITY_LABELS[unit.severity],
        ]
        if unit.label:
            summary_parts.append(unit.label)
        self.unit_summary.setText(" · ".join(summary_parts))
        pending = self._review.remaining_count
        priority = self._review.priority_remaining_count
        priority_text = (
            f" · {priority} de prioridad alta"
            if priority
            else " · sin casos de prioridad alta pendientes"
        )
        self.priority_summary.setText(
            f"{pending} {'decisión pendiente' if pending == 1 else 'decisiones pendientes'}"
            f"{priority_text}"
        )
        self.priority_summary.setAccessibleName(
            f"Estado de la revisión. {self.priority_summary.text()}"
        )
        self.unit_warning.setText(unit.warning or "")
        self.unit_warning.setVisible(bool(unit.warning))
        self.progress_indicator.set_progress(self._review.resolved_count)
        self.previous_button.setEnabled(self._index > 0)
        self.next_button.setText(
            "Aplicar todos los cambios" if self._review.remaining_count <= 1 else "Siguiente"
        )
        self._refresh_compact_labels()
        self.original_pane.set_payload(
            self._artifacts.read(self._review.job_id, unit.original_artifact_id)
        )
        proposed_id = unit.edited_artifact_id or unit.proposed_artifact_id
        proposed = (
            self._artifacts.read(self._review.job_id, proposed_id)
            if proposed_id is not None
            else self._artifacts.read(self._review.job_id, unit.original_artifact_id)
        )
        self.proposed_pane.set_payload(proposed)
        self.original_pane.selector.setEnabled(unit.original_selectable)
        if unit.choice is ReviewChoice.ORIGINAL:
            self.original_pane.selector.setChecked(True)
        elif unit.choice in {ReviewChoice.PROPOSED, ReviewChoice.EDITED}:
            self.proposed_pane.selector.setChecked(True)
        else:
            self.selection_group.setExclusive(False)
            self.original_pane.selector.setChecked(False)
            self.proposed_pane.selector.setChecked(False)
            self.selection_group.setExclusive(True)
            if unit.recommended_choice is ReviewChoice.ORIGINAL and unit.original_selectable:
                self.original_pane.selector.setChecked(True)
            else:
                self.proposed_pane.selector.setChecked(True)

    def _save_unit(self) -> bool:
        unit = self._unit()
        if self.original_pane.selector.isChecked():
            self._review = self._review.decide(unit.id, ReviewChoice.ORIGINAL)
            return True
        if not self.proposed_pane.selector.isChecked():
            QMessageBox.information(
                self,
                "Elige una versión",
                "Marca el original o la versión corregida para continuar.",
            )
            return False
        proposed_text = self.proposed_pane.text()
        original_proposal = (
            self._artifacts.read_text(self._review.job_id, unit.proposed_artifact_id)
            if unit.proposed_artifact_id is not None
            else ""
        )
        if proposed_text == original_proposal:
            self._review = self._review.decide(unit.id, ReviewChoice.PROPOSED)
            return True
        if unit.edited_artifact_id is not None and proposed_text == self._artifacts.read_text(
            self._review.job_id, unit.edited_artifact_id
        ):
            self._review = self._review.decide(
                unit.id,
                ReviewChoice.EDITED,
                edited_artifact_id=unit.edited_artifact_id,
            )
            return True
        edited = self._artifacts.put_text(
            job_id=self._review.job_id,
            text=proposed_text,
            media_type="text/markdown; charset=utf-8",
        )
        self._review = self._review.decide(
            unit.id,
            ReviewChoice.EDITED,
            edited_artifact_id=edited.id,
        )
        return True

    def _proposal_edited(self) -> None:
        if self.proposed_pane.editor.hasFocus():
            self.proposed_pane.selector.setChecked(True)

    def _previous(self) -> None:
        if not self._save_unit():
            return
        self._manual_navigation = True
        self._index -= 1
        self._load_unit()

    def _next(self) -> None:
        if not self._save_unit():
            return
        next_index = self._review.next_unresolved_index(self._index)
        if next_index is not None:
            self._index = next_index
            self._load_unit()
            return
        if self._manual_navigation and self._index < len(self._review.units) - 1:
            self._index += 1
            self._load_unit()
            return
        self.accept()

    def _shortcut(self, sequence: str, callback: Callable[[], None]) -> QShortcut:
        shortcut = QShortcut(QKeySequence(sequence), self)
        shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.activated.connect(callback)
        return shortcut

    def _choose_original(self) -> None:
        if self.original_pane.selector.isEnabled():
            self.original_pane.selector.setChecked(True)

    def _choose_proposed(self) -> None:
        if self.proposed_pane.selector.isEnabled():
            self.proposed_pane.selector.setChecked(True)

    def _approve_all(self) -> None:
        """Apply conservative proposals without overriding high-risk originals."""

        review = self._review
        for unit in review.units:
            if unit.recommended_choice is ReviewChoice.ORIGINAL and unit.original_selectable:
                review = review.decide(unit.id, ReviewChoice.ORIGINAL)
            elif unit.proposed_artifact_id is not None:
                review = review.decide(unit.id, ReviewChoice.PROPOSED)
            elif unit.original_selectable:
                review = review.decide(unit.id, ReviewChoice.ORIGINAL)
            else:
                QMessageBox.warning(
                    self,
                    "No se pueden aprobar todas",
                    "Una de las revisiones no contiene una propuesta utilizable.",
                )
                return
        self._review = review
        self.accept()


class _ReviewPane(QFrame):
    def __init__(
        self,
        title: str,
        *,
        editable: bool,
        selection_text: str,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("reviewPane")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(9)
        top = QGridLayout()
        self.top_layout = top
        self.heading = QLabel(title, self)
        self.heading.setObjectName("reviewPaneTitle")
        top.addWidget(self.heading, 0, 0)
        top.setColumnStretch(1, 1)
        self.selector = QRadioButton(selection_text, self)
        self.selector.setMinimumWidth(self.selector.sizeHint().width() + 18)
        top.addWidget(self.selector, 0, 2)
        layout.addLayout(top)
        self.editor = QPlainTextEdit(self)
        self.editor.setReadOnly(not editable)
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.image = QLabel(self)
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setVisible(False)
        self.image_scroll = QScrollArea(self)
        self.image_scroll.setWidgetResizable(True)
        self.image_scroll.setWidget(self.image)
        self.image_scroll.setVisible(False)
        layout.addWidget(self.editor, 1)
        layout.addWidget(self.image_scroll, 1)
        actions = QGridLayout()
        self.actions_layout = actions
        actions.setColumnStretch(0, 1)
        self.locate_button = QPushButton("Ir al inicio", self)
        self.locate_button.setAccessibleName(f"Volver a {title.lower()}")
        self.locate_button.setToolTip(f"Volver a {title.lower()}")
        self.locate_button.clicked.connect(self._locate)
        actions.addWidget(self.locate_button, 0, 1)
        self.restore_button: QPushButton | None
        if editable:
            self.restore_button = QPushButton("Restaurar propuesta", self)
            self.restore_button.setAccessibleName("Restaurar propuesta")
            self.restore_button.setToolTip("Restaurar propuesta")
            self.restore_button.clicked.connect(self._restore)
            actions.addWidget(self.restore_button, 0, 2)
        else:
            self.restore_button = None
        layout.addLayout(actions)
        self._payload = b""
        self._initial_text = ""

    def set_compact_mode(self, compact: bool) -> None:
        for widget in (self.heading, self.selector):
            self.top_layout.removeWidget(widget)
        self.actions_layout.removeWidget(self.locate_button)
        if self.restore_button is not None:
            self.actions_layout.removeWidget(self.restore_button)
        if compact:
            self.selector.setMinimumWidth(0)
            self.top_layout.addWidget(self.heading, 0, 0)
            self.top_layout.addWidget(self.selector, 1, 0)
            self.actions_layout.addWidget(self.locate_button, 0, 0)
            if self.restore_button is not None:
                self.actions_layout.addWidget(self.restore_button, 1, 0)
        else:
            self.selector.setMinimumWidth(self.selector.sizeHint().width() + 18)
            self.top_layout.addWidget(self.heading, 0, 0)
            self.top_layout.setColumnStretch(1, 1)
            self.top_layout.addWidget(self.selector, 0, 2)
            self.actions_layout.setColumnStretch(0, 1)
            self.actions_layout.addWidget(self.locate_button, 0, 1)
            if self.restore_button is not None:
                self.actions_layout.addWidget(self.restore_button, 0, 2)

    def set_payload(self, payload: bytes) -> None:
        self._payload = payload
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            pixmap = QPixmap()
            if pixmap.loadFromData(payload):
                self.editor.setVisible(False)
                self.image_scroll.setVisible(True)
                self.image.setPixmap(pixmap)
                self._initial_text = ""
                return
            text = "Este elemento no puede mostrarse, pero se conservará sin cambios."
        self.image_scroll.setVisible(False)
        self.editor.setVisible(True)
        self._initial_text = text
        self.editor.blockSignals(True)
        self.editor.setPlainText(text)
        self.editor.blockSignals(False)
        self._locate()

    def text(self) -> str:
        return self.editor.toPlainText()

    def _locate(self) -> None:
        if self.editor.isVisible():
            cursor = self.editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            self.editor.setTextCursor(cursor)
            self.editor.ensureCursorVisible()
        else:
            self.image_scroll.verticalScrollBar().setValue(0)
            self.image_scroll.horizontalScrollBar().setValue(0)

    def _restore(self) -> None:
        self.editor.setPlainText(self._initial_text)
        self.selector.setChecked(True)
        self._locate()


class _ReviewProgressIndicator(QWidget):
    """Weighted overview of every real review unit in the document."""

    def __init__(
        self,
        phases: tuple[_PhaseWeight, ...],
        current: ReviewKind,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self._phases = phases
        self._current = current
        self._resolved_count = 0
        self.setMinimumHeight(62)
        self.setAccessibleName("Progreso de revisión")

    def set_progress(self, resolved_count: int) -> None:
        current_count = next(
            phase.unit_count for phase in self._phases if phase.kind is self._current
        )
        self._resolved_count = max(0, min(resolved_count, current_count))
        total = sum(phase.unit_count for phase in self._phases)
        current_index = next(
            index for index, candidate in enumerate(self._phases) if candidate.kind is self._current
        )
        completed_before = sum(phase.unit_count for phase in self._phases[:current_index])
        self.setAccessibleDescription(
            f"{completed_before + self._resolved_count} de {total} revisiones resueltas."
        )
        self.update()

    @property
    def phase_fractions(self) -> tuple[tuple[ReviewKind, float], ...]:
        total = sum(phase.unit_count for phase in self._phases)
        return tuple((phase.kind, phase.unit_count / total) for phase in self._phases)

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        current_index = next(
            index for index, phase in enumerate(self._phases) if phase.kind is self._current
        )
        total_units = sum(phase.unit_count for phase in self._phases)
        completed_before = sum(phase.unit_count for phase in self._phases[:current_index])
        completed_units = completed_before + self._resolved_count
        percent = round(100 * completed_units / total_units)
        painter.setPen(QColor(COLORS.text_primary))
        progress_label = f"Progreso de revisión · {percent} %"
        painter.drawText(
            QRectF(0, 0, self.width(), 22),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            progress_label,
        )

        legend_x = self.width()
        metrics = painter.fontMetrics()
        legend_width = sum(
            metrics.horizontalAdvance(f"{_PHASE_LABELS[phase.kind]} {phase.unit_count}") + 27
            for phase in self._phases
        )
        phases_to_draw = (
            self._phases
            if legend_width <= self.width() - metrics.horizontalAdvance(progress_label) - SPACING.lg
            else ()
        )
        for phase in reversed(phases_to_draw):
            label = f"{_PHASE_LABELS[phase.kind]} {phase.unit_count}"
            label_width = metrics.horizontalAdvance(label)
            legend_x -= label_width
            painter.setPen(QColor(COLORS.text_muted))
            painter.drawText(
                QRectF(legend_x, 0, label_width, 22),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                label,
            )
            legend_x -= 13
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(_phase_color(phase.kind)))
            painter.drawEllipse(QRectF(legend_x, 8, 6, 6))
            legend_x -= 14

        gap = 4.0
        available = max(1.0, self.width() - gap * (len(self._phases) - 1))
        x = 0.0
        painter.setPen(Qt.PenStyle.NoPen)
        for index, phase in enumerate(self._phases):
            width = available * phase.unit_count / total_units
            segment = QRectF(x, 38, width, 9)
            painter.setBrush(QColor(COLORS.progress_track))
            painter.drawRoundedRect(segment, 4.5, 4.5)
            if index < current_index:
                fill_fraction = 1.0
            elif index == current_index:
                fill_fraction = self._resolved_count / phase.unit_count
            else:
                fill_fraction = 0.0
            if fill_fraction:
                painter.setBrush(QColor(_phase_color(phase.kind)))
                fill = QRectF(segment.x(), segment.y(), segment.width() * fill_fraction, 9)
                painter.drawRoundedRect(fill, 4.5, 4.5)
            x += width + gap
        painter.end()


def _normalize_phase_plan(
    phases: tuple[ReviewKind | tuple[ReviewKind, int], ...],
    *,
    current_kind: ReviewKind,
    current_count: int,
) -> tuple[_PhaseWeight, ...]:
    ordered: list[_PhaseWeight] = []
    for phase in phases:
        kind, count = phase if isinstance(phase, tuple) else (phase, 1)
        normalized_count = current_count if kind is current_kind else max(1, count)
        existing = next((item for item in ordered if item.kind is kind), None)
        if existing is None:
            ordered.append(_PhaseWeight(kind, normalized_count))
            continue
        index = ordered.index(existing)
        ordered[index] = _PhaseWeight(kind, existing.unit_count + normalized_count)
    if not any(item.kind is current_kind for item in ordered):
        ordered.append(_PhaseWeight(current_kind, current_count))
    return tuple(ordered)
