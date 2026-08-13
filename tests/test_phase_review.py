from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QMessageBox, QSplitter

from parsezen.application.review_coordinator import apply_phase_review
from parsezen.domain.jobs import (
    DocumentFormat,
    DocumentJob,
    DocumentSource,
    JobConfiguration,
    ProcessingPlan,
)
from parsezen.domain.reviews import (
    ReviewChoice,
    ReviewKind,
    ReviewSession,
    ReviewSeverity,
    ReviewUnit,
)
from parsezen.domain.stages import StageKind, StageStatus
from parsezen.infrastructure.artifact_store import ArtifactStore
from parsezen.presentation.phase_review_dialog import PhaseReviewDialog
from parsezen.review_projection import project_review_text


def reversible(payload: bytes) -> bytes:
    return bytes(value ^ 0x5A for value in payload)


def make_review(store: ArtifactStore) -> ReviewSession:
    original = store.put_text(job_id="job", artifact_id="original", text="Texto original")
    proposed = store.put_text(job_id="job", artifact_id="proposed", text="Texto corregido")
    return ReviewSession.create(
        job_id="job",
        stage=StageKind.REFINE,
        kind=ReviewKind.REFINEMENT,
        input_artifact_id=original.id,
        input_version=1,
        units=(ReviewUnit("unit", original.id, proposed.id),),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_phase_review_keeps_manual_edit_as_encrypted_artifact(qtbot, tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    dialog = PhaseReviewDialog(make_review(store), store)
    qtbot.addWidget(dialog)

    dialog.proposed_pane.editor.setPlainText("Mi corrección")
    dialog.proposed_pane.selector.setChecked(True)
    dialog._next()

    unit = dialog.review.units[0]
    assert unit.edited_artifact_id is not None
    assert store.read_text("job", unit.edited_artifact_id) == "Mi corrección"
    raw = (tmp_path / "artifacts" / "job" / f"{unit.edited_artifact_id}.pza").read_bytes()
    assert b"Mi correcci" not in raw


def test_phase_review_saves_active_edit_on_exit_and_resumes_after_it(qtbot, tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    original = store.put_text(job_id="job", text="Original")
    first = store.put_text(job_id="job", text="Primera propuesta")
    second = store.put_text(job_id="job", text="Segunda propuesta")
    review = ReviewSession.create(
        job_id="job",
        stage=StageKind.REFINE,
        kind=ReviewKind.REFINEMENT,
        input_artifact_id=original.id,
        input_version=1,
        units=(
            ReviewUnit("first", original.id, first.id),
            ReviewUnit("second", original.id, second.id),
        ),
    )
    dialog = PhaseReviewDialog(review, store)
    qtbot.addWidget(dialog)

    dialog.proposed_pane.editor.setPlainText("Edición activa")
    dialog.reject()

    saved_unit = dialog.review.units[0]
    assert dialog.result() == dialog.DialogCode.Rejected
    assert saved_unit.choice is ReviewChoice.EDITED
    assert saved_unit.edited_artifact_id is not None
    raw = (tmp_path / "artifacts" / "job" / f"{saved_unit.edited_artifact_id}.pza").read_bytes()
    assert "Edición activa".encode() not in raw

    resumed = PhaseReviewDialog(dialog.review, store)
    qtbot.addWidget(resumed)
    assert resumed._index == 1
    assert resumed.proposed_pane.text() == "Segunda propuesta"


def test_phase_review_does_not_resolve_an_unmodified_recommendation_on_exit(
    qtbot,
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    dialog = PhaseReviewDialog(make_review(store), store)
    qtbot.addWidget(dialog)

    dialog.reject()

    assert dialog.review.units[0].choice is None


def test_phase_review_requires_an_explicit_choice_before_next(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    dialog = PhaseReviewDialog(make_review(store), store)
    qtbot.addWidget(dialog)
    messages: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda _parent, _title, text: messages.append(text),
    )

    dialog._next()

    assert dialog.review.units[0].choice is None
    assert messages == ["Elige una versión o indica que no hay texto que añadir para continuar."]


def test_translation_bulk_action_keeps_important_context_only_cases_pending(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    source = store.put_text(job_id="job", text="Texto de origen")
    high = store.put_text(job_id="job", text="Propuesta importante")
    medium = store.put_text(job_id="job", text="Propuesta segura")
    review = ReviewSession.create(
        job_id="job",
        stage=StageKind.TRANSLATE,
        kind=ReviewKind.TRANSLATION,
        input_artifact_id=source.id,
        input_version=1,
        units=(
            ReviewUnit(
                "important",
                source.id,
                high.id,
                original_selectable=False,
                recommended_choice=ReviewChoice.PROPOSED,
                severity=ReviewSeverity.HIGH,
            ),
            ReviewUnit(
                "safe",
                source.id,
                medium.id,
                original_selectable=False,
                recommended_choice=ReviewChoice.PROPOSED,
            ),
        ),
    )
    messages: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda _parent, _title, text: messages.append(text),
    )
    dialog = PhaseReviewDialog(review, store)
    qtbot.addWidget(dialog)

    assert not dialog.original_pane.selector.isEnabled()
    assert "Recomendación" in dialog.unit_summary.text()
    dialog._approve_all()

    assert dialog.result() != dialog.DialogCode.Accepted
    assert dialog._unit().id == "important"
    assert dialog.review.units[0].choice is None
    assert dialog.review.units[1].choice is ReviewChoice.PROPOSED
    assert messages


def test_translation_review_describes_the_flagged_result_without_calling_it_a_proposal(
    qtbot,
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    source = store.put_text(job_id="job", text="Original context")
    current = store.put_text(job_id="job", text="Current result")
    review = ReviewSession.create(
        job_id="job",
        stage=StageKind.TRANSLATE,
        kind=ReviewKind.TRANSLATION,
        input_artifact_id=current.id,
        input_version=1,
        units=(
            ReviewUnit(
                "flagged",
                source.id,
                current.id,
                original_selectable=False,
                severity=ReviewSeverity.HIGH,
            ),
        ),
    )

    dialog = PhaseReviewDialog(review, store)
    qtbot.addWidget(dialog)

    assert dialog.original_pane.heading.text() == "Extracto original · Contexto"
    assert dialog.proposed_pane.heading.text() == "Resultado actual · Editable"
    assert dialog.proposed_pane.selector.text() == "Confirmar sin cambios"
    assert dialog.proposed_pane.restore_button is not None
    assert dialog.proposed_pane.restore_button.text() == "Restaurar resultado"
    assert "Corrige el resultado" in dialog.instruction_label.text()
    assert "Recomendación provisional" not in dialog.case_summary.text()
    assert dialog.approve_all_button.isHidden()


def test_first_case_can_return_to_the_previous_review_phase(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    source = store.put_text(job_id="job", text="Original context")
    current = store.put_text(job_id="job", text="Current result")
    review = ReviewSession.create(
        job_id="job",
        stage=StageKind.TRANSLATE,
        kind=ReviewKind.TRANSLATION,
        input_artifact_id=current.id,
        input_version=1,
        units=(ReviewUnit("flagged", source.id, current.id, original_selectable=False),),
    )
    callbacks: list[bool] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    dialog = PhaseReviewDialog(
        review,
        store,
        previous_phase_callback=lambda: callbacks.append(True) or True,
    )
    qtbot.addWidget(dialog)
    dialog.show()

    dialog._previous()

    assert callbacks == [True]
    assert dialog.result() == dialog.DialogCode.Rejected


def test_phase_review_completes_only_its_blocked_stage(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    review = make_review(store).decide("unit", choice=ReviewChoice.PROPOSED)
    job = DocumentJob.create(
        DocumentSource(Path("book.md"), DocumentFormat.MARKDOWN, 100, 1),
        JobConfiguration(plan=ProcessingPlan.LOCAL_AI_REVIEWED),
        order=0,
        job_id="job",
    )
    prepare = (
        job.stage(StageKind.PREPARE)
        .transition(StageStatus.READY)
        .transition(StageStatus.RUNNING)
        .transition(StageStatus.COMPLETED)
    )
    refine = (
        job.stage(StageKind.REFINE)
        .transition(StageStatus.READY)
        .transition(StageStatus.RUNNING)
        .transition(StageStatus.BLOCKED_FOR_REVIEW, review_id=review.id)
    )
    job = job.replace_stage(prepare).replace_stage(refine)

    applied = apply_phase_review(job, review)

    assert applied.job.stage(StageKind.REFINE).status is StageStatus.COMPLETED
    assert applied.job.stage(StageKind.PUBLISH).status is StageStatus.READY


def test_phase_review_defaults_to_proposal_and_navigates_restores(
    qtbot,
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    first_original = store.put_text(job_id="job", text="First original")
    first_proposed = store.put_text(job_id="job", text="First proposal")
    second_original = store.put_text(job_id="job", text="Second original")
    second_proposed = store.put_text(job_id="job", text="Second proposal")
    review = ReviewSession.create(
        job_id="job",
        stage=StageKind.REFINE,
        kind=ReviewKind.REFINEMENT,
        input_artifact_id=first_original.id,
        input_version=1,
        units=(
            ReviewUnit("first", first_original.id, first_proposed.id),
            ReviewUnit("second", second_original.id, second_proposed.id),
        ),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    dialog = PhaseReviewDialog(review, store)
    qtbot.addWidget(dialog)
    assert not dialog.proposed_pane.selector.isChecked()
    dialog.proposed_pane.selector.setChecked(True)
    dialog._next()
    assert dialog._index == 1
    assert not dialog.proposed_pane.selector.isChecked()
    dialog.proposed_pane.selector.setChecked(True)
    dialog._previous()
    dialog.original_pane.selector.setChecked(True)
    dialog._next()
    assert dialog._index == 1
    dialog.proposed_pane.selector.setChecked(True)
    dialog._previous()
    assert dialog._index == 0
    dialog._next()
    dialog.proposed_pane.editor.setPlainText("Manual second")
    dialog.proposed_pane._restore()
    assert dialog.proposed_pane.text() == "Second proposal"
    dialog._next()

    assert dialog.review.units[0].choice is ReviewChoice.ORIGINAL
    assert dialog.review.units[1].choice is ReviewChoice.PROPOSED


def test_reopened_complete_review_walks_every_case_before_finishing(qtbot, tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    original = store.put_text(job_id="job", text="Original")
    first = store.put_text(job_id="job", text="Primera propuesta")
    second = store.put_text(job_id="job", text="Segunda propuesta")
    review = (
        ReviewSession.create(
            job_id="job",
            stage=StageKind.REFINE,
            kind=ReviewKind.REFINEMENT,
            input_artifact_id=original.id,
            input_version=1,
            units=(
                ReviewUnit("first", original.id, first.id),
                ReviewUnit("second", original.id, second.id),
            ),
        )
        .decide("first", ReviewChoice.PROPOSED)
        .decide("second", ReviewChoice.PROPOSED)
    )
    dialog = PhaseReviewDialog(review, store)
    qtbot.addWidget(dialog)

    assert dialog._manual_navigation
    assert dialog._index == 0
    assert dialog.next_button.text() == "Confirmar y siguiente"

    dialog._next()
    assert dialog._index == 1
    assert dialog.result() != dialog.DialogCode.Accepted
    assert dialog.next_button.text() == "Aplicar correcciones"

    dialog._previous()
    assert dialog._index == 0
    dialog._next()
    assert dialog._index == 1
    dialog._next()

    assert dialog.result() == dialog.DialogCode.Accepted


def test_phase_review_can_approve_every_proposal_at_once(qtbot, tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    original = store.put_text(job_id="job", text="Original")
    first = store.put_text(job_id="job", text="Primera propuesta")
    second = store.put_text(job_id="job", text="Segunda propuesta")
    review = ReviewSession.create(
        job_id="job",
        stage=StageKind.REFINE,
        kind=ReviewKind.REFINEMENT,
        input_artifact_id=original.id,
        input_version=1,
        units=(
            ReviewUnit("first", original.id, first.id),
            ReviewUnit("second", original.id, second.id),
        ),
    )
    dialog = PhaseReviewDialog(review, store)
    qtbot.addWidget(dialog)

    assert dialog.approve_all_button.text() == "Aplicar recomendaciones seguras"
    dialog._approve_all()

    assert dialog.result() == dialog.DialogCode.Accepted
    assert {unit.choice for unit in dialog.review.units} == {ReviewChoice.PROPOSED}


def test_phase_review_resumes_at_the_first_pending_priority_case(
    qtbot,
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    original = store.put_text(job_id="job", text="Original")
    proposed = store.put_text(job_id="job", text="Propuesta")
    review = ReviewSession.create(
        job_id="job",
        stage=StageKind.REFINE,
        kind=ReviewKind.REFINEMENT,
        input_artifact_id=original.id,
        input_version=1,
        units=(
            ReviewUnit(
                "resolved-high",
                original.id,
                proposed.id,
                severity=ReviewSeverity.HIGH,
            ),
            ReviewUnit(
                "pending-high",
                original.id,
                proposed.id,
                severity=ReviewSeverity.HIGH,
            ),
            ReviewUnit(
                "pending-low",
                original.id,
                proposed.id,
                severity=ReviewSeverity.LOW,
            ),
        ),
    ).decide("resolved-high", ReviewChoice.ORIGINAL)

    dialog = PhaseReviewDialog(review, store)
    qtbot.addWidget(dialog)

    assert dialog._unit().id == "pending-high"
    assert dialog.priority_summary.text().startswith("2 decisiones pendientes · 1")
    assert "Alt+O" in dialog.original_pane.selector.toolTip()
    dialog.proposed_pane.selector.setChecked(True)
    dialog._next()
    assert dialog._unit().id == "pending-low"


def test_phase_review_preserves_risky_originals_in_the_safe_bulk_action(
    qtbot,
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    original = store.put_text(job_id="job", text="# The History")
    risky = store.put_text(job_id="job", text="# Che History")
    safe = store.put_text(job_id="job", text="Texto corregido")
    review = ReviewSession.create(
        job_id="job",
        stage=StageKind.REFINE,
        kind=ReviewKind.REFINEMENT,
        input_artifact_id=original.id,
        input_version=1,
        units=(
            ReviewUnit(
                "risky",
                original.id,
                risky.id,
                recommended_choice=ReviewChoice.ORIGINAL,
                warning="La propuesta elimina contenido.",
            ),
            ReviewUnit("safe", original.id, safe.id),
        ),
    )
    dialog = PhaseReviewDialog(review, store)
    qtbot.addWidget(dialog)

    assert not dialog.original_pane.selector.isChecked()
    assert dialog.unit_warning.text() == "La propuesta elimina contenido."
    assert dialog.approve_all_button.text() == "Aplicar recomendaciones seguras"
    dialog._approve_all()

    assert tuple(unit.choice for unit in dialog.review.units) == (
        ReviewChoice.ORIGINAL,
        ReviewChoice.PROPOSED,
    )


def test_correction_projects_private_syntax_in_both_panes_and_restores_exactly(
    qtbot,
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    original_text = (
        "Original.\n\n"
        "PZDOC: 123...\n\n"
        "Comentario interno: conservar original.\n\n"
        "![](<__parsezen_resources__/images/original.png>)\n"
    )
    proposed_text = (
        "Propuesta.\n\n"
        "PZDOC: 456...\n\n"
        "Comentario interno: conservar propuesta.\n\n"
        "![Alt](__parsezen_resources__/images/proposed.png)\n"
    )
    original = store.put_text(job_id="job", text=original_text)
    proposed = store.put_text(job_id="job", text=proposed_text)
    review = ReviewSession.create(
        job_id="job",
        stage=StageKind.REFINE,
        kind=ReviewKind.REFINEMENT,
        input_artifact_id=original.id,
        input_version=1,
        units=(ReviewUnit("unit", original.id, proposed.id),),
    )
    dialog = PhaseReviewDialog(review, store)
    qtbot.addWidget(dialog)

    for pane in (dialog.original_pane, dialog.proposed_pane):
        assert "PZDOC" not in pane.text()
        assert "Comentario interno" not in pane.text()
        assert "__parsezen_resources__" not in pane.text()
        assert "![]" not in pane.text()
    assert project_review_text(original_text).restore(dialog.original_pane.text()) == original_text

    edited_visible = dialog.proposed_pane.text().replace("Propuesta", "Propuesta editada")
    dialog.proposed_pane.editor.setPlainText(edited_visible)
    dialog._next()

    unit = dialog.review.units[0]
    assert unit.choice is ReviewChoice.EDITED
    assert unit.edited_artifact_id is not None
    assert store.read_text("job", unit.edited_artifact_id) == project_review_text(
        proposed_text
    ).restore(edited_visible)


def test_phase_review_progress_is_proportional_and_controls_are_explicit(
    qtbot,
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    original = store.put_text(job_id="job", text="Original")
    proposed = store.put_text(job_id="job", text="Propuesta")
    review = ReviewSession.create(
        job_id="job",
        stage=StageKind.REFINE,
        kind=ReviewKind.REFINEMENT,
        input_artifact_id=original.id,
        input_version=1,
        units=tuple(ReviewUnit(f"change-{index}", original.id, proposed.id) for index in range(90)),
    )
    dialog = PhaseReviewDialog(
        review,
        store,
        phase_plan=(
            (ReviewKind.TRANSLATION, 10),
            (ReviewKind.REFINEMENT, 90),
        ),
    )
    qtbot.addWidget(dialog)
    dialog.resize(1180, 760)
    dialog.show()
    qtbot.waitExposed(dialog)

    assert dialog.progress_indicator.phase_fractions == (
        (ReviewKind.TRANSLATION, 1.0),
        (ReviewKind.REFINEMENT, 0.0),
    )
    dialog.progress_indicator.set_progress(45)
    assert dialog.progress_indicator.accessibleDescription() == (
        "Fase Corrección: 45 de 90 decisiones confirmadas. "
        "Progreso global: 55 de 100. Quedan 45 en esta fase."
    )
    assert not dialog.progress_indicator.grab().isNull()
    dialog.progress_indicator.resize(220, 62)
    assert not dialog.progress_indicator.grab().isNull()
    assert dialog.findChild(QSplitter).handleWidth() == 1
    assert dialog.original_pane.locate_button.text() == "Ir al inicio"
    assert dialog.proposed_pane.restore_button is not None
    assert dialog.proposed_pane.restore_button.text() == "Restaurar propuesta"
    assert dialog.original_pane.selector.minimumWidth() >= (
        dialog.original_pane.selector.sizeHint().width()
    )


def test_structure_review_never_offers_bulk_approval(qtbot, tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    original = store.put_text(job_id="job", text="# Original")
    proposed = store.put_text(job_id="job", text="# Propuesta")
    review = ReviewSession.create(
        job_id="job",
        stage=StageKind.STRUCTURE,
        kind=ReviewKind.STRUCTURE,
        input_artifact_id=original.id,
        input_version=1,
        units=(ReviewUnit("structure", original.id, proposed.id),),
    )
    dialog = PhaseReviewDialog(
        review,
        store,
        structure_outline=(
            "# Libro\n\n## Parte\n\n### Capítulo\n",
            "# Libro\n\n## Parte\n\n## Capítulo\n",
        ),
    )
    qtbot.addWidget(dialog)

    assert dialog.approve_all_button.isHidden()
    assert not dialog.outline_comparison.isHidden()
    assert dialog.original_outline.toPlainText() == "Libro\n  └─ Parte\n    └─ Capítulo"
    assert dialog.proposed_outline.toPlainText() == "Libro\n  └─ Parte\n  └─ Capítulo"

    dialog.set_compact_mode(True)

    assert dialog.outline_layout.itemAtPosition(2, 0).widget() is dialog.proposed_outline_label


def test_review_pane_displays_binary_page_images(qtbot, tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    pixmap = QPixmap(16, 16)
    pixmap.fill(QColor("white"))
    payload = QByteArray()
    buffer = QBuffer(payload)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert pixmap.save(buffer, "PNG")
    image = store.put(job_id="job", payload=bytes(payload), media_type="image/png")
    text = store.put_text(job_id="job", text="Recognized text")
    review = ReviewSession.create(
        job_id="job",
        stage=StageKind.PREPARE,
        kind=ReviewKind.OCR,
        input_artifact_id=text.id,
        input_version=1,
        units=(
            ReviewUnit(
                "page",
                image.id,
                text.id,
                original_selectable=False,
            ),
        ),
    )

    dialog = PhaseReviewDialog(review, store, translation_follows_ocr=True)
    qtbot.addWidget(dialog)

    assert not dialog.original_pane.image_scroll.isHidden()
    assert dialog.original_pane.editor.isHidden()
    assert "idioma del resultado" in dialog.instruction_label.text()
    assert "no necesitas traducirlo" in dialog.instruction_label.text()
    assert not dialog.proposed_pane.selector.isChecked()
    assert dialog.proposed_pane.no_text_button is not None


def test_phase_review_stacks_panes_and_actions_on_compact_width(
    qtbot,
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    dialog = PhaseReviewDialog(make_review(store), store)
    qtbot.addWidget(dialog)
    dialog.resize(320, 720)
    dialog.show()
    qtbot.waitExposed(dialog)

    assert dialog.width() == 320
    assert dialog.splitter.orientation() is Qt.Orientation.Vertical
    previous_position = dialog.footer_layout.getItemPosition(
        dialog.footer_layout.indexOf(dialog.previous_button)
    )
    next_position = dialog.footer_layout.getItemPosition(
        dialog.footer_layout.indexOf(dialog.next_button)
    )
    save_position = dialog.footer_layout.getItemPosition(
        dialog.footer_layout.indexOf(dialog.save_later_button)
    )
    assert previous_position[0] == next_position[0]
    assert save_position[0] > next_position[0]
    assert dialog.progress_indicator.accessibleDescription()
