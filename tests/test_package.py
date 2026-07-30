from importlib import import_module, metadata
from pathlib import Path

from PySide6.QtGui import QImage

import parsezen


def test_package_exposes_version() -> None:
    assert parsezen.__version__ == metadata.version("parsezen")


def test_package_exposes_visible_and_storage_names() -> None:
    assert parsezen.APP_DISPLAY_NAME == "Parsezen"
    assert parsezen.APP_STORAGE_NAME == "Parsezen"


def test_ui_brand_asset_exists() -> None:
    branding = import_module("parsezen.branding")

    assert branding.BRAND_LOGO_PATH.is_file()
    assert branding.BRAND_DARK_LOGO_PATH.is_file()
    assert branding.APP_ICON_PATH.is_file()

    icon = QImage(str(branding.APP_ICON_PATH))
    assert not icon.isNull()
    assert (icon.width(), icon.height()) == (1024, 1024)
    assert icon.hasAlphaChannel()
    assert icon.pixelColor(0, 0).alpha() == 0
    assert branding.APP_ICON_ICO_PATH.is_file()


def test_windows_package_uses_the_mascot_icon_and_visible_name() -> None:
    root = Path(__file__).parents[1]
    spec = (root / "distribution" / "windows" / "Parsezen.spec").read_text(encoding="utf-8")
    installer = (root / "distribution" / "windows" / "Parsezen.iss").read_text(encoding="utf-8")

    assert 'icon=str(root / "assets" / "branding" / "generated" / "parsezen-app-icon.ico")' in spec
    assert 'name="Parsezen"' in spec
    assert '#define AppName "Parsezen"' in installer
    assert "SetupIconFile=..\\..\\assets\\branding\\generated\\parsezen-app-icon.ico" in installer
    assert "OutputBaseFilename=Parsezen-Setup-{#AppVersion}" in installer
    assert 'Filename: "{app}\\{#AppName}.exe"' in installer
    assert "AppPublisher=Parsezen contributors" in installer
    assert "AppSupportURL=https://github.com/jaimeacena/parsezen/issues" in installer
    assert 'Type: filesandordirs; Name: "{app}\\_internal"' in installer


def test_windows_release_workflow_publishes_verifiable_artifacts() -> None:
    root = Path(__file__).parents[1]
    workflow = (root / ".github" / "workflows" / "package-windows.yml").read_text(encoding="utf-8")

    assert "actions/attest-build-provenance@v4" in workflow
    assert "Parsezen-Setup-*.exe.sha256" in workflow
    assert "python-environment.json" in workflow


def test_desktop_entry_point_imports() -> None:
    entry_point = import_module("parsezen.__main__")

    assert callable(entry_point.main)
    assert callable(entry_point.build_main_window)


def test_ui_module_imports() -> None:
    main_window = import_module("parsezen.presentation.main_window")
    local_ai = import_module("parsezen.presentation.local_ai_controller")
    runner = import_module("parsezen.presentation.processing_runner")

    assert main_window.ParsezenMainWindow is not None
    assert local_ai.LocalAIController is not None
    assert runner.ProcessingWorker is not None


def test_desktop_package_smoke_mode_starts_and_closes_offscreen(
    monkeypatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    entry_point = import_module("parsezen.__main__")

    assert entry_point.main(["parsezen", "--package-smoke"]) == 0


def test_ai_and_settings_modules_import() -> None:
    improvement = import_module("parsezen.improvement")
    settings = import_module("parsezen.settings")

    assert improvement.ImprovementMode.CLEAN.value == "clean"
    assert settings.AppSettings().context_window is None
