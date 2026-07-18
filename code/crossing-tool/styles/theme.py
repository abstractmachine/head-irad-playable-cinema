"""Shared visual language tokens and helpers for Crossing visualizers.

This module is part of the Visualizer Framework public surface. It defines a
common interaction language (spacing, typography, control states, accents,
scroll behavior) so different visualizers feel like one system.

Visualizers should compose with these tokens/helpers rather than inventing
parallel styles per tool.

Design language:
  - Background : 50% grey   (#808080)
  - Lines/text : white       (#ffffff)
  - UI borders  : 25% grey   (#404040)  — splitter handles, group-box and
                              section frames  (controlled by UI_BORDER)
  - Highlight   : fuchsia    (#ff00ff)
  - Typography  : Hanken Grotesk for UI chrome, Roboto Mono for data / info fields
  - Single base font size; individual components may adjust later.

Usage (in each visualizer's public launcher function)::

    from styles import theme

    app = QApplication(sys.argv)
    theme.apply_theme(app)

    # For widgets that should use the monospace face explicitly:
    widget.setFont(theme.font_mono())
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Color tokens
# ---------------------------------------------------------------------------

BG           = "#808080"   # 50% grey — main window / widget background
PANEL_BG     = "#6e6e6e"   # slightly darker for control-panel areas
INPUT_BG     = "#606060"   # input fields, list backgrounds
TITLE_BG     = "#4d4d4d"   # section titlebar backgrounds (slightly darker than TAB_BG)
CELL_BG      = "#606060"   # table cell backgrounds (same as TITLE_BG for now)
BTN_BG       = "#606060"   # button face
BTN_HOVER    = "#8d8d8d"   # button hover (lighter than BG)
BTN_PRESSED  = "#565656"   # button pressed (darker)
BTN_H        = 24          # fixed height for all buttons (px))
TEXT         = "#ffffff"   # primary text — white
TEXT_DIM     = "#909090"   # secondary / hint text
BORDER       = "#ffffff"   # interactive element borders (buttons, inputs) — white
UI_BORDER    = "#404040"   # structural chrome borders (group boxes, frames) — 25% grey
SPLITTER     = "#737373"   # splitter drag handles — 45% grey (barely visible on BG)
ACCENT       = "#ffff00"   # selections, active, checked states
ACCENT_TEXT  = "#000000"   # text on ACCENT background (black on yellow; use #ffffff for dark accents)
CANVAS_BG    = "#3a3a3a"   # video / image display areas (dark so content pops)
TAB_BG       = "#545454"   # tab labels + pane content — 10 % lighter than CANVAS_BG
# ---------------------------------------------------------------------------
# Typography
# ---------------------------------------------------------------------------

BASE_PT      = 8           # single base point size for all UI elements
SUBTITLE_PT  = 12          # subtitle overlay — larger for readability

FAMILY_UI    = "Geist"       # buttons, labels, combos, window chrome
FAMILY_MONO  = "Geist Mono"  # lists, text areas, info / data labels
WEIGHT_UI    = 500           # font-weight for UI chrome (100–900)
WEIGHT_MONO  = 500           # font-weight for monospaced data fields

# ---------------------------------------------------------------------------
# Font registration
# ---------------------------------------------------------------------------

_FONTS_DIR   = Path(__file__).parent / "fonts"

_FONT_FILES  = [
    # Geist (UI)
    _FONTS_DIR / "Geist"      / "static" / "Geist-Regular.ttf",
    _FONTS_DIR / "Geist"      / "static" / "Geist-Italic.ttf",
    _FONTS_DIR / "Geist"      / "static" / "Geist-Light.ttf",
    _FONTS_DIR / "Geist"      / "static" / "Geist-LightItalic.ttf",
    _FONTS_DIR / "Geist"      / "static" / "Geist-Medium.ttf",
    _FONTS_DIR / "Geist"      / "static" / "Geist-MediumItalic.ttf",
    _FONTS_DIR / "Geist"      / "static" / "Geist-SemiBold.ttf",
    _FONTS_DIR / "Geist"      / "static" / "Geist-Bold.ttf",
    _FONTS_DIR / "Geist"      / "static" / "Geist-BoldItalic.ttf",
    # Geist Mono
    _FONTS_DIR / "Geist_Mono" / "static" / "GeistMono-Regular.ttf",
    _FONTS_DIR / "Geist_Mono" / "static" / "GeistMono-Italic.ttf",
    _FONTS_DIR / "Geist_Mono" / "static" / "GeistMono-Light.ttf",
    _FONTS_DIR / "Geist_Mono" / "static" / "GeistMono-LightItalic.ttf",
    _FONTS_DIR / "Geist_Mono" / "static" / "GeistMono-Medium.ttf",
    _FONTS_DIR / "Geist_Mono" / "static" / "GeistMono-MediumItalic.ttf",
    _FONTS_DIR / "Geist_Mono" / "static" / "GeistMono-SemiBold.ttf",
    _FONTS_DIR / "Geist_Mono" / "static" / "GeistMono-Bold.ttf",
    _FONTS_DIR / "Geist_Mono" / "static" / "GeistMono-BoldItalic.ttf",
]


def _register_fonts() -> None:
    """Load bundled font files into Qt's font database (idempotent)."""
    from PyQt5.QtGui import QFontDatabase
    for path in _FONT_FILES:
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))


# ---------------------------------------------------------------------------
# QFont helpers
# ---------------------------------------------------------------------------

def font_ui(bold: bool = False) -> "QFont":
    """Return a Roboto QFont at BASE_PT (UI chrome)."""
    from PyQt5.QtGui import QFont
    f = QFont(FAMILY_UI, BASE_PT)
    if bold:
        f.setBold(True)
    return f


def font_mono(bold: bool = False) -> "QFont":
    """Return a Roboto Mono QFont at BASE_PT (data / info fields)."""
    from PyQt5.QtGui import QFont
    f = QFont(FAMILY_MONO, BASE_PT)
    if bold:
        f.setBold(True)
    return f


def font_subtitle() -> "QFont":
    """Return a Hanken Grotesk QFont at SUBTITLE_PT (subtitle overlay)."""
    from PyQt5.QtGui import QFont
    return QFont(FAMILY_UI, SUBTITLE_PT)


# ---------------------------------------------------------------------------
# Stylesheet
# ---------------------------------------------------------------------------

_STYLESHEET = f"""
/* ── Base ─────────────────────────────────────────────────── */
QWidget {{
    background-color: {BG};
    color: {TEXT};
    font-family: "{FAMILY_UI}";
    font-size: {BASE_PT}pt;
}}

/* ── Labels ────────────────────────────────────────────────── */
QLabel {{
    background-color: transparent;
    color: {TEXT};
}}

/* ── Buttons ───────────────────────────────────────────────── */
QPushButton {{
    background-color: {BTN_BG};
    color: {TEXT};
    border: none;
    padding: 0 10px;
    border-radius: 3px;
    min-height: {BTN_H}px;
    max-height: {BTN_H}px;
    font-size: {BASE_PT + 1}pt;
}}
QPushButton:hover    {{ background-color: {BTN_HOVER}; }}
QPushButton:pressed  {{ background-color: {BTN_PRESSED}; }}
QPushButton:checked  {{ background-color: {ACCENT}; color: {ACCENT_TEXT}; }}
QPushButton:disabled {{ color: {TEXT_DIM}; }}

/* ── Combo boxes & line edits ──────────────────────────────── */
QComboBox, QLineEdit {{
    background-color: {INPUT_BG};
    color: {TEXT};
    border: none;
    padding: 3px 6px;
    border-radius: 3px;
}}
QComboBox::drop-down {{ border: none; }}
QComboBox QAbstractItemView {{
    background-color: {INPUT_BG};
    color: {TEXT};
    selection-background-color: {ACCENT};
    selection-color: {ACCENT_TEXT};
}}

/* ── List widgets ──────────────────────────────────────────── */
QListWidget {{
    background-color: {INPUT_BG};
    color: {TEXT};
    border: none;
    font-family: "{FAMILY_MONO}";
    font-size: {BASE_PT}pt;
}}
QListWidget::item:selected       {{ background-color: {ACCENT}; color: {ACCENT_TEXT}; }}
QListWidget::item:hover          {{ background-color: {BTN_HOVER}; }}
QListWidget::item:selected:hover {{ background-color: {ACCENT}; color: {ACCENT_TEXT}; }}

/* ── Text areas ────────────────────────────────────────────── */
QTextEdit {{
    background-color: {INPUT_BG};
    color: {TEXT};
    border: none;
    font-family: "{FAMILY_MONO}";
    font-size: {BASE_PT}pt;
}}

/* ── Sliders ───────────────────────────────────────────────── */
QSlider::groove:horizontal {{
    background: {BTN_PRESSED};
    height: 6px;
    border-radius: 3px;
}}
QSlider::handle:horizontal {{
    background: {TEXT};
    width: 14px;
    height: 14px;
    margin: -4px 0;
    border-radius: 7px;
}}
QSlider::sub-page:horizontal {{
    background: {ACCENT};
    border-radius: 3px;
}}

/* ── Scroll bars ───────────────────────────────────────────── */
QScrollBar:vertical {{
    background: {CANVAS_BG};
    width: 8px;
}}
QScrollBar::handle:vertical {{
    background: transparent;
    border-top: none;
    border-bottom: none;
    border-left: 2px solid {ACCENT};
    border-right: none;
    border-radius: 0;
    min-height: 20px;
}}
QScrollBar::handle:vertical:hover, QScrollBar::handle:vertical:pressed {{
    background: {ACCENT};
    border-left: 2px solid {ACCENT};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{
    background: {CANVAS_BG};
    height: 8px;
}}
QScrollBar::handle:horizontal {{
    background: transparent;
    border-left: none;
    border-right: none;
    border-top: 2px solid {ACCENT};
    border-bottom: none;
    border-radius: 0;
    min-width: 20px;
}}
QScrollBar::handle:horizontal:hover, QScrollBar::handle:horizontal:pressed {{
    background: {ACCENT};
    border-top: 2px solid {ACCENT};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

/* ── Splitter handles ──────────────────────────────────────── */
QSplitter::handle {{ background: transparent; width: 10px; height: 10px; }}

/* ── Status bar ────────────────────────────────────────────── */
QStatusBar {{
    background-color: {PANEL_BG};
    color: {TEXT_DIM};
    font-family: "{FAMILY_MONO}";
    font-size: {BASE_PT}pt;
}}

/* ── Group boxes ───────────────────────────────────────────── */
QGroupBox {{
    border: 1px solid {UI_BORDER};
    border-radius: 4px;
    margin-top: 10px;
    color: {TEXT};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 4px;
}}

/* ── Checkboxes ────────────────────────────────────────────── */
QCheckBox {{
    color: {TEXT};
    spacing: 6px;
}}
QCheckBox::indicator {{
    width: 14px;
    height: 14px;
    border: none;
    background-color: {INPUT_BG};
    border-radius: 2px;
}}
QCheckBox::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
}}

/* ── Frames (dividers) ─────────────────────────────────────── */
QFrame[frameShape="4"],
QFrame[frameShape="5"] {{ color: {UI_BORDER}; }}

/* ── Dialogs & message boxes ───────────────────────────────── */
QMessageBox {{ background-color: {BG}; }}

/* ── Tooltips ──────────────────────────────────────────────── */
QToolTip {{
    background-color: {PANEL_BG};
    color: {TEXT};
    border: none;
    font-size: {BASE_PT}pt;
}}
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def table_stylesheet() -> str:
    """Return the shared QTableWidget stylesheet used by list visualizers."""
    return f"""
        QTableWidget {{
            background: transparent;
            border: none;
            gridline-color: {BG};
        }}
        QTableWidget::item {{
            background: #666666;
            color: {TEXT};
            border: none;
            padding: 2px;
        }}
        QTableWidget::item:selected {{
            background: {ACCENT};
            color: {ACCENT_TEXT};
        }}
        QHeaderView::section {{
            background: {PANEL_BG};
            color: {TEXT};
            font-weight: bold;
            border: none;
            padding: 4px 2px;
        }}
        QTableCornerButton::section {{
            background: {PANEL_BG};
            border: none;
        }}
    """


def apply_theme(app) -> None:
    """Apply the canonical visualizer style contract to *app*.

    Call once from the visualizer's launcher function, immediately after
    creating the QApplication instance.

    This function standardizes presentation only. It does not alter business
    logic, project operations, or data ownership boundaries.
    """
    # Force Fusion so palette and stylesheet rules are honoured for combo
    # popups instead of being overridden by the native GTK/platform style.
    from PyQt5.QtWidgets import QStyleFactory
    app.setStyle(QStyleFactory.create("Fusion"))
    _register_fonts()
    app.setStyleSheet(_STYLESHEET)
    from PyQt5.QtGui import QFont, QPalette, QColor
    app.setFont(QFont(FAMILY_UI, BASE_PT))
    # Set Highlight/HighlightedText for ALL three color groups (Active,
    # Inactive, Disabled).  Qt's Fusion style reads QPalette when drawing
    # combo-popup items, and popup windows use the Inactive group because
    # keyboard focus stays on the parent window.  Without setting All, only
    # the Active group is fuchsia and the popup still shows the system default.
    _pal = app.palette()
    _pal.setColor(QPalette.All, QPalette.Highlight,       QColor(ACCENT))
    _pal.setColor(QPalette.All, QPalette.HighlightedText, QColor(ACCENT_TEXT))
    app.setPalette(_pal)


# ---------------------------------------------------------------------------
# GripSplitter — QSplitter with painted grip-dot handles
# ---------------------------------------------------------------------------

from PyQt5.QtWidgets import QSplitter, QSplitterHandle  # noqa: E402
from PyQt5.QtCore import Qt                              # noqa: E402
from PyQt5.QtGui import QPainter, QColor                # noqa: E402


class _GripHandle(QSplitterHandle):
    """Splitter handle drawn as a short column of grip dots.

    A plain click (press + release without dragging) toggles the pane on
    the *right* side of this handle between collapsed (0) and its previous
    width.  Drag behaviour is unchanged.
    """

    _DOT_COLOUR      = QColor(SPLITTER)
    _HOVER_COLOUR    = QColor(ACCENT)
    _DOT_R           = 2   # dot radius (px)
    _DOT_GAP         = 6   # centre-to-centre spacing (px)
    _N_DOTS          = 5   # number of dots
    _CLICK_THRESHOLD = 4   # manhattanLength below which a release = click

    def __init__(self, orientation, parent):
        super().__init__(orientation, parent)
        self._hovered    = False
        self._press_pos  = None   # set on mouse-down; cleared on release
        self._saved_size = 0      # remembered size of right pane before collapse

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press_pos = event.pos()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._press_pos is not None:
            if (event.pos() - self._press_pos).manhattanLength() <= self._CLICK_THRESHOLD:
                self._toggle_right_pane()
        self._press_pos = None
        super().mouseReleaseEvent(event)

    # ------------------------------------------------------------------
    # Toggle logic

    def _handle_index(self) -> int:
        """Return this handle's position index in the parent splitter."""
        sp = self.splitter()
        for i in range(1, sp.count()):
            if sp.handle(i) is self:
                return i
        return -1

    def _toggle_right_pane(self) -> None:
        """Collapse or expand the pane immediately to the right of this handle.

        Space is always exchanged with the nearest non-collapsed pane to the
        left so that already-collapsed neighbours are not accidentally opened.
        """
        sp  = self.splitter()
        idx = self._handle_index()
        if idx < 0:
            return
        sizes      = list(sp.sizes())
        right_size = sizes[idx]

        # Find the nearest left pane that is currently visible (size > 0).
        left_idx = idx - 1
        while left_idx >= 0 and sizes[left_idx] == 0:
            left_idx -= 1

        if left_idx < 0:
            return   # nothing visible to exchange space with

        if right_size == 0:
            # Expand — take space from the leftmost visible neighbour
            w_min  = getattr(sp.widget(idx), "minimumWidth", lambda: 160)()
            target = self._saved_size if self._saved_size > 0 else max(w_min, 160)
            target = min(target, sizes[left_idx] - 1)
            if target <= 0:
                return
            sizes[idx]      = target
            sizes[left_idx] -= target
        else:
            # Collapse — give space to the leftmost visible neighbour
            self._saved_size = right_size
            sizes[left_idx] += right_size
            sizes[idx]       = 0

        # Block splitterMoved so that any connected handler (e.g. CatalogBrowser's
        # _on_splitter_moved) does not override the sizes we just computed.
        # The handler is for interactive drag resizing only.
        sp.blockSignals(True)
        sp.setSizes(sizes)
        sp.blockSignals(False)

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(CANVAS_BG))
        p.setRenderHint(QPainter.Antialiasing)
        colour = self._HOVER_COLOUR if self._hovered else self._DOT_COLOUR
        p.setBrush(colour)
        p.setPen(Qt.NoPen)
        w, h = self.width(), self.height()
        cx = w // 2
        span = (self._N_DOTS - 1) * self._DOT_GAP
        y0 = (h - span) // 2
        for i in range(self._N_DOTS):
            cy = y0 + i * self._DOT_GAP
            p.drawEllipse(cx - self._DOT_R, cy - self._DOT_R,
                          self._DOT_R * 2, self._DOT_R * 2)
        p.end()


class GripSplitter(QSplitter):
    """QSplitter that uses grip-dot handles instead of a solid bar."""

    def createHandle(self):
        return _GripHandle(self.orientation(), self)


# ---------------------------------------------------------------------------
# JumpScrollBar — vertical scrollbar with hover highlight + click-to-jump
# ---------------------------------------------------------------------------

from PyQt5.QtWidgets import QScrollBar, QStyle, QStyleOptionSlider  # noqa: E402
from PyQt5.QtGui import QCursor                                      # noqa: E402


class JumpScrollBar(QScrollBar):
    """Vertical scrollbar with two UX improvements over the default:

    1. Hovering *anywhere* on the bar immediately highlights the handle fuchsia
       (not just when the cursor is directly on the handle thumb).
    2. Clicking in the track (not on the handle) jumps the viewport to that
       position instantly instead of doing a page-step.  Click+drag is also
       supported for continuous scrubbing.
    """

    _STYLE_IDLE = (
        f"QScrollBar:vertical {{ background: {CANVAS_BG}; width: 16px; }}"
        "QScrollBar::handle:vertical {"
        "    background: transparent;"
        f"   border-left: 2px solid {ACCENT};"
        "    border-radius: 0; min-height: 20px; }"
        "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }"
    )
    _STYLE_HOVER = (
        f"QScrollBar:vertical {{ background: {CANVAS_BG}; width: 16px; }}"
        "QScrollBar::handle:vertical {"
        f"   background: {ACCENT};"
        f"   border-left: 2px solid {ACCENT};"
        "    border-radius: 0; min-height: 20px; }"
        "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }"
    )

    def __init__(self, parent=None) -> None:
        super().__init__(Qt.Vertical, parent)
        self.setStyleSheet(self._STYLE_IDLE)
        self._drag_active = False

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # If the scrollbar becomes visible while the cursor is already over it
        # (common with ScrollBarAsNeeded), Qt won't fire enterEvent.  Apply the
        # hover style immediately so the user gets visual feedback right away.
        if self.underMouse():
            self.setStyleSheet(self._STYLE_HOVER)

    def _groove(self):
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(
            QStyle.CC_ScrollBar, opt, QStyle.SC_ScrollBarGroove, self
        )
        return groove, opt.upsideDown

    def enterEvent(self, event) -> None:
        self.setStyleSheet(self._STYLE_HOVER)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if not self._drag_active:
            self.setStyleSheet(self._STYLE_IDLE)
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            handle_rect = self.style().subControlRect(
                QStyle.CC_ScrollBar, opt, QStyle.SC_ScrollBarSlider, self
            )
            if not handle_rect.contains(event.pos()):
                groove = self.style().subControlRect(
                    QStyle.CC_ScrollBar, opt, QStyle.SC_ScrollBarGroove, self
                )
                pos = event.y() - groove.y()
                value = QStyle.sliderValueFromPosition(
                    self.minimum(), self.maximum(), pos, groove.height(),
                    opt.upsideDown,
                )
                self.setValue(value)
                self._drag_active = True
                self.grabMouse()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_active:
            local = self.mapFromGlobal(QCursor.pos())
            groove, upside_down = self._groove()
            pos = local.y() - groove.y()
            value = QStyle.sliderValueFromPosition(
                self.minimum(), self.maximum(), pos, groove.height(), upside_down
            )
            self.setValue(value)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._drag_active and event.button() == Qt.LeftButton:
            self._drag_active = False
            self.releaseMouse()
            if not self.rect().contains(self.mapFromGlobal(QCursor.pos())):
                self.setStyleSheet(self._STYLE_IDLE)
            return
        super().mouseReleaseEvent(event)


# ---------------------------------------------------------------------------
# Window geometry persistence helpers
# ---------------------------------------------------------------------------

def save_window_geometry(win, key: str) -> None:
    """Save *win*'s current screen geometry to prefs under *key*."""
    from tool import prefs as _prefs
    g = win.geometry()
    _prefs.set(key, [g.x(), g.y(), g.width(), g.height()])


def restore_window_geometry(win, key: str) -> None:
    """Restore *win*'s geometry from prefs.  No-op if nothing was saved yet.

    Clamps the position so the window is never placed fully off-screen.
    """
    from tool import prefs as _prefs
    geom = _prefs.get(key)
    if not (isinstance(geom, (list, tuple)) and len(geom) == 4):
        return
    from PyQt5.QtWidgets import QApplication
    x, y, w, h = (int(v) for v in geom)
    screen = QApplication.primaryScreen().availableGeometry()
    x = max(screen.left(), min(x, screen.right()  - 100))
    y = max(screen.top(),  min(y, screen.bottom() - 100))
    win.setGeometry(x, y, w, h)


# ---------------------------------------------------------------------------
# SVG icon helper
# ---------------------------------------------------------------------------

try:
    from PyQt5.QtSvg import QSvgRenderer as _QSvgRenderer
    _SVG_AVAILABLE = True
except ImportError:
    _SVG_AVAILABLE = False


def svg_icon(name: str, size: int = 16, color: str = "#ffffff"):
    """Load an Iconoir SVG, recolour strokes to *color*, return a QIcon.

    This is the canonical icon-loading function for all Crossing Tool
    visualizers.  Both ``book_visualizer`` and ``silhouette_visualizer``
    delegate here instead of maintaining their own local copies.

    Parameters
    ----------
    name:
        Icon filename stem (without ``.svg`` extension), e.g. ``"trash"``.
    size:
        Rendered pixel size (square).  All four icon mode/state combinations
        are set to the same pixmap so Qt always finds a match.
    color:
        Hex colour string used to replace the canonical ``#000000`` stroke
        colour in the SVG source.  Defaults to white (``"#ffffff"``).

    Returns
    -------
    QIcon
        A QIcon with the recoloured SVG rendered at *size* × *size* pixels,
        or an empty QIcon when the SVG file does not exist or QtSvg is not
        available.
    """
    from PyQt5.QtGui import QIcon, QPixmap
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QPainter

    icon_dir = Path(__file__).parent / "icons" / "iconoir"
    path = icon_dir / f"{name}.svg"
    if not path.exists():
        return QIcon()
    coloured = path.read_bytes().replace(b"#000000", color.encode())
    if _SVG_AVAILABLE:
        renderer = _QSvgRenderer(coloured)
        pix = QPixmap(size, size)
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        renderer.render(painter)
        painter.end()
        icon = QIcon()
        for mode in (QIcon.Normal, QIcon.Active, QIcon.Selected, QIcon.Disabled):
            icon.addPixmap(pix, mode, QIcon.Off)
            icon.addPixmap(pix, mode, QIcon.On)
        return icon
    return QIcon()
