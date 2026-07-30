from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from PySide6.QtWidgets import QApplication

import parsezen.__main__ as entry
import parsezen.ocr_worker as ocr_worker
import parsezen.offline_translation_worker as translation_worker
from parsezen.errors import SettingsError
from parsezen.presentation.main_window import ParsezenMainWindow


def test_worker_switches_dispatch_without_starting_qt(monkeypatch) -> None:
    monkeypatch.setattr(ocr_worker, "main", lambda arguments: 11 if arguments == ["page"] else 1)
    monkeypatch.setattr(
        translation_worker,
        "main",
        lambda arguments: 12 if arguments == ["book"] else 1,
    )

    assert entry.main(["parsezen", "--ocr-worker", "page"]) == 11
    assert entry.main(["parsezen", "--translation-worker", "book"]) == 12


def test_regular_desktop_lifecycle_configures_theme_before_window(
    monkeypatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        entry,
        "_apply_startup_theme",
        lambda _application: calls.append("theme"),
    )

    class Application:
        @staticmethod
        def instance() -> None:
            return None

        def __init__(self, arguments: list[str]) -> None:
            calls.append(("application", arguments))

        def setApplicationName(self, name: str) -> None:  # noqa: N802
            calls.append(("name", name))

        def setApplicationDisplayName(self, name: str) -> None:  # noqa: N802
            calls.append(("display", name))

        def setWindowIcon(self, icon: object) -> None:  # noqa: N802
            calls.append(("icon", icon))

        def exec(self) -> int:
            return 7

    class Window:
        def __init__(self) -> None:
            calls.append("window")

        def show(self) -> None:
            calls.append("show")

    assert (
        entry._run_desktop_application(
            ["parsezen"],
            Application,
            lambda path: ("QIcon", path),
            Window,
        )
        == 7
    )
    assert "show" in calls
    assert ("name", "Parsezen") in calls
    assert calls.index("theme") < calls.index("window") < calls.index("show")


def test_logging_failure_does_not_prevent_desktop_startup(monkeypatch) -> None:
    monkeypatch.setattr(
        entry,
        "configure_logging",
        lambda: (_ for _ in ()).throw(OSError("read-only")),
    )

    entry._configure_logging_without_startup_failure()


def test_logging_rotates_the_owned_handler_without_duplicating_it(tmp_path: Path) -> None:
    try:
        first = entry.configure_logging(tmp_path)
        second = entry.configure_logging(tmp_path)

        assert first == second == tmp_path / entry.LOG_FILENAME
        owned = [
            handler
            for handler in entry.LOGGER.handlers
            if getattr(handler, "_parsezen_file_handler", False)
        ]
        assert len(owned) == 1
    finally:
        for handler in list(entry.LOGGER.handlers):
            if getattr(handler, "_parsezen_file_handler", False):
                entry.LOGGER.removeHandler(handler)
                handler.close()


def test_main_window_recovers_from_damaged_settings(monkeypatch) -> None:
    monkeypatch.setattr(
        entry,
        "load_settings",
        lambda: (_ for _ in ()).throw(SettingsError("Ajustes dañados")),
    )
    monkeypatch.setattr(entry, "get_history_path", lambda: Path("history.json"))
    monkeypatch.setattr(entry, "_main_window_type", lambda: lambda **kwargs: kwargs)

    window = cast(dict[str, object], entry.build_main_window())

    assert window["startup_message"] == "Ajustes dañados"
    assert window["history_path"] == Path("history.json")


def test_real_main_window_surfaces_a_corrupt_settings_warning(
    tmp_path: Path,
    qtbot,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        entry,
        "load_settings",
        lambda: (_ for _ in ()).throw(SettingsError("Configuración corrupta conservada.")),
    )
    monkeypatch.setattr(entry, "get_history_path", lambda: tmp_path / "history.json")
    monkeypatch.setattr(
        entry,
        "_main_window_type",
        lambda: (
            lambda **kwargs: ParsezenMainWindow(
                **kwargs,
                state_path=tmp_path / "workspace.sqlite3",
                auto_discover_ai=False,
            )
        ),
    )
    window = entry.build_main_window()
    qtbot.addWidget(window)

    assert window.parsezen_workspace.recovery_warning.text() == (
        "Configuración corrupta conservada."
    )


def test_package_smoke_opens_and_closes_the_real_window(
    qapp: QApplication,
    monkeypatch,
) -> None:
    assert QApplication.instance() is qapp
    verified: list[bool] = []
    monkeypatch.setattr(entry.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        entry,
        "_verify_packaged_runtime_imports",
        lambda: verified.append(True),
    )

    assert entry.main(["parsezen", "--package-smoke"]) == 0
    assert verified == [True]


def test_regular_main_delegates_to_the_testable_desktop_lifecycle(monkeypatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        entry,
        "_configure_logging_without_startup_failure",
        lambda: calls.append("logging"),
    )
    monkeypatch.setattr(
        entry,
        "_run_desktop_application",
        lambda arguments, *_factories: calls.append(arguments) or 9,
    )

    assert entry.main(["parsezen"]) == 9
    assert calls == ["logging", ["parsezen"]]


def test_packaged_runtime_verifies_every_lazy_processing_edge(monkeypatch) -> None:
    from argostranslate import sbd as argos_sbd

    # The Windows package deliberately excludes spaCy; Argos then uses its supported
    # fallback, while OCR and the translation package remain importable.
    monkeypatch.setattr(argos_sbd, "spacy", None)

    entry._verify_packaged_runtime_imports()


def test_packaged_runtime_rejects_an_incomplete_lazy_dependency(monkeypatch) -> None:
    import easyocr
    from argostranslate import sbd as argos_sbd

    monkeypatch.setattr(argos_sbd, "spacy", None)
    monkeypatch.setattr(easyocr, "Reader", None)

    with pytest.raises(RuntimeError, match="runtime imports are incomplete"):
        entry._verify_packaged_runtime_imports()
