from datetime import UTC, datetime
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel

from parsezen.domain.attempt_activity import (
    AttemptEvent,
    AttemptEventStatus,
    AttemptPhase,
    AttemptTimeline,
    FailureSnapshot,
    ReusableWork,
)
from parsezen.domain.outcomes import OutcomeSummary
from parsezen.presentation.activity_view import (
    ActivityView,
    build_failure_diagnostic,
    outcome_summary_lines,
)
from parsezen.recent_activity import RecentJob, RecentJobStatus


def _failed_job(source: Path, *, finished_at: datetime | None = None) -> RecentJob:
    first = datetime(2026, 8, 3, 8, 0, tzinfo=UTC)
    timeline = AttemptTimeline(
        (
            AttemptEvent(AttemptPhase.PREPARATION, AttemptEventStatus.STARTED, timestamp=first),
            AttemptEvent(
                AttemptPhase.PREPARATION,
                AttemptEventStatus.COMPLETED,
                timestamp=first,
            ),
            AttemptEvent(
                AttemptPhase.TRANSLATION,
                AttemptEventStatus.STARTED,
                timestamp=datetime(2026, 8, 3, 8, 1, tzinfo=UTC),
            ),
            AttemptEvent(
                AttemptPhase.TRANSLATION,
                AttemptEventStatus.FAILED,
                timestamp=datetime(2026, 8, 3, 8, 2, tzinfo=UTC),
            ),
        )
    )
    return RecentJob(
        source,
        RecentJobStatus.FAILED,
        finished_at or datetime(2026, 8, 3, 8, 2, 30, tzinfo=UTC),
        attempt_id="attempt-123",
        timeline=timeline,
        failure=FailureSnapshot(
            AttemptPhase.TRANSLATION,
            "local_ai",
            "La transformación local no pudo continuar.",
            diagnostic_reference="ref-456",
            reusable_work=ReusableWork.PREVIOUS_PHASES,
        ),
    )


def test_activity_view_exposes_result_actions_and_reflows(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "book.pdf"
    result = tmp_path / "book.epub"
    source.write_bytes(b"source")
    result.write_bytes(b"result")
    recent = RecentJob(
        source,
        RecentJobStatus.COMPLETED,
        datetime.now(UTC),
        result,
        OutcomeSummary(
            output_format="EPUB",
            operations=("Conversión", "Traducción", "Edición EPUB"),
            processed_pages=100,
            ocr_pages=8,
            preserved_images=12,
            chapters=14,
            translation_issues=2,
            integrity_verified=True,
            integrity_checks=3,
            review_units=5,
            review_changes=3,
            review_originals=2,
            duration_seconds=600,
            estimate_lower_seconds=480,
            estimate_upper_seconds=900,
            ai_review_recommended=True,
            ai_review_blocks=2,
            ai_review_signals=3,
            linguistic_review_mode="corrected_during_translation",
            translation_checked_blocks=20,
            translation_reviewed_blocks=16,
            translation_unreviewed_blocks=4,
        ),
    )
    view = ActivityView((recent,))
    qtbot.addWidget(view)
    opened: list[Path] = []
    view.open_requested.connect(opened.append)

    assert "Integridad técnica" in view.details_summary.text()
    assert "comprobada" in view.details_summary.text()
    assert "Incidencias detectadas" in view.details_summary.text()
    assert "Revisión manual" in view.details_summary.text()
    assert "Revisión con IA sugerida" in view.details_summary.text()
    assert "No se ejecutó automáticamente" in view.details_summary.text()
    assert "corrección integrada durante la traducción" in view.details_summary.text()
    assert "20 bloques comprobados automáticamente" in view.details_summary.text()
    assert "4 sin revisión semántica" in view.details_summary.text()
    assert "sin segunda verificación bilingüe independiente" in view.details_summary.text()
    assert "2 incidencias pendientes" in view.details_summary.text()

    qtbot.mouseClick(view.open_button, Qt.MouseButton.LeftButton)
    assert opened == [result]

    view.set_compact_mode(True)
    assert view.panels.orientation() is Qt.Orientation.Vertical
    assert view.actions_layout.itemAtPosition(0, 1).widget() is view.folder_button
    view.set_compact_mode(False)
    assert view.panels.orientation() is Qt.Orientation.Horizontal


def test_summary_never_presents_technical_integrity_as_semantic_quality() -> None:
    lines = outcome_summary_lines(
        OutcomeSummary(
            output_format="MD",
            integrity_verified=True,
            integrity_checks=2,
            translation_issues=3,
            manual_review_expected=False,
        )
    )

    assert any("Integridad técnica: comprobada" in line for line in lines)
    assert any("3 de traducción" in line for line in lines)
    assert any("no estaba activada" in line for line in lines)


@pytest.mark.parametrize(
    ("summary", "expected"),
    (
        (OutcomeSummary("EPUB", editor_completed=True), "editor EPUB final completado"),
        (
            OutcomeSummary("MD", manual_review_expected=True),
            "no hubo incidencias que exigieran una decisión",
        ),
        (
            OutcomeSummary("MD", manual_review_expected=True, translation_issues=1),
            "no consta una decisión manual",
        ),
    ),
)
def test_summary_distinguishes_pending_manual_review_states(
    summary: OutcomeSummary,
    expected: str,
) -> None:
    assert any(expected in line for line in outcome_summary_lines(summary))


def test_failed_activity_explains_phase_work_timeline_and_safe_actions(
    qtbot,
    tmp_path: Path,
) -> None:
    source = tmp_path / "private-book.txt"
    source.write_text("Contenido privado", encoding="utf-8")
    failed = _failed_job(source)
    view = ActivityView((failed,), current_job_ids={source: "job-1"})
    qtbot.addWidget(view)

    assert "Error en traducción" in view.jobs_list.item(0).text()
    assert "Error en traducción" in view.details_status.text()
    assert "Qué ocurrió" in view.failure_heading.text()
    assert failed.failure is not None
    assert failed.failure.message in view.failure_message.text()
    assert "Tu trabajo" in view.reusable_work_heading.text()
    assert "fases anteriores" in view.reusable_work.text()
    assert "Traducción · iniciada" in view.timeline_text.text()
    assert "translating" not in view.timeline_text.text()
    assert "attempt-123" not in " ".join(
        label.text() for label in view.details.findChildren(QLabel)
    )
    assert not view.return_button.isHidden()
    assert not view.copy_diagnostic_button.isHidden()
    assert view.open_button.isHidden()
    assert view.folder_button.isHidden()

    returned: list[str] = []
    view.return_to_document_requested.connect(returned.append)
    qtbot.mouseClick(view.return_button, Qt.MouseButton.LeftButton)
    assert returned == ["job-1"]

    copied: list[bool] = []
    view.diagnostic_copied.connect(lambda: copied.append(True))
    qtbot.mouseClick(view.copy_diagnostic_button, Qt.MouseButton.LeftButton)
    diagnostic = build_failure_diagnostic(failed)
    assert view.copy_feedback.text() == "Diagnóstico copiado."
    assert copied == [True]
    assert diagnostic == QApplication.clipboard().text()
    assert source.name not in diagnostic
    assert str(source) not in diagnostic
    assert failed.failure.message not in diagnostic
    assert "attempt-123" in diagnostic
    assert "ref-456" in diagnostic
    assert "translate · failed" in diagnostic


def test_historical_failed_activity_has_no_fake_recovery_or_result_action(
    qtbot,
    tmp_path: Path,
) -> None:
    source = tmp_path / "historical.txt"
    source.write_text("Original", encoding="utf-8")
    view = ActivityView((_failed_job(source),))
    qtbot.addWidget(view)

    returned: list[str] = []
    opened: list[Path] = []
    view.return_to_document_requested.connect(returned.append)
    view.open_requested.connect(opened.append)

    assert view.return_button.isHidden()
    assert not view.copy_diagnostic_button.isHidden()
    assert "solo están disponibles" in view.recovery_note.text()
    qtbot.mouseDClick(view.jobs_list.viewport(), Qt.MouseButton.LeftButton)
    assert returned == []
    assert opened == []


def test_legacy_failed_activity_hides_reference_without_safe_tokens(
    qtbot,
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy.txt"
    source.write_text("Original", encoding="utf-8")
    legacy = RecentJob(source, RecentJobStatus.FAILED, datetime.now(UTC))
    view = ActivityView((legacy,))
    qtbot.addWidget(view)

    assert view.failure_reference.isHidden()
    assert (
        "referencia técnica"
        not in " ".join(label.text() for label in view.details.findChildren(QLabel)).casefold()
    )
    assert "Con error" in view.jobs_list.item(0).text()
    assert view.details_status.text().startswith("Con error · ")
    assert "prepar" not in view.jobs_list.item(0).text().casefold()

    diagnostic = build_failure_diagnostic(legacy)
    assert "Fase de error: unknown" in diagnostic
    assert "Fase de error: prepare" not in diagnostic


def test_activity_rows_keep_cancelled_truthful_and_reflow_without_overflow(
    qtbot,
    tmp_path: Path,
) -> None:
    source = tmp_path / "cancelled.txt"
    source.write_text("Original", encoding="utf-8")
    cancelled = RecentJob(source, RecentJobStatus.CANCELLED, datetime.now(UTC))
    view = ActivityView((cancelled,))
    qtbot.addWidget(view)
    view.resize(320, 620)
    view.show()
    qtbot.waitExposed(view)

    assert "Cancelado" in view.jobs_list.item(0).text()
    assert view._compact is True  # noqa: SLF001
    assert view.panels.orientation() is Qt.Orientation.Vertical
    assert view.jobs_list.horizontalScrollBar().maximum() == 0
    assert view.actions_layout.itemAtPosition(0, 1).widget() is view.folder_button
