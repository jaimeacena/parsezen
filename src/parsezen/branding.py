"""Brand assets shared by the desktop runtime and its tests."""

from __future__ import annotations

import sys
from pathlib import Path


def _resource_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root is not None:
        return Path(frozen_root)
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "assets").is_dir():
        return source_root
    return Path(sys.prefix) / "share" / "parsezen"


_RESOURCE_ROOT = _resource_root()
_GENERATED_ROOT = _RESOURCE_ROOT / "assets" / "branding" / "generated"
BRAND_LOGO_PATH = _GENERATED_ROOT / "parsezen-logo-light.png"
BRAND_DARK_LOGO_PATH = _GENERATED_ROOT / "parsezen-logo-dark.png"
APP_ICON_PATH = _GENERATED_ROOT / "parsezen-symbol-1024.png"
APP_ICON_ICO_PATH = _GENERATED_ROOT / "parsezen-app-icon.ico"
INTER_FONT_PATH = _RESOURCE_ROOT / "assets" / "fonts" / "Inter.ttf"
