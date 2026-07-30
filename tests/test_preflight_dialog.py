from PySide6.QtWidgets import QLabel

from parsezen.application.preflight import (
    DocumentPreflight,
    PreflightFinding,
    PreflightSeverity,
    combine_preflights,
)
from parsezen.domain.estimates import DurationEstimate
from parsezen.presentation.preflight_dialog import PreflightDialog


def test_preflight_dialog_explains_time_risk_and_manual_review(qtbot) -> None:
    document = DocumentPreflight(
        "job",
        "100 páginas",
        DurationEstimate(1_800, 2_400, 3_600, 6),
        ("traducción", "editor EPUB final"),
        (
            PreflightFinding(
                "pdf-to-epub",
                PreflightSeverity.ATTENTION,
                "El diseño se reconstruirá",
                "Revisa la jerarquía final.",
            ),
        ),
    )
    dialog = PreflightDialog(combine_preflights((document,)), {"job": "Libro.pdf"})
    qtbot.addWidget(dialog)

    labels = tuple(label.text() for label in dialog.findChildren(QLabel))
    assert any("Libro.pdf" in label for label in labels)
    assert any("Tiempo automático aproximado" in label for label in labels)
    assert any("Revisión prevista" in label for label in labels)
    assert any("Ten en cuenta" in label for label in labels)
