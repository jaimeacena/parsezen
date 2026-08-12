"""Desktop entry point."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Sequence
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any

from platformdirs import user_log_path

from parsezen import APP_DISPLAY_NAME, APP_STORAGE_NAME, __version__
from parsezen.branding import APP_ICON_PATH
from parsezen.errors import SettingsError
from parsezen.settings import AppSettings, load_settings, save_settings

if TYPE_CHECKING:
    from parsezen.presentation.main_window import ParsezenMainWindow

LOGGER = logging.getLogger(__package__)
LOG_FILENAME = "parsezen.log"
LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUP_COUNT = 3
OCR_WORKER_SWITCH = "--ocr-worker"
TRANSLATION_WORKER_SWITCH = "--translation-worker"
PACKAGE_SMOKE_SWITCH = "--package-smoke"


def _verify_packaged_runtime_imports() -> None:
    """Import lazy OCR/translation edges that a graphical startup alone cannot exercise."""

    import easyocr
    from argostranslate import package as argos_package
    from argostranslate import sbd as argos_sbd
    from docling.document_converter import DocumentConverter
    from docling.models.plugins.defaults import (
        layout_engines,
        ocr_engines,
        table_structure_engines,
    )

    if (
        easyocr.Reader is None
        or argos_package.get_installed_packages is None
        or argos_sbd.spacy is not None
        or DocumentConverter is None
        or not ocr_engines()["ocr_engines"]
        or not layout_engines()["layout_engines"]
        or not table_structure_engines()["table_structure_engines"]
    ):
        raise RuntimeError("Packaged runtime imports are incomplete.")


def configure_logging(log_directory: Path | None = None) -> Path:
    """Configure the single local rotating log and return its path."""
    directory = (
        log_directory
        if log_directory is not None
        else user_log_path(APP_STORAGE_NAME, appauthor=False)
    )
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / LOG_FILENAME

    for existing_handler in list(LOGGER.handlers):
        if getattr(existing_handler, "_parsezen_file_handler", False):
            LOGGER.removeHandler(existing_handler)
            existing_handler.close()

    handler = RotatingFileHandler(
        log_path,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler._parsezen_file_handler = True  # type: ignore[attr-defined]
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    LOGGER.addHandler(handler)
    return log_path


def build_main_window() -> ParsezenMainWindow:
    """Build the application window."""
    window_type = _main_window_type()
    startup_message: str | None = None
    try:
        settings = load_settings()
    except SettingsError as exc:
        LOGGER.warning("settings_load_failed error_type=%s", type(exc).__name__)
        settings = AppSettings()
        startup_message = str(exc)
    return window_type(
        settings=settings,
        on_settings_changed=save_settings,
        startup_message=startup_message,
        history_path=get_history_path(),
    )


def get_history_path() -> Path:
    """Resolve the activity file without importing processing code during module import."""

    from parsezen.recent_activity import get_history_path as resolve_history_path

    return resolve_history_path()


def _main_window_type() -> type[ParsezenMainWindow]:
    from parsezen.presentation.main_window import ParsezenMainWindow

    return ParsezenMainWindow


def _configure_logging_without_startup_failure() -> None:
    """Configure logs when possible without making storage failures fatal."""
    try:
        configure_logging()
    except OSError:
        pass


def _run_desktop_application(
    arguments: list[str],
    application_type: type[Any],
    icon_type: Callable[[str], Any],
    window_factory: Callable[[], Any],
) -> int:
    """Run the regular desktop lifecycle through small injectable factories."""
    application = application_type(arguments)
    application.setApplicationName(APP_DISPLAY_NAME)
    application.setApplicationDisplayName(APP_DISPLAY_NAME)
    application.setWindowIcon(icon_type(str(APP_ICON_PATH)))
    _apply_startup_theme(application)
    window = window_factory()
    window.show()
    exit_code = int(application.exec())
    LOGGER.info("application_stopped exit_code=%d", exit_code)
    return exit_code


def _apply_startup_theme(application: Any) -> None:
    """Resolve the saved/system theme before the first window is constructed."""

    from PySide6.QtWidgets import QApplication

    if not isinstance(application, QApplication):
        return
    from parsezen.presentation.design_system import (
        apply_parsezen_theme,
        preferred_theme_mode,
    )

    apply_parsezen_theme(application, preferred_theme_mode())


def main(argv: Sequence[str] | None = None) -> int:
    """Start the desktop application."""
    arguments = list(argv) if argv is not None else sys.argv
    if len(arguments) > 1 and arguments[1] == OCR_WORKER_SWITCH:
        from parsezen.ocr_worker import main as worker_main

        return worker_main(arguments[2:])
    if len(arguments) > 1 and arguments[1] == TRANSLATION_WORKER_SWITCH:
        from parsezen.offline_translation_worker import main as worker_main

        return worker_main(arguments[2:])

    package_smoke = len(arguments) == 2 and arguments[1] == PACKAGE_SMOKE_SWITCH

    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    if package_smoke:
        if getattr(sys, "frozen", False):
            _verify_packaged_runtime_imports()
        existing_application = QApplication.instance()
        application = (
            existing_application
            if isinstance(existing_application, QApplication)
            else QApplication([arguments[0]])
        )
        application.setApplicationName(APP_DISPLAY_NAME)
        application.setApplicationDisplayName(APP_DISPLAY_NAME)
        application.setWindowIcon(QIcon(str(APP_ICON_PATH)))
        _apply_startup_theme(application)
        from parsezen.presentation.main_window import ParsezenMainWindow

        window = ParsezenMainWindow(settings=AppSettings())
        window.show()
        application.processEvents()
        window.close()
        application.processEvents()
        return 0

    _configure_logging_without_startup_failure()
    LOGGER.info("application_started version=%s", __version__)
    return _run_desktop_application(arguments, QApplication, QIcon, build_main_window)


if __name__ == "__main__":
    raise SystemExit(main())
