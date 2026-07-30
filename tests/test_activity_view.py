from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import Qt

from parsezen.domain.outcomes import OutcomeSummary
from parsezen.job_sessions import RecentJob, RecentJobStatus
from parsezen.presentation.activity_view import ActivityView, outcome_summary_lines


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
            integrity_verified=True,
            integrity_checks=3,
            review_units=5,
            review_changes=3,
            review_originals=2,
            duration_seconds=600,
            estimate_lower_seconds=480,
            estimate_upper_seconds=900,
        ),
    )
    view = ActivityView((recent,))
    qtbot.addWidget(view)
    opened: list[Path] = []
    view.open_requested.connect(opened.append)

    assert "Integridad técnica: comprobada" in view.details_summary.text()
    assert "Incidencias detectadas" in view.details_summary.text()
    assert "Revisión manual" in view.details_summary.text()

    qtbot.mouseClick(view.open_button, Qt.MouseButton.LeftButton)
    assert opened == [result]

    view.set_compact_mode(True)
    assert view.actions_layout.itemAtPosition(1, 0).widget() is view.folder_button


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
