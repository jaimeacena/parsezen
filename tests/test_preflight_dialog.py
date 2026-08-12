from PySide6.QtWidgets import QLabel, QPushButton

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
        early_check_recommended=True,
        flow_steps=("PDF", "Traducir a español y corregir", "Organizar", "EPUB"),
    )
    dialog = PreflightDialog(combine_preflights((document,)), {"job": "Libro.pdf"})
    qtbot.addWidget(dialog)

    labels = tuple(label.text() for label in dialog.findChildren(QLabel))
    assert any("Libro.pdf" in label for label in labels)
    assert any("Duración aproximada" in label for label in labels)
    assert any(
        "PDF  →  Traducir a español y corregir  →  Organizar  →  EPUB" in label for label in labels
    )
    assert any("Comprobación previa" in label for label in labels)
    assert dialog.details_panel.isHidden()
    assert any("Revisión prevista" in label for label in labels)

    dialog.details_button.click()

    assert not dialog.details_panel.isHidden()
    assert dialog.details_button.text() == "Ocultar detalles técnicos"
    assert any(
        button.text() == "Cambiar configuración" for button in dialog.findChildren(QPushButton)
    )


def test_single_document_preflight_does_not_repeat_time_or_confidence(qtbot) -> None:
    document = DocumentPreflight(
        "job",
        "317 páginas",
        DurationEstimate(7_140, 12_000, 23_760, 0),
        ("editor EPUB final",),
        (),
        flow_steps=("PDF", "EPUB"),
    )
    dialog = PreflightDialog(combine_preflights((document,)), {"job": "Libro.pdf"})
    qtbot.addWidget(dialog)

    assert dialog.findChild(QLabel, "preflightDuration").text() == "Entre 2 h y 6 h 30 min"
    assert not any(
        "Estimación inicial" in label.text()
        for label in dialog.findChildren(QLabel)
        if not dialog.details_panel.isAncestorOf(label)
    )
