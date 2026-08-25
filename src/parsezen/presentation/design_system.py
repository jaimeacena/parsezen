"""Parsezen's semantic design tokens, themes, palette and vector assets.

This is the only module allowed to define product colours.  Presentation
code consumes the semantic roles exposed through :data:`COLORS`.
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass
from enum import StrEnum

from PySide6.QtCore import QPointF, QRectF, QSettings, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QIcon,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QApplication

from parsezen.branding import INTER_FONT_PATH


class ThemeMode(StrEnum):
    SYSTEM = "system"
    LIGHT = "light"
    DARK = "dark"


@dataclass(slots=True)
class ParsezenColors:
    """Resolved semantic colour roles for one theme."""

    canvas: str
    surface: str
    surface_subtle: str
    surface_raised: str
    surface_hover: str
    overlay: str
    overlay_text: str
    text_primary: str
    text_secondary: str
    text_muted: str
    text_inverse: str
    text_disabled: str
    divider: str
    border: str
    border_strong: str
    border_focus: str
    border_error: str
    action_primary: str
    action_primary_hover: str
    action_primary_active: str
    action_primary_soft: str
    action_secondary_hover: str
    action_destructive: str
    action_destructive_hover: str
    info: str
    info_soft: str
    success: str
    success_soft: str
    warning: str
    warning_soft: str
    error: str
    error_soft: str
    disabled: str
    progress_track: str
    table_surface: str
    table_header: str
    table_hover: str
    table_selected: str
    table_border: str
    table_separator: str
    selection: str
    focus_ring: str
    loading: str
    phase_ocr: str
    phase_translation: str
    phase_correction: str
    phase_structure: str
    format_pdf: str
    format_epub: str
    format_docx: str


_LIGHT_COLORS = ParsezenColors(
    canvas="#F3F6F8",
    surface="#FCFDFD",
    surface_subtle="#EEF2F4",
    surface_raised="#FFFFFF",
    surface_hover="#E8F0F2",
    overlay="#0B1F2A",
    overlay_text="#F5F8F9",
    text_primary="#0B1F2A",
    text_secondary="#3D5663",
    text_muted="#5F7480",
    text_inverse="#FFFFFF",
    text_disabled="#71828A",
    divider="#D5DFE3",
    border="#718A95",
    border_strong="#526E7A",
    border_focus="#08767D",
    border_error="#B4232A",
    action_primary="#08767D",
    action_primary_hover="#095E64",
    action_primary_active="#064C51",
    action_primary_soft="#E1F3F3",
    action_secondary_hover="#E8F0F2",
    action_destructive="#B4232A",
    action_destructive_hover="#8F1B21",
    info="#005E8A",
    info_soft="#E3F2FA",
    success="#0B7548",
    success_soft="#E4F4EC",
    warning="#8A4B00",
    warning_soft="#FFF0D8",
    error="#B4232A",
    error_soft="#FBE8E9",
    disabled="#71828A",
    progress_track="#C3D3D8",
    table_surface="#FCFDFD",
    table_header="#EEF2F4",
    table_hover="#F0F5F6",
    table_selected="#E4F1F1",
    table_border="#718A95",
    table_separator="#D5DFE3",
    selection="#D5EBEC",
    focus_ring="#08767D",
    loading="#08767D",
    phase_ocr="#005E8A",
    phase_translation="#08767D",
    phase_correction="#6C3AA0",
    phase_structure="#9A4E00",
    format_pdf="#A52A2A",
    format_epub="#0B7548",
    format_docx="#165D9C",
)
_DARK_COLORS = ParsezenColors(
    canvas="#0C1419",
    surface="#121D23",
    surface_subtle="#19262D",
    surface_raised="#202E35",
    surface_hover="#1D3039",
    overlay="#071117",
    overlay_text="#E7F0F2",
    text_primary="#E7F0F2",
    text_secondary="#B5C4CA",
    text_muted="#8FA3AC",
    text_inverse="#041F26",
    text_disabled="#82959D",
    divider="#2B3D45",
    border="#587684",
    border_strong="#6C8A96",
    border_focus="#5ED1D3",
    border_error="#FF9696",
    action_primary="#35C4C8",
    action_primary_hover="#5ED1D3",
    action_primary_active="#23A4A8",
    action_primary_soft="#14383B",
    action_secondary_hover="#1D3039",
    action_destructive="#FF9696",
    action_destructive_hover="#FFB2B2",
    info="#63C3F0",
    info_soft="#123142",
    success="#63D39A",
    success_soft="#14372A",
    warning="#F6B85E",
    warning_soft="#3A2B16",
    error="#FF9696",
    error_soft="#3D2024",
    disabled="#82959D",
    progress_track="#263A44",
    table_surface="#121D23",
    table_header="#19262D",
    table_hover="#1A2C35",
    table_selected="#172E31",
    table_border="#587684",
    table_separator="#2B3D45",
    selection="#17383B",
    focus_ring="#5ED1D3",
    loading="#5ED1D3",
    phase_ocr="#63C3F0",
    phase_translation="#5ED1D3",
    phase_correction="#C4A0F3",
    phase_structure="#F6B85E",
    format_pdf="#FF9696",
    format_epub="#63D39A",
    format_docx="#7EC8FF",
)
COLORS = ParsezenColors(
    **{
        field_name: getattr(_DARK_COLORS, field_name)
        for field_name in ParsezenColors.__dataclass_fields__
    }
)
_CURRENT_THEME = ThemeMode.DARK
_PREFERRED_THEME = ThemeMode.SYSTEM


@dataclass(frozen=True, slots=True)
class TypographyTokens:
    body_points: int = 10
    body_small_points: int = 9
    label_points: int = 10
    section_points: int = 12
    title_points: int = 18
    display_points: int = 24
    regular_weight: int = 400
    medium_weight: int = 550
    strong_weight: int = 650


@dataclass(frozen=True, slots=True)
class SpacingTokens:
    xxs: int = 2
    xs: int = 4
    sm: int = 8
    md: int = 12
    lg: int = 16
    xl: int = 24
    xxl: int = 32
    xxxl: int = 48


@dataclass(frozen=True, slots=True)
class ControlTokens:
    compact_height: int = 32
    default_height: int = 38
    comfortable_height: int = 44
    minimum_touch_target: int = 40
    icon_small: int = 16
    icon_medium: int = 20
    icon_large: int = 24


@dataclass(frozen=True, slots=True)
class RadiusTokens:
    small: int = 6
    medium: int = 10
    large: int = 14
    pill: int = 999


@dataclass(frozen=True, slots=True)
class BreakpointTokens:
    compact: int = 640
    medium: int = 960
    wide: int = 1280


@dataclass(frozen=True, slots=True)
class MotionTokens:
    fast_ms: int = 100
    normal_ms: int = 180
    slow_ms: int = 280


@dataclass(frozen=True, slots=True)
class ZIndexTokens:
    content: int = 0
    sticky: int = 10
    overlay: int = 100
    modal: int = 200
    toast: int = 300


@dataclass(frozen=True, slots=True)
class ShadowTokens:
    """Elevation recipe values for the few overlays that need depth.

    Qt style sheets do not support shadows directly.  Keeping the recipe here
    prevents individual dialogs from inventing arbitrary values when a
    ``QGraphicsDropShadowEffect`` is genuinely useful.
    """

    none_blur: int = 0
    subtle_blur: int = 12
    subtle_y_offset: int = 2
    subtle_alpha: int = 28
    overlay_blur: int = 28
    overlay_y_offset: int = 8
    overlay_alpha: int = 48


TYPOGRAPHY = TypographyTokens()
SPACING = SpacingTokens()
CONTROLS = ControlTokens()
RADII = RadiusTokens()
BREAKPOINTS = BreakpointTokens()
MOTION = MotionTokens()
Z_INDEX = ZIndexTokens()
SHADOWS = ShadowTokens()
SPACE_1 = 4
SPACE_2 = 8
SPACE_3 = 12
SPACE_4 = 16
SPACE_5 = 24
SPACE_6 = 32
CONTROL_HEIGHT = CONTROLS.default_height
ICON_SMALL = CONTROLS.icon_small
ICON_MEDIUM = CONTROLS.icon_medium
RADIUS_SMALL = RADII.small
RADIUS_MEDIUM = RADII.medium
RADIUS_LARGE = RADII.large
_FONT_FAMILY: str | None = None


def load_application_font() -> str:
    """Load Parsezen's bundled OFL font and return a reliable family name."""

    global _FONT_FAMILY
    if _FONT_FAMILY is not None:
        return _FONT_FAMILY
    font_id = QFontDatabase.addApplicationFont(str(INTER_FONT_PATH))
    families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
    _FONT_FAMILY = families[0] if families else "Segoe UI"
    return _FONT_FAMILY


def settings_icon(*, attention: bool = False) -> QIcon:
    """Create a crisp gear with an optional actionable-attention marker."""

    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.translate(12, 12)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(COLORS.text_primary))
    for angle in range(0, 360, 45):
        painter.save()
        painter.rotate(angle)
        painter.drawRoundedRect(QRectF(-1.6, -9.2, 3.2, 4.2), 0.8, 0.8)
        painter.restore()
    pen = QPen(QColor(COLORS.text_primary), 2.4)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(QRectF(-5.8, -5.8, 11.6, 11.6))
    painter.drawEllipse(QRectF(-1.7, -1.7, 3.4, 3.4))
    if attention:
        painter.resetTransform()
        painter.setPen(QPen(QColor(COLORS.canvas), 1.2))
        painter.setBrush(QColor(COLORS.warning))
        painter.drawEllipse(QRectF(17, 1, 6, 6))
    painter.end()
    return QIcon(pixmap)


def add_documents_icon() -> QIcon:
    """Create a local document-plus icon without suggesting cloud transfer."""

    pixmap = QPixmap(30, 30)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(COLORS.action_primary), 1.8)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    document = QPainterPath()
    document.moveTo(7, 3)
    document.lineTo(18, 3)
    document.lineTo(24, 9)
    document.lineTo(24, 14)
    document.moveTo(7, 3)
    document.lineTo(7, 27)
    document.lineTo(16, 27)
    document.moveTo(18, 3)
    document.lineTo(18, 9)
    document.lineTo(24, 9)
    painter.drawPath(document)
    plus_pen = QPen(QColor(COLORS.action_primary), 2.2)
    plus_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(plus_pen)
    painter.drawLine(21, 17, 21, 27)
    painter.drawLine(16, 22, 26, 22)
    painter.end()
    return QIcon(pixmap)


def cloud_upload_icon() -> QIcon:
    """Create a single-path cloud download icon without overlapping arcs."""

    pixmap = QPixmap(40, 40)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(COLORS.action_primary), 2.2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    cloud = QPainterPath()
    cloud.moveTo(13, 28)
    cloud.lineTo(10, 28)
    cloud.cubicTo(6.7, 28, 5, 25.7, 5, 22.8)
    cloud.cubicTo(5, 19.5, 7.4, 17.1, 10.6, 16.8)
    cloud.cubicTo(12.2, 11.8, 16.1, 8.5, 21, 8.5)
    cloud.cubicTo(26.3, 8.5, 30.4, 12.3, 31.3, 17.3)
    cloud.cubicTo(34.2, 18, 36, 20.2, 36, 23)
    cloud.cubicTo(36, 25.8, 33.8, 28, 31, 28)
    cloud.lineTo(27, 28)
    painter.drawPath(cloud)
    painter.drawLine(QPointF(20, 18), QPointF(20, 33))
    painter.drawLine(QPointF(14.5, 27.5), QPointF(20, 33))
    painter.drawLine(QPointF(25.5, 27.5), QPointF(20, 33))
    painter.end()
    return QIcon(pixmap)


def folder_icon() -> QIcon:
    """Create a compact folder icon for the inherited output destination."""

    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(COLORS.text_secondary), 1.8)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawLine(4, 7, 9, 7)
    painter.drawLine(9, 7, 11, 9)
    painter.drawLine(11, 9, 20, 9)
    painter.drawRoundedRect(QRectF(3.5, 7.5, 17, 12), 2.2, 2.2)
    painter.end()
    return QIcon(pixmap)


def local_ai_icon() -> QIcon:
    """Create a restrained local-compute chip matching the destination icon."""

    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(COLORS.text_secondary), 1.7)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(QRectF(5, 5, 14, 14), 3, 3)
    painter.drawRoundedRect(QRectF(9, 9, 6, 6), 1.5, 1.5)
    for coordinate in (8, 12, 16):
        painter.drawLine(QPointF(coordinate, 2.5), QPointF(coordinate, 5))
        painter.drawLine(QPointF(coordinate, 19), QPointF(coordinate, 21.5))
        painter.drawLine(QPointF(2.5, coordinate), QPointF(5, coordinate))
        painter.drawLine(QPointF(19, coordinate), QPointF(21.5, coordinate))
    painter.end()
    return QIcon(pixmap)


def play_icon() -> QIcon:
    """Create the conventional start glyph used by the primary queue action."""

    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(COLORS.text_inverse))
    path = QPainterPath()
    path.moveTo(8, 5.5)
    path.lineTo(19, 12)
    path.lineTo(8, 18.5)
    path.closeSubpath()
    painter.drawPath(path)
    painter.end()
    return QIcon(pixmap)


def pause_icon() -> QIcon:
    """Create a balanced pause glyph used while a queue run is active."""

    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(COLORS.text_inverse))
    painter.drawRoundedRect(QRectF(7, 5, 3.5, 14), 1.2, 1.2)
    painter.drawRoundedRect(QRectF(13.5, 5, 3.5, 14), 1.2, 1.2)
    painter.end()
    return QIcon(pixmap)


def back_icon() -> QIcon:
    """Create a restrained vector back arrow for internal pages."""

    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(COLORS.text_primary), 1.8)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.drawLine(5, 12, 19, 12)
    painter.drawLine(5, 12, 11, 6)
    painter.drawLine(5, 12, 11, 18)
    painter.end()
    return QIcon(pixmap)


def chevron_icon(*, expanded: bool) -> QIcon:
    """Create a font-independent disclosure chevron."""

    pixmap = QPixmap(18, 18)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(COLORS.text_secondary), 1.7)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    if expanded:
        painter.drawLine(QPointF(4.5, 11), QPointF(9, 6.5))
        painter.drawLine(QPointF(9, 6.5), QPointF(13.5, 11))
    else:
        painter.drawLine(QPointF(4.5, 7), QPointF(9, 11.5))
        painter.drawLine(QPointF(9, 11.5), QPointF(13.5, 7))
    painter.end()
    return QIcon(pixmap)


def editor_icon(name: str) -> QIcon:
    """Draw a compact, font-independent icon for the EPUB editing toolbars."""

    pixmap = QPixmap(20, 20)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(COLORS.text_primary), 1.65)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    if name in {"undo", "redo"}:
        direction = -1 if name == "undo" else 1
        start_x = 10 + direction * 5
        tip_x = 10 + direction * 7
        path = QPainterPath()
        path.moveTo(start_x, 6)
        path.lineTo(tip_x, 10)
        path.lineTo(start_x, 14)
        painter.drawPath(path)
        curve = QPainterPath()
        curve.moveTo(tip_x, 10)
        curve.cubicTo(
            10 + direction * 1,
            5,
            10 - direction * 7,
            7,
            10 - direction * 6,
            15,
        )
        painter.drawPath(curve)
    elif name in {"zoom_out", "zoom_in"}:
        painter.drawLine(QPointF(5, 10), QPointF(15, 10))
        if name == "zoom_in":
            painter.drawLine(QPointF(10, 5), QPointF(10, 15))
    elif name in {"previous", "next"}:
        direction = -1 if name == "previous" else 1
        painter.drawLine(
            QPointF(10 - direction * 4, 4.5),
            QPointF(10 + direction * 2, 10),
        )
        painter.drawLine(
            QPointF(10 + direction * 2, 10),
            QPointF(10 - direction * 4, 15.5),
        )
    elif name == "more":
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(COLORS.text_primary))
        for x in (5, 10, 15):
            painter.drawEllipse(QRectF(x - 1.2, 8.8, 2.4, 2.4))
    elif name in {"move_up", "move_down"}:
        direction = -1 if name == "move_up" else 1
        painter.drawLine(QPointF(10, 4), QPointF(10, 16))
        painter.drawLine(
            QPointF(10, 4 if direction < 0 else 16),
            QPointF(5.5, 8.5 if direction < 0 else 11.5),
        )
        painter.drawLine(
            QPointF(10, 4 if direction < 0 else 16),
            QPointF(14.5, 8.5 if direction < 0 else 11.5),
        )
    elif name in {"outdent", "indent"}:
        direction = -1 if name == "outdent" else 1
        painter.drawLine(QPointF(9, 5), QPointF(17, 5))
        painter.drawLine(QPointF(9, 10), QPointF(17, 10))
        painter.drawLine(QPointF(9, 15), QPointF(17, 15))
        painter.drawLine(
            QPointF(7 if direction < 0 else 3, 7),
            QPointF(3 if direction < 0 else 7, 10),
        )
        painter.drawLine(
            QPointF(3 if direction < 0 else 7, 10),
            QPointF(7 if direction < 0 else 3, 13),
        )
    elif name == "bullet_list":
        painter.setBrush(QColor(COLORS.text_primary))
        painter.setPen(Qt.PenStyle.NoPen)
        for y in (5, 10, 15):
            painter.drawEllipse(QRectF(3, y - 1, 2, 2))
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for y in (5, 10, 15):
            painter.drawLine(QPointF(8, y), QPointF(17, y))
    elif name == "numbered_list":
        font = painter.font()
        font.setPixelSize(6)
        font.setBold(True)
        painter.setFont(font)
        for number, y in enumerate((6, 11, 16), start=1):
            painter.drawText(QRectF(1, y - 4, 5, 6), Qt.AlignmentFlag.AlignCenter, str(number))
            painter.drawLine(QPointF(8, y - 1), QPointF(17, y - 1))
    elif name == "italic":
        painter.drawLine(QPointF(8, 4), QPointF(15, 4))
        painter.drawLine(QPointF(5, 16), QPointF(12, 16))
        painter.drawLine(QPointF(12.5, 4), QPointF(7.5, 16))
    elif name.startswith("align_"):
        widths = {
            "align_left": ((4, 16), (4, 13), (4, 16), (4, 11)),
            "align_center": ((4, 16), (6, 14), (4, 16), (7, 13)),
            "align_right": ((4, 16), (7, 16), (4, 16), (9, 16)),
            "align_justify": ((4, 16), (4, 16), (4, 16), (4, 16)),
        }
        for line_y, (x1, x2) in zip((5, 8.5, 12, 15.5), widths[name], strict=True):
            painter.drawLine(QPointF(x1, line_y), QPointF(x2, line_y))
    elif name == "link":
        painter.save()
        painter.translate(10, 10)
        painter.rotate(-38)
        painter.drawRoundedRect(QRectF(-8, -3, 9, 6), 3, 3)
        painter.drawRoundedRect(QRectF(-1, -3, 9, 6), 3, 3)
        painter.drawLine(QPointF(-3, 0), QPointF(3, 0))
        painter.restore()
    elif name == "clear_formatting":
        painter.save()
        painter.translate(10, 10)
        painter.rotate(-38)
        painter.drawRoundedRect(QRectF(-5.5, -3.2, 11, 6.4), 1.4, 1.4)
        painter.drawLine(QPointF(1.3, -3.2), QPointF(1.3, 3.2))
        painter.restore()
        painter.drawLine(QPointF(4, 17), QPointF(16, 17))
    elif name == "split":
        painter.drawLine(QPointF(3, 5), QPointF(17, 5))
        painter.drawLine(QPointF(3, 8.5), QPointF(17, 8.5))
        painter.drawLine(QPointF(3, 13.5), QPointF(8, 13.5))
        painter.drawLine(QPointF(12, 13.5), QPointF(17, 13.5))
        painter.drawLine(QPointF(10, 11), QPointF(10, 17))
        painter.drawLine(QPointF(10, 17), QPointF(7.5, 14.8))
        painter.drawLine(QPointF(10, 17), QPointF(12.5, 14.8))
    elif name == "merge":
        painter.drawLine(QPointF(3, 5), QPointF(8, 5))
        painter.drawLine(QPointF(3, 15), QPointF(8, 15))
        painter.drawLine(QPointF(17, 5), QPointF(12, 5))
        painter.drawLine(QPointF(17, 15), QPointF(12, 15))
        painter.drawLine(QPointF(8, 5), QPointF(12, 10))
        painter.drawLine(QPointF(8, 15), QPointF(12, 10))
        painter.drawLine(QPointF(12, 10), QPointF(17, 10))
    elif name == "rename":
        body = QPainterPath()
        body.moveTo(4, 14.5)
        body.lineTo(5.2, 10.2)
        body.lineTo(13.2, 2.2)
        body.lineTo(17.8, 6.8)
        body.lineTo(9.8, 14.8)
        body.closeSubpath()
        painter.drawPath(body)
        painter.drawLine(QPointF(4, 16.8), QPointF(16.5, 16.8))
    else:
        painter.end()
        raise ValueError(f"Unknown EPUB editor icon: {name}")

    painter.end()
    return QIcon(pixmap)


def theme_toggle_icon(mode: ThemeMode | None = None) -> QIcon:
    """Show the active appearance with a conventional sun or crescent."""

    active_mode = mode or current_theme_mode()
    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    color = QColor(COLORS.text_primary)
    pen = QPen(color, 1.7)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    if active_mode is ThemeMode.LIGHT:
        painter.drawEllipse(QRectF(8, 8, 8, 8))
        for angle in range(0, 360, 45):
            painter.save()
            painter.translate(12, 12)
            painter.rotate(angle)
            painter.drawLine(0, -9, 0, -7)
            painter.restore()
    else:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QRectF(5, 4, 15, 16))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(COLORS.surface))
        painter.drawEllipse(QRectF(10, 2, 13, 15))
    painter.end()
    return QIcon(pixmap)


def contrast_ratio(foreground: str, background: str) -> float:
    """Return the WCAG relative-luminance contrast ratio for two hex colours."""

    def luminance(value: str) -> float:
        channels = [int(value[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [
            channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
            for channel in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def preferred_theme_mode(settings: QSettings | None = None) -> ThemeMode:
    """Read the persisted appearance preference, safely defaulting to the OS."""

    store = settings or QSettings("Parsezen", "Parsezen")
    raw = str(store.value("appearance/theme", ThemeMode.SYSTEM.value))
    try:
        return ThemeMode(raw)
    except ValueError:
        return ThemeMode.SYSTEM


def resolve_theme_mode(
    mode: ThemeMode,
    application: QApplication | None = None,
) -> ThemeMode:
    """Resolve ``system`` to a concrete light or dark theme."""

    if mode is not ThemeMode.SYSTEM:
        return mode
    app = application or QApplication.instance()
    if isinstance(app, QApplication):
        color_scheme = app.styleHints().colorScheme()
        if color_scheme is Qt.ColorScheme.Dark:
            return ThemeMode.DARK
        if color_scheme is Qt.ColorScheme.Light:
            return ThemeMode.LIGHT
        window_color = app.palette().color(app.palette().ColorRole.Window)
        return ThemeMode.DARK if window_color.lightnessF() < 0.5 else ThemeMode.LIGHT
    return ThemeMode.LIGHT


def is_high_contrast_enabled() -> bool:
    """Return the native Windows high-contrast preference when available."""

    if sys.platform != "win32":
        return False

    class _HighContrast(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_uint),
            ("dwFlags", ctypes.c_uint),
            ("lpszDefaultScheme", ctypes.c_wchar_p),
        ]

    high_contrast = _HighContrast()
    high_contrast.cbSize = ctypes.sizeof(_HighContrast)
    try:
        success = ctypes.windll.user32.SystemParametersInfoW(
            0x0042,
            high_contrast.cbSize,
            ctypes.byref(high_contrast),
            0,
        )
    except (AttributeError, OSError):
        return False
    return bool(success and high_contrast.dwFlags & 0x00000001)


def reduced_motion_enabled() -> bool:
    """Respect the Windows client-animation accessibility preference."""

    if sys.platform != "win32":
        return False
    animations_enabled = ctypes.c_int(1)
    try:
        success = ctypes.windll.user32.SystemParametersInfoW(
            0x1042,
            0,
            ctypes.byref(animations_enabled),
            0,
        )
    except (AttributeError, OSError):
        return False
    return bool(success and not animations_enabled.value)


def current_theme_mode() -> ThemeMode:
    """Return the currently rendered concrete theme."""

    return _CURRENT_THEME


def current_theme_preference() -> ThemeMode:
    """Return the user's persisted theme preference, including ``system``."""

    return _PREFERRED_THEME


def _apply_native_high_contrast_colors(palette: QPalette) -> None:
    """Make custom painters follow the active native high-contrast palette."""

    active = QPalette.ColorGroup.Active
    disabled = QPalette.ColorGroup.Disabled

    def color(role: QPalette.ColorRole, group: QPalette.ColorGroup = active) -> str:
        return palette.color(group, role).name(QColor.NameFormat.HexRgb).upper()

    window = color(QPalette.ColorRole.Window)
    base = color(QPalette.ColorRole.Base)
    alternate = color(QPalette.ColorRole.AlternateBase)
    text = color(QPalette.ColorRole.WindowText)
    field_text = color(QPalette.ColorRole.Text)
    highlighted_text = color(QPalette.ColorRole.HighlightedText)
    highlight = color(QPalette.ColorRole.Highlight)
    disabled_text = color(QPalette.ColorRole.Text, disabled)
    border = color(QPalette.ColorRole.Mid)
    strong_border = color(QPalette.ColorRole.Dark)
    values = {
        "canvas": window,
        "surface": base,
        "surface_subtle": alternate,
        "surface_raised": base,
        "surface_hover": alternate,
        "overlay": color(QPalette.ColorRole.ToolTipBase),
        "overlay_text": color(QPalette.ColorRole.ToolTipText),
        "text_primary": text,
        "text_secondary": field_text,
        "text_muted": field_text,
        "text_inverse": highlighted_text,
        "text_disabled": disabled_text,
        "divider": border,
        "border": border,
        "border_strong": strong_border,
        "border_focus": highlight,
        "border_error": text,
        "action_primary": highlight,
        "action_primary_hover": highlight,
        "action_primary_active": highlight,
        "action_primary_soft": alternate,
        "action_secondary_hover": alternate,
        "action_destructive": text,
        "action_destructive_hover": text,
        "info": text,
        "info_soft": alternate,
        "success": text,
        "success_soft": alternate,
        "warning": text,
        "warning_soft": alternate,
        "error": text,
        "error_soft": alternate,
        "disabled": disabled_text,
        "progress_track": border,
        "table_surface": base,
        "table_header": alternate,
        "table_hover": alternate,
        "table_selected": highlight,
        "table_border": border,
        "table_separator": strong_border,
        "selection": highlight,
        "focus_ring": highlight,
        "loading": highlight,
        "phase_ocr": text,
        "phase_translation": text,
        "phase_correction": text,
        "phase_structure": text,
        "format_pdf": text,
        "format_epub": text,
        "format_docx": text,
    }
    for field_name, value in values.items():
        setattr(COLORS, field_name, value)


def apply_parsezen_theme(
    application: QApplication,
    mode: ThemeMode = ThemeMode.SYSTEM,
    *,
    persist: bool = False,
) -> None:
    """Apply one centralized WCAG-oriented theme without reloading the app."""

    global _CURRENT_THEME, _PREFERRED_THEME
    resolved_mode = resolve_theme_mode(mode, application)
    high_contrast = is_high_contrast_enabled()
    if persist:
        QSettings("Parsezen", "Parsezen").setValue("appearance/theme", mode.value)
    if (
        application.property("parsezenTheme") == resolved_mode.value
        and bool(application.property("parsezenHighContrast")) is high_contrast
        and _PREFERRED_THEME is mode
    ):
        return
    source = _DARK_COLORS if resolved_mode is ThemeMode.DARK else _LIGHT_COLORS
    for field_name in ParsezenColors.__dataclass_fields__:
        setattr(COLORS, field_name, getattr(source, field_name))
    _CURRENT_THEME = resolved_mode
    _PREFERRED_THEME = mode

    font = QFont(load_application_font(), TYPOGRAPHY.body_points)
    application.setFont(font)
    application.setProperty("parsezenTheme", resolved_mode.value)
    application.setProperty("parsezenReducedMotion", reduced_motion_enabled())
    application.setProperty("parsezenHighContrast", high_contrast)
    if high_contrast:
        native_palette = application.style().standardPalette()
        application.setStyleSheet("")
        application.setPalette(native_palette)
        _apply_native_high_contrast_colors(native_palette)
        return
    application.setStyleSheet(
        f"""
        QWidget {{
            color: {COLORS.text_primary};
            background-color: transparent;
        }}
        QMainWindow, QWidget#parsezenPage {{
            background-color: {COLORS.canvas};
        }}
        QScrollArea, QScrollArea > QWidget > QWidget {{
            background-color: transparent;
        }}
        QDialog {{
            background-color: {COLORS.canvas};
        }}
        QLabel#dialogTitle {{
            color: {COLORS.text_primary};
            font-size: 18pt;
            font-weight: 650;
        }}
        QLabel#sectionTitle {{
            color: {COLORS.text_primary};
            font-size: {TYPOGRAPHY.section_points}pt;
            font-weight: 650;
        }}
        QLabel[secondary="true"] {{
            color: {COLORS.text_secondary};
        }}
        QLabel[muted="true"] {{
            color: {COLORS.text_muted};
        }}
        QLabel#reviewProgress {{
            color: {COLORS.text_secondary};
            font-size: 10.5pt;
        }}
        QLabel#reviewPrioritySummary {{
            color: {COLORS.text_secondary};
            font-size: 10pt;
        }}
        QLabel#reviewTitle {{
            color: {COLORS.text_primary};
            font-size: 16pt;
            font-weight: 650;
        }}
        QLabel#reviewHelp, QLabel#qualitySummary,
        QLabel#reviewProgressLabel, QLabel#reviewCategoryLabel {{
            color: {COLORS.text_secondary};
        }}
        QLabel#qualityIssueMessage, QLabel#reviewWarning {{
            color: {COLORS.warning};
        }}
        QLabel#preflightSummary, QLabel#preflightDocumentName {{
            color: {COLORS.text_primary};
            font-weight: 650;
        }}
        QLabel#preflightDocumentName {{
            font-size: {TYPOGRAPHY.section_points}pt;
        }}
        QLabel#preflightSectionTitle {{
            color: {COLORS.text_primary};
            font-weight: 650;
            margin-top: {SPACING.sm}px;
        }}
        QLabel#preflightDuration {{
            color: {COLORS.text_primary};
            font-size: 13pt;
            font-weight: 650;
        }}
        QLabel#preflightFlow {{
            color: {COLORS.text_primary};
            padding-top: {SPACING.xs}px;
        }}
        QFrame#preflightDocument {{
            background-color: transparent;
            border: none;
            border-bottom: 1px solid {COLORS.divider};
        }}
        QFrame#preflightTechnicalDetails {{
            background-color: transparent;
            border: none;
            border-top: 1px solid {COLORS.divider};
        }}
        QPushButton#preflightDetailsToggle {{
            color: {COLORS.text_secondary};
            background-color: transparent;
            border: none;
            padding: {SPACING.xs}px 0;
            min-height: 32px;
        }}
        QPushButton#preflightDetailsToggle:hover {{
            color: {COLORS.text_primary};
            background-color: transparent;
        }}
        QPushButton#preflightDetailsToggle:focus {{
            border: 2px solid {COLORS.focus_ring};
            border-radius: {RADIUS_SMALL}px;
        }}
        QLabel#preflightFinding[severity="attention"] {{
            color: {COLORS.warning};
        }}
        QLabel#preflightFinding[severity="high"] {{
            color: {COLORS.error};
        }}
        QFrame#statusMessage {{
            color: {COLORS.info};
            background-color: {COLORS.info_soft};
            border: none;
            border-radius: {RADIUS_MEDIUM}px;
        }}
        QFrame#statusMessage[tone="success"] {{
            color: {COLORS.success};
            background-color: {COLORS.success_soft};
        }}
        QFrame#statusMessage[tone="warning"] {{
            color: {COLORS.warning};
            background-color: {COLORS.warning_soft};
        }}
        QFrame#statusMessage[tone="error"] {{
            color: {COLORS.error};
            background-color: {COLORS.error_soft};
        }}
        QFrame#statusMessage:focus {{
            border: 2px solid {COLORS.focus_ring};
        }}
        QWidget#configurationOptionRow {{
            min-height: 44px;
            background-color: transparent;
            border: none;
            border-radius: {RADIUS_SMALL}px;
        }}
        QWidget#configurationOptionRow:hover {{
            background-color: {COLORS.surface_hover};
        }}
        QWidget#configurationOptionRow:focus {{
            background-color: {COLORS.action_primary_soft};
        }}
        QFrame#configurationChoice {{
            min-height: 58px;
            background-color: {COLORS.surface};
            border: 1px solid {COLORS.border};
            border-radius: {RADIUS_MEDIUM}px;
        }}
        QFrame#configurationChoice:hover {{
            background-color: {COLORS.surface_hover};
        }}
        QFrame#configurationChoice[selected="true"] {{
            background-color: {COLORS.action_primary_soft};
            border: 2px solid {COLORS.action_primary};
        }}
        QFrame#componentSetupCard {{
            background-color: {COLORS.surface};
            border: 1px solid {COLORS.border};
            border-radius: {RADIUS_MEDIUM}px;
        }}
        QFrame#componentSetupCard:hover {{
            background-color: {COLORS.surface_hover};
        }}
        QFrame#componentSetupCard[status="prepared"] {{
            border-color: {COLORS.success};
        }}
        QFrame#componentSetupCard[status="downloadable"] {{
            border-color: {COLORS.info};
        }}
        QFrame#componentSetupCard[status="insufficient"] {{
            border-color: {COLORS.border};
        }}
        QLabel#componentTitle {{
            color: {COLORS.text_primary};
            background-color: transparent;
            font-weight: 650;
        }}
        QLabel#componentDescription, QLabel#componentDetail, QLabel#componentSetupHelp {{
            color: {COLORS.text_secondary};
            background-color: transparent;
        }}
        QLabel#componentStatus {{
            color: {COLORS.text_secondary};
            background-color: transparent;
            font-weight: 650;
        }}
        QLabel#componentStatus[status="prepared"] {{
            color: {COLORS.success};
        }}
        QLabel#componentStatus[status="downloadable"] {{
            color: {COLORS.info};
        }}
        QLabel#componentStatus[status="insufficient"] {{
            color: {COLORS.warning};
        }}
        QPushButton#componentDownloadButton,
        QPushButton#componentRefreshButton {{
            min-height: {CONTROLS.default_height}px;
        }}
        QPushButton#componentDownloadButton {{
            color: {COLORS.text_inverse};
            background-color: {COLORS.action_primary};
            border-color: {COLORS.action_primary};
            font-weight: 600;
        }}
        QPushButton#componentDownloadButton:hover {{
            background-color: {COLORS.action_primary_hover};
            border-color: {COLORS.action_primary_hover};
        }}
        QLabel#choiceTitle {{
            color: {COLORS.text_primary};
            background-color: transparent;
            font-weight: 650;
        }}
        QLabel#choiceDescription {{
            color: {COLORS.text_secondary};
            background-color: transparent;
        }}
        QWidget#configurationSwitchRow {{
            min-height: 44px;
            background-color: transparent;
            border: none;
        }}
        QLabel#configurationOptionLabel {{
            color: {COLORS.text_primary};
            background-color: transparent;
            font-weight: 600;
        }}
        QLabel#configurationOptionValue {{
            color: {COLORS.text_secondary};
            background-color: transparent;
        }}
        QLabel#configurationOptionChevron {{
            min-width: 16px;
            color: {COLORS.text_muted};
            background-color: transparent;
            font-size: 18px;
        }}
        QLabel#configurationValidation {{
            color: {COLORS.error};
            background-color: {COLORS.error_soft};
            border: none;
            border-radius: {RADIUS_SMALL}px;
            padding: 8px;
        }}
        QFrame#statusMessage QLabel#statusMessageIcon {{
            min-width: 24px;
            max-width: 24px;
            min-height: 24px;
            max-height: 24px;
            border: none;
            font-weight: 700;
        }}
        QLabel#brandLogo {{
            background: transparent;
        }}
        QGroupBox {{
            margin-top: 14px;
            padding: 16px 0 0 0;
            color: {COLORS.text_primary};
            background-color: transparent;
            border: none;
            font-weight: 600;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 0;
            padding: 0;
            background-color: transparent;
        }}
        QPushButton {{
            min-height: {CONTROL_HEIGHT}px;
            padding: 0 16px;
            color: {COLORS.text_primary};
            background-color: {COLORS.surface_raised};
            border: 1px solid {COLORS.divider};
            border-radius: {RADIUS_SMALL}px;
        }}
        QPushButton:hover {{
            background-color: {COLORS.action_secondary_hover};
        }}
        QPushButton:focus {{
            background-color: {COLORS.action_primary_soft};
            border-color: {COLORS.divider};
        }}
        QPushButton:disabled {{
            color: {COLORS.text_disabled};
            border-color: {COLORS.divider};
            background-color: {COLORS.surface_subtle};
        }}
        QPushButton#primaryAction {{
            color: {COLORS.text_inverse};
            background-color: {COLORS.action_primary};
            border-color: {COLORS.action_primary};
            font-weight: 600;
        }}
        QPushButton#primaryAction:hover {{
            background-color: {COLORS.action_primary_hover};
            border-color: {COLORS.action_primary_hover};
        }}
        QPushButton#primaryAction:pressed {{
            background-color: {COLORS.action_primary_active};
            border-color: {COLORS.action_primary_active};
        }}
        QPushButton#process_button {{
            color: {COLORS.text_inverse};
            background-color: {COLORS.action_primary};
            border-color: {COLORS.action_primary};
            font-weight: 600;
        }}
        QPushButton#process_button:hover {{
            background-color: {COLORS.action_primary_hover};
            border-color: {COLORS.action_primary_hover};
        }}
        QPushButton#glossaryButton,
        QPushButton#aiRefreshButton,
        QPushButton#model_add_button {{
            min-width: {CONTROLS.compact_height - 2}px;
            max-width: {CONTROLS.compact_height - 2}px;
            min-height: {CONTROLS.compact_height - 2}px;
            max-height: {CONTROLS.compact_height - 2}px;
            padding: 0;
        }}
        QPushButton#batchPageButton {{
            min-height: 22px;
            max-height: 22px;
            padding-top: 0;
            padding-bottom: 0;
        }}
        QToolButton {{
            min-width: 36px;
            min-height: 36px;
            color: {COLORS.text_primary};
            background-color: transparent;
            border: 1px solid transparent;
            border-radius: {RADIUS_SMALL}px;
        }}
        QToolButton:hover {{
            color: {COLORS.text_primary};
            background-color: {COLORS.surface_hover};
        }}
        QToolButton:focus {{
            background-color: {COLORS.action_primary_soft};
            border-color: transparent;
        }}
        QToolButton#reviewPaneMore {{
            min-width: 32px;
            max-width: 32px;
            min-height: 32px;
            max-height: 32px;
            padding: 0;
            font-size: 16px;
        }}
        QToolButton#reviewPaneMore::menu-indicator {{
            image: none;
        }}
        QLineEdit, QComboBox, QSpinBox, QTextEdit, QPlainTextEdit, QTreeView {{
            min-height: {CONTROL_HEIGHT}px;
            padding: 0 11px;
            color: {COLORS.text_primary};
            selection-color: {COLORS.text_primary};
            selection-background-color: {COLORS.selection};
            background-color: {COLORS.surface};
            border: 1px solid {COLORS.border_strong};
            border-radius: {RADIUS_SMALL}px;
        }}
        QTextEdit, QPlainTextEdit, QTreeView {{
            padding: 10px;
        }}
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus,
        QTextEdit:focus, QPlainTextEdit:focus, QTreeView:focus {{
            border: 2px solid {COLORS.focus_ring};
        }}
        QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
            color: {COLORS.text_disabled};
            background-color: {COLORS.surface_subtle};
            border-color: {COLORS.border};
        }}
        QComboBox::drop-down {{
            width: 30px;
            border: none;
        }}
        QComboBox#model_choice_combo,
        QComboBox#context_window_combo {{
            min-height: {CONTROLS.compact_height - 2}px;
            max-height: {CONTROLS.compact_height - 2}px;
        }}
        QComboBox QAbstractItemView {{
            padding: 5px;
            color: {COLORS.text_primary};
            background-color: {COLORS.surface};
            border: 1px solid {COLORS.border};
            outline: none;
            selection-background-color: {COLORS.selection};
        }}
        QCheckBox, QRadioButton {{
            min-height: 28px;
            spacing: 9px;
            color: {COLORS.text_primary};
        }}
        QCheckBox::indicator {{
            width: 16px;
            height: 16px;
            background-color: {COLORS.surface_subtle};
            border: 1px solid {COLORS.border_strong};
            border-radius: 4px;
        }}
        QCheckBox::indicator:hover {{
            border-color: {COLORS.action_primary};
        }}
        QCheckBox::indicator:checked {{
            background-color: {COLORS.action_primary};
            border: 2px solid {COLORS.action_primary_hover};
        }}
        QRadioButton::indicator {{
            width: 16px;
            height: 16px;
            background-color: {COLORS.surface_subtle};
            border: 2px solid {COLORS.border_strong};
            border-radius: 9px;
        }}
        QRadioButton::indicator:hover {{
            border-color: {COLORS.action_primary};
        }}
        QRadioButton::indicator:checked {{
            background-color: {COLORS.action_primary};
            border: 4px solid {COLORS.surface_subtle};
        }}
        QCheckBox:disabled, QRadioButton:disabled {{
            color: {COLORS.text_disabled};
        }}
        QFrame#reviewPane {{
            background-color: {COLORS.surface};
            border: none;
            border-radius: {RADIUS_SMALL}px;
        }}
        QFrame#reviewPane[selected="true"] {{
            background-color: {COLORS.action_primary_soft};
            border: none;
        }}
        QFrame#revisionOriginalPane, QFrame#revisionProposalPane,
        QFrame#epubStructurePane, QFrame#epubPreviewPane {{
            background-color: {COLORS.surface};
            border: none;
            border-radius: {RADIUS_SMALL}px;
        }}
        QPushButton#reviewChoiceButton:checked {{
            color: {COLORS.action_primary_hover};
            background-color: {COLORS.action_primary_soft};
            border-color: {COLORS.action_primary};
        }}
        QFrame#reviewPane QLabel#reviewPaneTitle {{
            color: {COLORS.text_primary};
            font-size: 11.5pt;
            font-weight: 650;
            border: none;
        }}
        QFrame#reviewPane QPlainTextEdit {{
            background-color: {COLORS.surface_raised};
        }}
        QDialog#modelManagerDialog, QDialog#glossaryDialog,
        QDialog#diagnosticsDialog {{
            background-color: {COLORS.canvas};
        }}
        QFrame#localAiOverview {{
            background-color: {COLORS.surface_subtle};
            border: none;
            border-radius: {RADIUS_SMALL}px;
        }}
        QLabel#localAiStatus {{
            color: {COLORS.text_primary};
        }}
        QPushButton#localAiConnectionAction {{
            min-height: 34px;
            max-height: 34px;
            color: {COLORS.action_primary_hover};
        }}
        QScrollArea#modelManagerScroll, QWidget#modelManagerBody {{
            background-color: {COLORS.canvas};
            border: none;
        }}
        QLabel#modelHardwareCheck {{
            min-width: 22px;
            max-width: 22px;
            min-height: 22px;
            max-height: 22px;
            color: {COLORS.success};
            background-color: {COLORS.success_soft};
            border: none;
            border-radius: 11px;
            font-weight: 700;
        }}
        QLabel#modelHardwareLabel, QLabel#modelReason,
        QLabel#modelDetails, QLabel#modelTechnicalDetails,
        QLabel#modelManagerHelp, QLabel#modelEmptyLabel {{
            color: {COLORS.text_secondary};
        }}
        QLabel#modelName {{
            color: {COLORS.text_primary};
            font-size: 11pt;
            font-weight: 650;
        }}
        QFrame#recommendedModelCard, QFrame#alternativeModelCard,
        QFrame#installedModelCard {{
            background-color: transparent;
            border: none;
            border-bottom: 1px solid {COLORS.divider};
            border-radius: 0;
        }}
        QFrame#recommendedModelCard {{
            background-color: {COLORS.action_primary_soft};
        }}
        QFrame#recommendedModelCard:hover, QFrame#alternativeModelCard:hover,
        QFrame#installedModelCard:hover {{
            background-color: {COLORS.surface_hover};
        }}
        QFrame#modelAccordion, QWidget#modelAccordionHeaderRow,
        QWidget#modelAccordionBody {{
            background-color: transparent;
            border: none;
        }}
        QPushButton#modelAccordionHeader {{
            padding: 0;
            text-align: left;
            background-color: transparent;
            border: none;
            font-size: 11pt;
            font-weight: 650;
        }}
        QPushButton#modelCatalogLink {{
            color: {COLORS.action_primary_hover};
            background-color: transparent;
            border: none;
            text-align: left;
        }}
        QPushButton[modelAction="true"] {{
            min-width: 118px;
        }}
        QPushButton[currentModel="true"] {{
            color: {COLORS.success};
            background-color: {COLORS.success_soft};
            border-color: transparent;
        }}
        QPushButton[dangerAction="true"] {{
            color: {COLORS.text_primary};
            border-color: {COLORS.divider};
        }}
        QPushButton[dangerAction="true"]:hover {{
            color: {COLORS.action_destructive};
            background-color: {COLORS.error_soft};
            border-color: {COLORS.action_destructive_hover};
        }}
        QProgressBar {{
            min-height: 8px;
            max-height: 8px;
            color: transparent;
            background-color: {COLORS.progress_track};
            border: none;
            border-radius: 4px;
        }}
        QProgressBar::chunk {{
            background-color: {COLORS.action_primary};
            border-radius: 4px;
        }}
        QTableWidget#glossaryTable, QListWidget#recentJobsList,
        QPlainTextEdit#diagnosticsReport {{
            color: {COLORS.text_primary};
            background-color: {COLORS.surface};
            border: 1px solid {COLORS.divider};
            border-radius: {RADIUS_SMALL}px;
        }}
        QLabel#activityHelp, QLabel#activityStatus, QLabel#activitySummary,
        QLabel#activityEmpty {{
            color: {COLORS.text_secondary};
        }}
        QLabel#activityTitle {{
            color: {COLORS.text_primary};
            font-size: 11pt;
            font-weight: 650;
        }}
        QLabel#activityFailureHeading {{
            color: {COLORS.text_primary};
            font-weight: 650;
            margin-top: {SPACING.xs}px;
        }}
        QLabel#activityFailureMessage, QLabel#activityReusableWork,
        QLabel#activityTimeline {{
            color: {COLORS.text_primary};
        }}
        QLabel#activityReference, QLabel#activityRecoveryNote,
        QLabel#activityFeedback {{
            color: {COLORS.text_secondary};
        }}
        QFrame#activityHistoryPanel, QFrame#activityDetails {{
            background-color: {COLORS.surface_raised};
            border: 1px solid {COLORS.divider};
            border-radius: {RADIUS_SMALL}px;
        }}
        QSplitter#activityPanels::handle {{
            background-color: transparent;
        }}
        QListWidget#recentJobsList {{
            background-color: transparent;
            border: none;
        }}
        QListWidget#recentJobsList::item {{
            min-height: 54px;
            padding: 8px 10px;
            border-radius: {RADIUS_SMALL}px;
        }}
        QListWidget {{
            color: {COLORS.text_primary};
            background-color: {COLORS.surface_raised};
            border: 1px solid {COLORS.divider};
            border-radius: {RADIUS_SMALL}px;
            outline: none;
        }}
        QListWidget::item {{
            min-height: 34px;
            padding: 3px 8px;
            border-bottom: 1px solid {COLORS.divider};
        }}
        QListWidget::item:hover {{
            background-color: {COLORS.surface_hover};
        }}
        QListWidget::item:selected {{
            color: {COLORS.text_primary};
            background-color: {COLORS.table_selected};
        }}
        QTableWidget QHeaderView::section {{
            color: {COLORS.text_secondary};
            background-color: {COLORS.surface_subtle};
            border: none;
            border-bottom: 1px solid {COLORS.divider};
            padding: 6px 8px;
        }}
        QMenu {{
            padding: 6px;
            color: {COLORS.text_primary};
            background-color: {COLORS.surface_raised};
            border: 1px solid {COLORS.divider};
        }}
        QMenu::item {{
            min-height: 30px;
            padding: 4px 24px 4px 10px;
            border-radius: {RADIUS_SMALL}px;
        }}
        QMenu::item:selected {{
            color: {COLORS.action_primary_hover};
            background-color: {COLORS.action_primary_soft};
        }}
        QMenu::item:disabled {{
            color: {COLORS.text_disabled};
        }}
        QTabWidget::pane {{
            border: none;
            background-color: transparent;
        }}
        QTabBar::tab {{
            min-height: 36px;
            padding: 0 14px;
            color: {COLORS.text_secondary};
            background-color: transparent;
            border-bottom: 2px solid transparent;
        }}
        QTabBar::tab:selected {{
            color: {COLORS.text_primary};
            border-bottom-color: {COLORS.action_primary};
        }}
        QTabBar::tab:hover {{
            color: {COLORS.text_primary};
            background-color: {COLORS.action_secondary_hover};
        }}
        QTabBar::tab:focus {{
            color: {COLORS.text_primary};
            background-color: {COLORS.action_primary_soft};
            border-bottom-color: {COLORS.action_primary};
        }}
        QSplitter::handle {{
            background-color: {COLORS.divider};
        }}
        QSplitter::handle:horizontal {{
            width: 1px;
            margin: 0 8px;
        }}
        QTableView#jobTable {{
            color: {COLORS.text_primary};
            background-color: {COLORS.table_surface};
            alternate-background-color: {COLORS.table_surface};
            border: none;
            border-radius: 12px;
            gridline-color: transparent;
            outline: none;
            selection-background-color: {COLORS.table_selected};
            selection-color: {COLORS.text_primary};
        }}
        QAbstractItemView:focus {{
            border: none;
        }}
        QHeaderView#jobTableHeader,
        QHeaderView#jobTableHeader::section {{
            color: {COLORS.text_primary};
            background-color: {COLORS.table_header};
            border: none;
        }}
        QScrollBar:vertical {{
            width: 10px;
            margin: 4px 2px;
            background: transparent;
        }}
        QScrollBar::handle:vertical {{
            min-height: 30px;
            border-radius: 4px;
            background: {COLORS.border_strong};
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
            height: 0;
        }}
        QScrollBar:horizontal {{
            height: 10px;
            margin: 2px 4px;
            background: transparent;
        }}
        QScrollBar::handle:horizontal {{
            min-width: 30px;
            border-radius: 4px;
            background: {COLORS.border_strong};
        }}
        QScrollBar::handle:vertical:hover,
        QScrollBar::handle:horizontal:hover {{
            background: {COLORS.text_muted};
        }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
            width: 0;
        }}
        QToolTip {{
            color: {COLORS.overlay_text};
            background-color: {COLORS.overlay};
            border: 1px solid {COLORS.divider};
            padding: 6px;
        }}
        """
    )
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(COLORS.canvas))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(COLORS.text_primary))
    palette.setColor(QPalette.ColorRole.Base, QColor(COLORS.surface))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(COLORS.surface_subtle))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(COLORS.overlay))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(COLORS.overlay_text))
    palette.setColor(QPalette.ColorRole.Button, QColor(COLORS.surface_raised))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(COLORS.text_primary))
    palette.setColor(QPalette.ColorRole.Text, QColor(COLORS.text_primary))
    palette.setColor(QPalette.ColorRole.BrightText, QColor(COLORS.text_inverse))
    palette.setColor(QPalette.ColorRole.Link, QColor(COLORS.info))
    palette.setColor(QPalette.ColorRole.LinkVisited, QColor(COLORS.phase_correction))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(COLORS.text_muted))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(COLORS.selection))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(COLORS.text_primary))
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(COLORS.text_disabled))
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.Base,
        QColor(COLORS.surface_subtle),
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.Button,
        QColor(COLORS.surface_subtle),
    )
    application.setPalette(palette)
