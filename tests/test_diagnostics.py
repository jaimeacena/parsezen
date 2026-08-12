from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

import parsezen.diagnostics as diagnostics_module
from parsezen.diagnostics import build_diagnostic_report
from parsezen.presentation.diagnostics_dialog import DiagnosticsDialog
from parsezen.recent_activity import (
    RecentJob,
    RecentJobStatus,
    append_recent_jobs,
)
from parsezen.settings import AppSettings


def test_diagnostic_report_contains_useful_state_but_no_private_paths_or_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "secret-book.pdf"
    result = tmp_path / "secret-book.md"
    source.write_bytes(b"%PDF-private")
    result.write_text("Private result", encoding="utf-8")
    history_path = tmp_path / "recent.json"
    append_recent_jobs(
        (
            RecentJob(source, RecentJobStatus.COMPLETED, datetime.now(UTC), result),
            RecentJob(source, RecentJobStatus.FAILED, datetime.now(UTC)),
        ),
        path=history_path,
    )
    checkpoint_root = tmp_path / "checkpoints"
    checkpoint_directory = checkpoint_root / ("a" * 64)
    checkpoint_directory.mkdir(parents=True)
    (checkpoint_directory / f"{'b' * 64}.json").write_bytes(b"encrypted")
    log_path = tmp_path / "parsezen.log"
    log_path.write_text(
        "processing_failed incident=deadbeef unexpected_error_type=RuntimeError "
        "module=parsezen.processing function=process_document line=199 "
        "stage=organizing_structure "
        f"private_path={source}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(diagnostics_module, "_module_available", lambda _name: True)
    monkeypatch.setattr(diagnostics_module, "_available_bytes", lambda _settings: 5 * 1024**3)

    report = build_diagnostic_report(
        AppSettings(model="qwen3:4b"),
        ollama_status="ready",
        installed_models=3,
        history_path=history_path,
        work_checkpoint_root=checkpoint_root,
        log_path=log_path,
    )

    assert "Estado de la IA: Preparada" in report
    assert "Modelo seleccionado: qwen3:4b" in report
    assert "Actividad reciente: 2 total · 1 completadas · 1 con error" in report
    assert "Checkpoints: 1 trabajos · 1 partes · 9 B" in report
    assert "RuntimeError · referencia deadbeef · etapa personalización" in report
    assert "Espacio temporal disponible: 5.0 GiB" in report
    assert str(tmp_path) not in report
    assert "secret-book" not in report
    assert "Private result" not in report


def test_diagnostics_dialog_copies_only_the_prepared_report(qtbot) -> None:
    report = "Parsezen — diagnóstico local\nPrivacidad: sin rutas."
    dialog = DiagnosticsDialog(report)
    qtbot.addWidget(dialog)

    qtbot.mouseClick(dialog.copy_button, Qt.MouseButton.LeftButton)

    assert dialog.report_view.toPlainText() == report
    assert "copiado" in dialog.copy_status.text().casefold()


def test_diagnostic_report_uses_the_current_sqlite_queue_count(tmp_path: Path) -> None:
    report = build_diagnostic_report(
        AppSettings(),
        ollama_status=None,
        installed_models=0,
        queued_documents=4,
        history_path=tmp_path / "missing-history",
        work_checkpoint_root=tmp_path / "missing-checkpoints",
        log_path=tmp_path / "missing.log",
    )

    assert "Documentos guardados en la cola: 4" in report
    assert "Documentos guardados en la cola: 4" in report


def test_diagnostic_fallbacks_remain_sanitized_when_local_checks_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_log = tmp_path / "expected.log"
    expected_log.write_text(
        "unrelated private text\nprocessing_failed error_type=ConversionError "
        "stage=converting duration_ms=10\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        diagnostics_module.importlib.util,
        "find_spec",
        lambda _name: (_ for _ in ()).throw(ModuleNotFoundError),
    )
    monkeypatch.setattr(
        diagnostics_module.shutil,
        "disk_usage",
        lambda _path: (_ for _ in ()).throw(OSError),
    )

    report = build_diagnostic_report(
        AppSettings(model="bad\nmodel"),
        ollama_status=None,
        installed_models=-4,
        history_path=tmp_path / "missing-history",
        work_checkpoint_root=tmp_path / "missing-checkpoints",
        log_path=expected_log,
    )

    assert "Estado de la IA: Sin comprobar" in report
    assert "Modelo seleccionado: Identificador no mostrable" in report
    assert "Modelos instalados detectados: 0" in report
    assert "OCR local: No disponible" in report
    assert "Espacio temporal disponible: 0 B" in report
    assert "Último fallo registrado: ConversionError · etapa conversión" in report
    assert "unrelated private text" not in report
    assert diagnostics_module._safe_model(None) == "Ninguno"
    assert diagnostics_module._format_bytes(2 * 1024**2) == "2.0 MiB"
    assert diagnostics_module._format_bytes(3 * 1024) == "3.0 KiB"


def test_diagnostics_uses_the_latest_canonical_early_check_failure(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "parsezen.log"
    log_path.write_text(
        "processing_failed attempt_id=older phase=translate error_code=translation "
        "error_type=TranslationError\n"
        "processing_failed attempt_id=newer phase=early_check error_code=early_check "
        "error_type=EarlyCheckError\n",
        encoding="utf-8",
    )

    report = build_diagnostic_report(
        AppSettings(),
        ollama_status=None,
        installed_models=0,
        history_path=tmp_path / "missing-history",
        work_checkpoint_root=tmp_path / "missing-checkpoints",
        log_path=log_path,
    )

    assert "EarlyCheckError \u00b7 etapa comprobaci\u00f3n temprana" in report
    assert "TranslationError" not in report
    assert "newer" not in report
