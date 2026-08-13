"""Focused two-pane review for one document phase."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QRectF, Qt, QTimer
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
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from parsezen.application.artifact_repository import ArtifactRepository
from parsezen.domain.reviews import (
    ReviewChoice,
    ReviewKind,
    ReviewSession,
    ReviewSeverity,
    ReviewUnit,
)
from parsezen.presentation.design_system import BREAKPOINTS, COLORS, SPACING, back_icon
from parsezen.review_projection import ReviewProjection, project_review_text
from parsezen.revision import markdown_outline_tree

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
_PHASE_PANE_LABELS = {
    ReviewKind.OCR: ("Página original · Solo contexto", "Texto reconocido"),
    ReviewKind.CONVERSION_WARNING: ("Página original · Solo contexto", "Conversión propuesta"),
    ReviewKind.TRANSLATION: ("Extracto original · Contexto", "Resultado actual · Editable"),
    ReviewKind.REFINEMENT: ("Versión actual", "Corrección propuesta"),
    ReviewKind.STRUCTURE: ("Versión actual", "Propuesta de estructura"),
}
_PHASE_FINAL_LABELS = {
    ReviewKind.OCR: "Aplicar OCR revisado",
    ReviewKind.CONVERSION_WARNING: "Aplicar conversión revisada",
    ReviewKind.TRANSLATION: "Confirmar traducción",
    ReviewKind.REFINEMENT: "Aplicar correcciones",
    ReviewKind.STRUCTURE: "Aplicar estructura",
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


def _recommendation_text(unit: ReviewUnit) -> str:
    if unit.recommended_choice is ReviewChoice.ORIGINAL and unit.original_selectable:
        return "Recomendación provisional: conservar el resultado actual; necesita confirmación"
    if unit.recommended_choice is ReviewChoice.PROPOSED:
        return "Recomendación provisional: usar la propuesta; necesita confirmación"
    return "Necesita confirmación"


def _phase_instruction(
    kind: ReviewKind,
    *,
    translation_follows_ocr: bool = False,
) -> str:
    if kind is ReviewKind.OCR and translation_follows_ocr:
        return (
            "El texto ya aparece en el idioma del resultado. Corrígelo tal como debe quedar; "
            "no necesitas traducirlo de nuevo. La página original es solo una referencia."
        )
    if kind is ReviewKind.TRANSLATION:
        return (
            "Parsezen ha detectado un posible problema en este fragmento. "
            "Corrige el resultado en el idioma solicitado o confírmalo sin cambios "
            "solo si es correcto."
        )
    if kind is ReviewKind.OCR:
        return "Corrige el texto reconocido o indica que no hay texto que añadir."
    return "Elige una versión y confirma la decisión; la recomendación es provisional."


@dataclass(frozen=True, slots=True)
class _PhaseWeight:
    kind: ReviewKind
    unit_count: int


class PhaseReviewDialog(QDialog):
    """Review only the units belonging to one phase and persist manual edits."""

    def __init__(
        self,
        review: ReviewSession,
        artifacts: ArtifactRepository,
        *,
        phase_plan: tuple[ReviewKind | tuple[ReviewKind, int], ...] = (),
        translation_follows_ocr: bool = False,
        linguistic_review_context: str | None = None,
        structure_outline: tuple[str, str] | None = None,
        previous_phase_callback: Callable[[], bool] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if not review.units:
            raise ValueError("A review must contain at least one unit.")
        self._review = review
        self._artifacts = artifacts
        self._translation_follows_ocr = translation_follows_ocr
        self._previous_phase_callback = previous_phase_callback
        self._context_only_original = review.kind in {
            ReviewKind.OCR,
            ReviewKind.TRANSLATION,
        }
        self._phase_plan = _normalize_phase_plan(
            phase_plan or (review.kind,),
            current_kind=review.kind,
            current_count=len(review.units),
        )
        self._index = review.first_unresolved_index() or 0
        self._manual_navigation = review.complete
        self._loading_unit = False
        self._active_case_dirty = False
        self._focus_timer = QTimer(self)
        self._focus_timer.setSingleShot(True)
        self._focus_timer.timeout.connect(self._focus_pending_decision_now)
        self.setWindowTitle("Revisión del documento · Parsezen")
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
            total_units=len(review.units),
        )
        layout.addWidget(self.progress_indicator)
        self.case_summary = QLabel("", self)
        self.case_summary.setObjectName("reviewCaseSummary")
        self.case_summary.setWordWrap(True)
        self.case_summary.setMaximumHeight(48)
        self.case_summary.setAccessibleName("Síntesis del caso")
        layout.addWidget(self.case_summary)
        instruction = _phase_instruction(
            review.kind,
            translation_follows_ocr=translation_follows_ocr,
        )
        if review.kind is ReviewKind.TRANSLATION and linguistic_review_context:
            instruction = f"{instruction} {linguistic_review_context}"
        self.instruction_label = QLabel(instruction, self)
        self.instruction_label.setObjectName("reviewInstruction")
        self.instruction_label.setWordWrap(True)
        layout.addWidget(self.instruction_label)
        self.priority_summary = QLabel("", self)
        self.priority_summary.setObjectName("reviewPrioritySummary")
        self.priority_summary.setWordWrap(True)
        self.unit_summary = QLabel("", self)
        self.unit_summary.setObjectName("reviewUnitSummary")
        self.unit_summary.setWordWrap(True)
        self.priority_summary.hide()
        self.unit_summary.hide()
        self.unit_warning = QLabel("", self)
        self.unit_warning.setObjectName("reviewWarning")
        self.unit_warning.setWordWrap(True)
        self.unit_warning.hide()
        layout.addWidget(self.unit_warning)

        self.outline_comparison = QFrame(self)
        self.outline_comparison.setObjectName("reviewOutlineComparison")
        outline_layout = QGridLayout(self.outline_comparison)
        self.outline_layout = outline_layout
        outline_layout.setContentsMargins(SPACING.sm, SPACING.sm, SPACING.sm, SPACING.sm)
        outline_layout.setHorizontalSpacing(SPACING.md)
        outline_layout.setVerticalSpacing(SPACING.xs)
        self.original_outline_label = QLabel("Árbol actual", self.outline_comparison)
        self.proposed_outline_label = QLabel("Árbol propuesto", self.outline_comparison)
        outline_layout.addWidget(self.original_outline_label, 0, 0)
        outline_layout.addWidget(self.proposed_outline_label, 0, 1)
        self.original_outline = QPlainTextEdit(self.outline_comparison)
        self.proposed_outline = QPlainTextEdit(self.outline_comparison)
        for outline in (self.original_outline, self.proposed_outline):
            outline.setReadOnly(True)
            outline.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
            outline.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            outline.setMaximumHeight(150)
            outline.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if review.kind is ReviewKind.STRUCTURE and structure_outline is not None:
            self.original_outline.setPlainText(markdown_outline_tree(structure_outline[0]))
            self.proposed_outline.setPlainText(markdown_outline_tree(structure_outline[1]))
        else:
            self.outline_comparison.hide()
        outline_layout.addWidget(self.original_outline, 1, 0)
        outline_layout.addWidget(self.proposed_outline, 1, 1)
        layout.addWidget(self.outline_comparison)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter = splitter
        splitter.setAccessibleName("Comparación de original y propuesta")
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(1)
        original_title, proposed_title = _PHASE_PANE_LABELS[review.kind]
        self.original_pane = _ReviewPane(
            original_title,
            editable=False,
            selection_text="Conservar esta versión",
            parent=splitter,
        )
        self.proposed_pane = _ReviewPane(
            proposed_title,
            editable=True,
            selection_text=(
                "Confirmar sin cambios"
                if review.kind is ReviewKind.TRANSLATION
                else "Usar esta versión"
            ),
            restore_text=(
                "Restaurar resultado"
                if review.kind is ReviewKind.TRANSLATION
                else "Restaurar propuesta"
            ),
            allow_no_text=review.kind is ReviewKind.OCR,
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
        if self.proposed_pane.no_text_button is not None:
            self.selection_group.addButton(self.proposed_pane.no_text_button)
        self.original_pane.selector.toggled.connect(self._selection_changed)
        self.proposed_pane.selector.toggled.connect(self._selection_changed)
        if self.proposed_pane.no_text_button is not None:
            self.proposed_pane.no_text_button.toggled.connect(self._selection_changed)
        self.proposed_pane.editor.textChanged.connect(self._proposal_edited)
        if self._context_only_original:
            self.original_pane.selector.hide()

        footer = QGridLayout()
        self.footer_layout = footer
        self.previous_button = QPushButton("Anterior", self)
        self.previous_button.setIcon(back_icon())
        self.previous_button.setToolTip("Corrección anterior")
        self.previous_button.setAccessibleName("Corrección anterior")
        self.previous_button.clicked.connect(self._previous)
        footer.addWidget(self.previous_button, 0, 0)
        footer.setColumnStretch(1, 1)
        self.approve_all_button = QPushButton("Aplicar recomendaciones seguras", self)
        self.approve_all_button.setAccessibleName("Aplicar recomendaciones seguras")
        self.approve_all_button.setToolTip(
            "Aplicar recomendaciones seguras y dejar visibles las incidencias importantes"
        )
        self.approve_all_button.clicked.connect(self._approve_all)
        if review.kind is not ReviewKind.REFINEMENT:
            self.approve_all_button.hide()
        footer.addWidget(self.approve_all_button, 0, 2)
        self.save_later_button = QPushButton("Guardar y salir", self)
        self.save_later_button.setAccessibleName("Guardar y salir")
        self.save_later_button.setAccessibleDescription(
            "Guarda la edición o decisión actual para poder reanudar esta revisión después."
        )
        self.save_later_button.setToolTip(
            "Guardar la edición o decisión actual y reanudar la revisión después"
        )
        self.save_later_button.clicked.connect(self.reject)
        footer.addWidget(self.save_later_button, 0, 3)
        self.next_button = QPushButton("Confirmar y siguiente", self)
        self.next_button.setAccessibleName("Confirmar y siguiente")
        self.next_button.setObjectName("primaryAction")
        self.next_button.clicked.connect(self._next)
        footer.addWidget(self.next_button, 0, 4)
        layout.addLayout(footer)

        self._shortcuts = (
            self._shortcut("Alt+O", self._choose_original),
            self._shortcut("Alt+P", self._choose_proposed),
            self._shortcut("Alt+N", self._choose_no_text),
            self._shortcut("Ctrl+Return", self._next),
        )
        self.original_pane.selector.setToolTip("Conservar esta versión (Alt+O)")
        self.proposed_pane.selector.setToolTip(
            "Confirmar sin cambios (Alt+P)"
            if review.kind is ReviewKind.TRANSLATION
            else "Usar esta versión (Alt+P)"
        )
        if self.proposed_pane.no_text_button is not None:
            self.proposed_pane.no_text_button.setToolTip("No hay texto que añadir (Alt+N)")
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
            self._layout_outline_comparison(compact=True)
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
            self._layout_outline_comparison(compact=False)
        self._refresh_compact_labels()

    def _layout_outline_comparison(self, *, compact: bool) -> None:
        if self.outline_comparison.isHidden():
            return
        for widget in (
            self.original_outline_label,
            self.proposed_outline_label,
            self.original_outline,
            self.proposed_outline,
        ):
            self.outline_layout.removeWidget(widget)
        if compact:
            self.original_outline.setMaximumHeight(100)
            self.proposed_outline.setMaximumHeight(100)
            self.outline_layout.addWidget(self.original_outline_label, 0, 0)
            self.outline_layout.addWidget(self.original_outline, 1, 0)
            self.outline_layout.addWidget(self.proposed_outline_label, 2, 0)
            self.outline_layout.addWidget(self.proposed_outline, 3, 0)
        else:
            self.original_outline.setMaximumHeight(150)
            self.proposed_outline.setMaximumHeight(150)
            self.outline_layout.addWidget(self.original_outline_label, 0, 0)
            self.outline_layout.addWidget(self.proposed_outline_label, 0, 1)
            self.outline_layout.addWidget(self.original_outline, 1, 0)
            self.outline_layout.addWidget(self.proposed_outline, 1, 1)

    def _refresh_compact_labels(self) -> None:
        if not self._compact:
            return
        if not self.approve_all_button.isHidden():
            self.approve_all_button.setText("Aplicar seguras")
        self.save_later_button.setText("Guardar y salir")
        if self._next_case_index() is None:
            self.next_button.setText(_PHASE_FINAL_LABELS[self._review.kind])

    @property
    def internal_page_title(self) -> str:
        return "Revisión del documento"

    @property
    def review(self) -> ReviewSession:
        return self._review

    def reject(self) -> None:  # noqa: D401
        """Save an interacted active case before the shared workflow closes."""

        if not self._save_active_case_if_dirty():
            return
        self._focus_timer.stop()
        super().reject()

    def accept(self) -> None:  # noqa: D401
        """Stop deferred focus work before the dialog is closed."""

        self._focus_timer.stop()
        super().accept()

    def _unit(self) -> ReviewUnit:
        return self._review.units[self._index]

    def _load_unit(self) -> None:
        unit = self._unit()
        self._loading_unit = True
        self._active_case_dirty = False
        summary_parts = [
            f"Caso {self._index + 1} de {len(self._review.units)}",
            _SEVERITY_LABELS[unit.severity],
        ]
        if unit.label:
            summary_parts.append(unit.label)
        if not unit.resolved:
            summary_parts.append(_recommendation_text(unit))
        pending = self._review.remaining_count
        priority = self._review.priority_remaining_count
        recommendation = (
            _recommendation_text(unit).replace("Recomendación provisional: ", "Provisional · ")
            if not unit.resolved
            else "Decisión confirmada"
        )
        summary = [
            f"{_PHASE_LABELS[self._review.kind]} · Caso {self._index + 1}/"
            f"{len(self._review.units)} · {unit.label or 'Contenido'}",
            f"Pendientes {pending}",
        ]
        if priority:
            summary.append(f"{priority} importantes")
        summary.append(recommendation)
        self.case_summary.setText(" · ".join(summary))
        self.unit_summary.setText(" · ".join(summary_parts))
        priority_text = f" · {priority} importantes" if priority else ""
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
        self.previous_button.setEnabled(
            self._index > 0 or self._previous_phase_callback is not None
        )
        has_next_case = self._next_case_index() is not None
        self.next_button.setText(
            "Confirmar y siguiente" if has_next_case else _PHASE_FINAL_LABELS[self._review.kind]
        )
        self.next_button.setAccessibleName(self.next_button.text())
        self.next_button.setToolTip(
            "Guardar esta decisión y continuar (Ctrl+Intro)"
            if has_next_case
            else "Guardar esta decisión y terminar la revisión (Ctrl+Intro)"
        )
        self._refresh_compact_labels()
        self.original_pane.set_payload(
            self._artifacts.read(self._review.job_id, unit.original_artifact_id),
            project_private=True,
        )
        proposed_id = unit.edited_artifact_id or unit.proposed_artifact_id
        proposed = (
            self._artifacts.read(self._review.job_id, proposed_id)
            if proposed_id is not None
            else self._artifacts.read(self._review.job_id, unit.original_artifact_id)
        )
        self.proposed_pane.set_payload(
            proposed,
            project_private=True,
        )
        self.original_pane.selector.setEnabled(unit.original_selectable)
        self.proposed_pane.selector.setEnabled(unit.proposed_selectable)
        if self.proposed_pane.no_text_button is not None:
            self.proposed_pane.no_text_button.setEnabled(
                unit.proposed_selectable and unit.proposed_artifact_id is not None
            )
        if unit.original_selectable:
            self.original_pane.selector.setAccessibleDescription(
                "Puedes conservar el resultado actual como reemplazo."
            )
        else:
            self.original_pane.selector.setAccessibleDescription(
                "Solo contexto de referencia; no se puede conservar como reemplazo."
            )
            self.original_pane.selector.setToolTip(
                "Solo contexto; no se puede usar para reemplazar el resultado."
            )
        buttons = [self.original_pane.selector, self.proposed_pane.selector]
        if self.proposed_pane.no_text_button is not None:
            buttons.append(self.proposed_pane.no_text_button)
        self.selection_group.setExclusive(False)
        for button in buttons:
            button.setChecked(False)
        self.selection_group.setExclusive(True)
        if unit.choice is ReviewChoice.ORIGINAL and unit.original_selectable:
            self.original_pane.selector.setChecked(True)
        elif unit.choice in {ReviewChoice.PROPOSED, ReviewChoice.EDITED}:
            self.proposed_pane.selector.setChecked(True)
        elif unit.choice is ReviewChoice.NO_TEXT and self.proposed_pane.no_text_button is not None:
            self.proposed_pane.no_text_button.setChecked(True)
        self._refresh_pane_selection()
        self._loading_unit = False
        self._focus_pending_decision()

    def _save_unit(self) -> bool:
        unit = self._unit()
        if (
            self.proposed_pane.no_text_button is not None
            and self.proposed_pane.no_text_button.isChecked()
        ):
            self._review = self._review.decide(unit.id, ReviewChoice.NO_TEXT)
            self._active_case_dirty = False
            return True
        if self.original_pane.selector.isChecked():
            if not unit.original_selectable:
                QMessageBox.information(
                    self,
                    "Este contenido es solo de referencia",
                    "El texto de origen sirve para comparar y no puede sustituir a la traducción.",
                )
                return False
            self._review = self._review.decide(unit.id, ReviewChoice.ORIGINAL)
            self._active_case_dirty = False
            return True
        if not self.proposed_pane.selector.isChecked():
            QMessageBox.information(
                self,
                "Elige una versión",
                "Elige una versión o indica que no hay texto que añadir para continuar.",
            )
            return False
        if not unit.proposed_selectable:
            QMessageBox.information(
                self,
                "Propuesta no disponible",
                "Esta propuesta no se puede seleccionar y se conservará el original.",
            )
            return False
        try:
            proposed_text = self.proposed_pane.restored_text()
        except ValueError:
            QMessageBox.warning(
                self,
                "Edición no válida",
                "La edición contiene sintaxis privada y no se puede guardar.",
            )
            return False
        original_proposal = (
            self._artifacts.read_text(self._review.job_id, unit.proposed_artifact_id)
            if unit.proposed_artifact_id is not None
            else ""
        )
        if proposed_text == original_proposal:
            self._review = self._review.decide(unit.id, ReviewChoice.PROPOSED)
            self._active_case_dirty = False
            return True
        if unit.edited_artifact_id is not None and proposed_text == self._artifacts.read_text(
            self._review.job_id, unit.edited_artifact_id
        ):
            self._review = self._review.decide(
                unit.id,
                ReviewChoice.EDITED,
                edited_artifact_id=unit.edited_artifact_id,
            )
            self._active_case_dirty = False
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
        self._active_case_dirty = False
        return True

    def _save_active_case_if_dirty(self) -> bool:
        if not self._active_case_dirty:
            return True
        return self._save_unit()

    def _next_case_index(self) -> int | None:
        next_unresolved = self._review.next_unresolved_index(self._index)
        if next_unresolved is not None:
            return next_unresolved
        if self._manual_navigation and self._index < len(self._review.units) - 1:
            return self._index + 1
        return None

    def _focus_pending_decision(self) -> None:
        self._focus_timer.start(0)

    def _focus_pending_decision_now(self) -> None:
        if self._unit().resolved:
            return
        target = (
            self.original_pane.selector
            if self.original_pane.selector.isVisible()
            and self.original_pane.selector.isEnabled()
            and self.original_pane.selector.isChecked()
            else (
                self.proposed_pane.no_text_button
                if self.proposed_pane.no_text_button is not None
                and self.proposed_pane.no_text_button.isChecked()
                else self.proposed_pane.selector
            )
        )
        target.setFocus()

    def _selection_changed(self, _checked: bool) -> None:
        self._refresh_pane_selection()
        if not self._loading_unit:
            self._active_case_dirty = True

    def _refresh_pane_selection(self) -> None:
        self.original_pane.set_selected(self.original_pane.selector.isChecked())
        self.proposed_pane.set_selected(
            self.proposed_pane.selector.isChecked()
            or bool(
                self.proposed_pane.no_text_button is not None
                and self.proposed_pane.no_text_button.isChecked()
            )
        )

    def _proposal_edited(self) -> None:
        if not self._loading_unit:
            self._active_case_dirty = True
            self.proposed_pane.selector.setChecked(True)

    def _previous(self) -> None:
        if self._index <= 0:
            if self._previous_phase_callback is None:
                return
            if not self._save_active_case_if_dirty():
                return
            confirmation = QMessageBox.question(
                self,
                "Volver a la fase anterior",
                "Se volverá a abrir la fase anterior para editarla. Las revisiones posteriores "
                "se recalcularán con tus decisiones conservadas como punto de partida. ¿Continuar?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if confirmation == QMessageBox.StandardButton.Yes and self._previous_phase_callback():
                self.reject()
            return
        if not self._save_unit():
            return
        self._manual_navigation = True
        self._index -= 1
        self._load_unit()

    def _next(self) -> None:
        if not self._save_unit():
            return
        next_index = self._next_case_index()
        if next_index is not None:
            self._index = next_index
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

    def _choose_no_text(self) -> None:
        if (
            self.proposed_pane.no_text_button is not None
            and self.proposed_pane.no_text_button.isEnabled()
        ):
            self.proposed_pane.no_text_button.setChecked(True)

    def _approve_all(self) -> None:
        """Apply conservative proposals without overriding high-risk originals."""

        if not self._save_active_case_if_dirty():
            return
        review = self._review
        important_left = False
        for unit in review.units:
            if unit.resolved:
                continue
            if (
                review.kind is ReviewKind.TRANSLATION
                and not unit.original_selectable
                and unit.severity in {ReviewSeverity.CRITICAL, ReviewSeverity.HIGH}
            ):
                important_left = True
                continue
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
        if important_left:
            next_index = self._review.first_unresolved_index()
            if next_index is not None:
                self._index = next_index
                self._load_unit()
            QMessageBox.information(
                self,
                "Revisión pendiente",
                "Las incidencias importantes siguen visibles para confirmarlas una a una.",
            )
            return
        self.accept()


class _ReviewPane(QFrame):
    def __init__(
        self,
        title: str,
        *,
        editable: bool,
        selection_text: str,
        restore_text: str = "Restaurar propuesta",
        allow_no_text: bool = False,
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
        self.heading.setAccessibleName(title)
        top.addWidget(self.heading, 0, 0)
        top.setColumnStretch(1, 1)
        self.selector = QPushButton(selection_text, self)
        self.selector.setObjectName("reviewChoiceButton")
        self.selector.setCheckable(True)
        self.selector.setAutoDefault(False)
        self.selector.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.selector.setAccessibleName(f"{selection_text}: {title}")
        self.selector.setMinimumWidth(self.selector.sizeHint().width())
        top.addWidget(self.selector, 0, 2)
        layout.addLayout(top)
        self.editor = QPlainTextEdit(self)
        self.editor.setReadOnly(not editable)
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.editor.setAccessibleName(
            f"{title}; {'edición propuesta' if editable else 'texto de referencia'}"
        )
        self.image = QLabel(self)
        self.image.setAccessibleName(f"{title}; imagen de referencia")
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
            self.restore_button = QPushButton(restore_text, self)
            self.restore_button.setAccessibleName(restore_text)
            self.restore_button.setToolTip(restore_text)
            self.restore_button.clicked.connect(self._restore)
            actions.addWidget(self.restore_button, 0, 2)
        else:
            self.restore_button = None
        self.no_text_button: QPushButton | None = None
        if allow_no_text:
            self.no_text_button = QPushButton("No hay texto que añadir", self)
            self.no_text_button.setObjectName("reviewChoiceButton")
            self.no_text_button.setCheckable(True)
            self.no_text_button.setAutoDefault(False)
            self.no_text_button.setAccessibleName(
                "No hay texto que añadir; conservar recursos privados"
            )
            self.no_text_button.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Preferred,
            )
            actions.addWidget(self.no_text_button, 0, 3)
        layout.addLayout(actions)
        self._payload = b""
        self._initial_text = ""
        self._projection: ReviewProjection | None = None
        self.set_selected(False)

    def set_compact_mode(self, compact: bool) -> None:
        for widget in (self.heading, self.selector):
            self.top_layout.removeWidget(widget)
        self.actions_layout.removeWidget(self.locate_button)
        if self.restore_button is not None:
            self.actions_layout.removeWidget(self.restore_button)
        if self.no_text_button is not None:
            self.actions_layout.removeWidget(self.no_text_button)
        if compact:
            self.selector.setMinimumWidth(0)
            self.top_layout.addWidget(self.heading, 0, 0)
            self.top_layout.addWidget(self.selector, 1, 0)
            self.actions_layout.addWidget(self.locate_button, 0, 0)
            if self.restore_button is not None:
                self.actions_layout.addWidget(self.restore_button, 1, 0)
            if self.no_text_button is not None:
                self.actions_layout.addWidget(self.no_text_button, 2, 0)
        else:
            self.selector.setMinimumWidth(0)
            self.top_layout.addWidget(self.heading, 0, 0)
            self.top_layout.setColumnStretch(1, 1)
            self.top_layout.addWidget(self.selector, 0, 2)
            self.actions_layout.setColumnStretch(0, 1)
            self.actions_layout.addWidget(self.locate_button, 0, 1)
            if self.restore_button is not None:
                self.actions_layout.addWidget(self.restore_button, 0, 2)
            if self.no_text_button is not None:
                self.actions_layout.addWidget(self.no_text_button, 0, 3)

    def set_payload(self, payload: bytes, *, project_private: bool = False) -> None:
        self._payload = payload
        self._projection = None
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
        if project_private:
            self._projection = project_review_text(text)
            text = self._projection.visible_text
        self.image_scroll.setVisible(False)
        self.editor.setVisible(True)
        self._initial_text = text
        self.editor.blockSignals(True)
        self.editor.setPlainText(text)
        self.editor.blockSignals(False)
        self._locate()

    def text(self) -> str:
        return self.editor.toPlainText()

    def restored_text(self) -> str:
        text = self.editor.toPlainText()
        return self._projection.restore(text) if self._projection is not None else text

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)

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
    """Show current-phase and global progress using only real review units."""

    def __init__(
        self,
        phases: tuple[_PhaseWeight, ...],
        current: ReviewKind,
        parent: QWidget,
        *,
        total_units: int | None = None,
    ) -> None:
        super().__init__(parent)
        normalized = list(phases)
        current_index = next(
            (index for index, phase in enumerate(normalized) if phase.kind is current),
            None,
        )
        if current_index is None:
            normalized.append(_PhaseWeight(current, max(1, total_units or 1)))
            current_index = len(normalized) - 1
        elif total_units is not None:
            normalized[current_index] = _PhaseWeight(current, max(1, total_units))
        self._phases = tuple(phase for phase in normalized if phase.unit_count > 0)
        self._current_index = next(
            index for index, phase in enumerate(self._phases) if phase.kind is current
        )
        self._current_count = self._phases[self._current_index].unit_count
        self._previous_units = sum(
            phase.unit_count for phase in self._phases[: self._current_index]
        )
        self._total_units = max(1, sum(phase.unit_count for phase in self._phases))
        self._current = current
        self._resolved_count = 0
        self.setMinimumHeight(62)
        self.setAccessibleName("Progreso global de revisión")

    def set_progress(self, resolved_count: int) -> None:
        self._resolved_count = max(0, min(resolved_count, self._current_count))
        global_resolved = self._previous_units + self._resolved_count
        self.setAccessibleDescription(
            f"Fase {_PHASE_LABELS[self._current]}: {self._resolved_count} de "
            f"{self._current_count} decisiones confirmadas. "
            f"Progreso global: {global_resolved} de {self._total_units}. "
            f"Quedan {self._current_count - self._resolved_count} en esta fase."
        )
        self.update()

    @property
    def phase_fractions(self) -> tuple[tuple[ReviewKind, float], ...]:
        current_fraction = self._resolved_count / self._current_count
        return tuple(
            (
                phase.kind,
                1.0
                if index < self._current_index
                else current_fraction
                if index == self._current_index
                else 0.0,
            )
            for index, phase in enumerate(self._phases)
        )

    @property
    def global_fraction(self) -> float:
        return (self._previous_units + self._resolved_count) / self._total_units

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        global_resolved = self._previous_units + self._resolved_count
        percent = round(100 * global_resolved / self._total_units)
        painter.setPen(QColor(COLORS.text_primary))
        if self.width() < 420:
            progress_label = (
                f"Global {global_resolved}/{self._total_units} · "
                f"{_PHASE_LABELS[self._current]} {self._resolved_count}/{self._current_count}"
            )
        else:
            progress_label = (
                f"Fase {_PHASE_LABELS[self._current]} · {self._resolved_count}/"
                f"{self._current_count} · Global {global_resolved}/{self._total_units} · "
                f"{percent} %"
            )
        painter.drawText(
            QRectF(0, 0, self.width(), 22),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            progress_label,
        )

        available = max(1.0, float(self.width()))
        painter.setPen(Qt.PenStyle.NoPen)
        segment = QRectF(0, 38, available, 9)
        painter.setBrush(QColor(COLORS.progress_track))
        painter.drawRoundedRect(segment, 4.5, 4.5)
        offset = 0.0
        for index, phase in enumerate(self._phases):
            width = segment.width() * phase.unit_count / self._total_units
            fraction = (
                1.0
                if index < self._current_index
                else self._resolved_count / self._current_count
                if index == self._current_index
                else 0.0
            )
            if fraction:
                painter.setBrush(QColor(_phase_color(phase.kind)))
                fill = QRectF(segment.x() + offset, segment.y(), width * fraction, 9)
                painter.drawRect(fill)
            offset += width
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
