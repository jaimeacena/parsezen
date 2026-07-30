from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QSplitter

from parsezen.application.review_coordinator import apply_phase_review
from parsezen.domain.jobs import (
    DocumentFormat,
    DocumentJob,
    DocumentSource,
    JobConfiguration,
    RefinementConfiguration,
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


def test_phase_review_completes_only_its_blocked_stage(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    review = make_review(store).decide("unit", choice=ReviewChoice.PROPOSED)
    job = DocumentJob.create(
        DocumentSource(Path("book.md"), DocumentFormat.MARKDOWN, 100, 1),
        JobConfiguration(refinement=RefinementConfiguration(enabled=True)),
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
    assert dialog.proposed_pane.selector.isChecked()
    dialog._next()
    assert dialog._index == 1
    assert dialog.proposed_pane.selector.isChecked()
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

    assert dialog.approve_all_button.text() == "Aprobar todas las correcciones"
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
    dialog._next()
    assert dialog._unit().id == "pending-low"


def test_phase_review_preserves_risky_originals_in_the_safe_bulk_action(
    qtbot,
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    original = store.put_text(job_id="job", text="Texto original")
    risky = store.put_text(job_id="job", text="Resumen inseguro")
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

    assert dialog.original_pane.selector.isChecked()
    assert dialog.unit_warning.text() == "La propuesta elimina contenido."
    assert dialog.approve_all_button.text() == "Aplicar correcciones seguras"
    dialog._approve_all()

    assert tuple(unit.choice for unit in dialog.review.units) == (
        ReviewChoice.ORIGINAL,
        ReviewChoice.PROPOSED,
    )


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
        (ReviewKind.TRANSLATION, 0.1),
        (ReviewKind.REFINEMENT, 0.9),
    )
    dialog.progress_indicator.set_progress(45)
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
    dialog = PhaseReviewDialog(review, store)
    qtbot.addWidget(dialog)

    assert dialog.approve_all_button.isHidden()


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

    dialog = PhaseReviewDialog(review, store)
    qtbot.addWidget(dialog)

    assert not dialog.original_pane.image_scroll.isHidden()
    assert dialog.original_pane.editor.isHidden()
    assert dialog.proposed_pane.selector.isChecked()


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
