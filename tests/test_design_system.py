import re
from pathlib import Path

from PIL import Image
from PySide6.QtWidgets import QApplication

import parsezen.presentation.design_system as design_system_module
from parsezen.branding import APP_ICON_PATH, BRAND_DARK_LOGO_PATH, BRAND_LOGO_PATH
from parsezen.presentation.design_system import (
    COLORS,
    ThemeMode,
    apply_parsezen_theme,
    contrast_ratio,
    current_theme_mode,
    current_theme_preference,
)


def test_theme_can_follow_system_and_switch_without_restarting(qtbot) -> None:
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    try:
        apply_parsezen_theme(application, ThemeMode.DARK)
        assert current_theme_mode() is ThemeMode.DARK
        assert current_theme_preference() is ThemeMode.DARK
        assert COLORS.canvas == "#0C1419"
        assert COLORS.table_surface != COLORS.canvas
        assert COLORS.table_border != COLORS.table_separator

        apply_parsezen_theme(application, ThemeMode.LIGHT)
        assert current_theme_mode() is ThemeMode.LIGHT
        assert COLORS.canvas == "#F3F6F8"
        assert COLORS.table_surface != COLORS.table_header
        assert COLORS.table_border != COLORS.table_separator

        apply_parsezen_theme(application, ThemeMode.SYSTEM)
        assert current_theme_preference() is ThemeMode.SYSTEM
        assert current_theme_mode() in {ThemeMode.LIGHT, ThemeMode.DARK}
    finally:
        apply_parsezen_theme(application, ThemeMode.DARK)


def test_semantic_colours_meet_core_wcag_contrast_targets() -> None:
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    try:
        for mode in (ThemeMode.LIGHT, ThemeMode.DARK):
            apply_parsezen_theme(application, mode)
            assert contrast_ratio(COLORS.text_primary, COLORS.canvas) >= 4.5
            assert contrast_ratio(COLORS.text_secondary, COLORS.canvas) >= 4.5
            assert contrast_ratio(COLORS.text_muted, COLORS.canvas) >= 4.5
            assert contrast_ratio(COLORS.text_inverse, COLORS.action_primary) >= 4.5
            assert contrast_ratio(COLORS.overlay_text, COLORS.overlay) >= 4.5
            assert contrast_ratio(COLORS.border, COLORS.surface) >= 3
            assert contrast_ratio(COLORS.focus_ring, COLORS.canvas) >= 3
            assert COLORS.divider != COLORS.border
            for status in (COLORS.info, COLORS.success, COLORS.warning, COLORS.error):
                assert contrast_ratio(status, COLORS.canvas) >= 4.5
    finally:
        apply_parsezen_theme(application, ThemeMode.DARK)


def test_each_theme_has_a_transparent_official_wordmark() -> None:
    for path in (Path(BRAND_LOGO_PATH), Path(BRAND_DARK_LOGO_PATH)):
        with Image.open(path).convert("RGBA") as logo:
            assert logo.getchannel("A").getextrema() == (0, 255)


def test_every_brand_variant_keeps_the_white_outlined_official_icon() -> None:
    with Image.open(APP_ICON_PATH).convert("RGBA") as icon:
        pixels = tuple(icon.get_flattened_data())
        assert icon.getchannel("A").getextrema() == (0, 255)
        assert all(icon.getpixel(point)[3] == 0 for point in ((0, 0), (1023, 0), (0, 1023)))
        assert (
            sum(alpha >= 240 and min(red, green, blue) >= 240 for red, green, blue, alpha in pixels)
            > 250_000
        )

    for path in (Path(BRAND_LOGO_PATH), Path(BRAND_DARK_LOGO_PATH)):
        with Image.open(path).convert("RGBA") as logo:
            icon_area = logo.crop((0, 0, logo.width // 4, logo.height))
            pixels = tuple(icon_area.get_flattened_data())
            assert (
                sum(
                    alpha >= 220 and min(red, green, blue) >= 235
                    for red, green, blue, alpha in pixels
                )
                > 500
            )
            assert (
                sum(
                    alpha >= 220 and blue > red * 1.2 and green > red * 1.2
                    for red, green, blue, alpha in pixels
                )
                > 200
            )


def test_product_ui_colours_are_centralized_in_the_design_system() -> None:
    source_root = Path(__file__).parents[1] / "src" / "parsezen"
    allowed = {
        source_root / "presentation" / "design_system.py",
        # These colours are written into the exported EPUB, not the app UI.
        source_root / "epub_builder.py",
    }
    arbitrary_colours: list[str] = []
    for source in source_root.rglob("*.py"):
        if source in allowed:
            continue
        for match in re.finditer(r"#[0-9A-Fa-f]{3,8}\b", source.read_text(encoding="utf-8")):
            arbitrary_colours.append(f"{source.relative_to(source_root)}:{match.start()}")

    assert arbitrary_colours == []


def test_high_contrast_uses_the_native_palette_for_widgets_and_painters(
    qtbot,
    monkeypatch,
) -> None:
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    monkeypatch.setattr(design_system_module, "is_high_contrast_enabled", lambda: True)

    try:
        apply_parsezen_theme(application, ThemeMode.LIGHT)

        assert application.styleSheet() == ""
        assert application.property("parsezenHighContrast") is True
        assert COLORS.canvas == application.palette().window().color().name().upper()
    finally:
        monkeypatch.setattr(design_system_module, "is_high_contrast_enabled", lambda: False)
        apply_parsezen_theme(application, ThemeMode.DARK)
