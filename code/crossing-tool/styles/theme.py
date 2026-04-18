"""Shared visual theme for all Crossing Tool visualizers.

Design language:
  - Background : 50% grey   (#808080)
  - Lines/text : white       (#ffffff)
  - UI borders  : 25% grey   (#404040)  — splitter handles, group-box and
                              section frames  (controlled by UI_BORDER)
  - Highlight   : fuchsia    (#ff00ff)
  - Typography  : Roboto for UI chrome, Roboto Mono for data / info fields
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
BTN_BG       = "#686868"   # button face
BTN_HOVER    = "#8d8d8d"   # button hover (lighter than BG)
BTN_PRESSED  = "#565656"   # button pressed (darker)
BTN_H        = 26          # fixed height for all buttons (px)
TEXT         = "#ffffff"   # primary text — white
TEXT_DIM     = "#c0c0c0"   # secondary / hint text
BORDER       = "#ffffff"   # interactive element borders (buttons, inputs) — white
UI_BORDER    = "#404040"   # structural chrome borders (group boxes, frames) — 25% grey
SPLITTER     = "#737373"   # splitter drag handles — 45% grey (barely visible on BG)
ACCENT       = "#ff00ff"   # fuchsia — selections, active, checked states
CANVAS_BG    = "#3a3a3a"   # video / image display areas (dark so content pops)
# ---------------------------------------------------------------------------
# Typography
# ---------------------------------------------------------------------------

BASE_PT      = 10          # single base point size for all UI elements

FAMILY_UI    = "Roboto"       # buttons, labels, combos, window chrome
FAMILY_MONO  = "Roboto Mono"  # lists, text areas, info / data labels

# ---------------------------------------------------------------------------
# Font registration
# ---------------------------------------------------------------------------

_FONTS_DIR   = Path(__file__).parent / "fonts"

_FONT_FILES  = [
    _FONTS_DIR / "Roboto"      / "Roboto-VariableFont_wdth,wght.ttf",
    _FONTS_DIR / "Roboto"      / "Roboto-Italic-VariableFont_wdth,wght.ttf",
    _FONTS_DIR / "Roboto_Mono" / "RobotoMono-VariableFont_wght.ttf",
    _FONTS_DIR / "Roboto_Mono" / "RobotoMono-Italic-VariableFont_wght.ttf",
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
}}
QPushButton:hover    {{ background-color: {BTN_HOVER}; }}
QPushButton:pressed  {{ background-color: {BTN_PRESSED}; }}
QPushButton:checked  {{ background-color: {ACCENT}; color: {TEXT}; }}
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
    selection-color: {TEXT};
}}

/* ── List widgets ──────────────────────────────────────────── */
QListWidget {{
    background-color: {INPUT_BG};
    color: {TEXT};
    border: none;
    font-family: "{FAMILY_MONO}";
    font-size: {BASE_PT}pt;
}}
QListWidget::item:selected       {{ background-color: {ACCENT}; color: {TEXT}; }}
QListWidget::item:hover          {{ background-color: {BTN_HOVER}; }}
QListWidget::item:selected:hover {{ background-color: {ACCENT}; color: {TEXT}; }}

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
    background: transparent;
    width: 16px;
}}
QScrollBar::handle:vertical {{
    background: transparent;
    border-top: none;
    border-bottom: none;
    border-left: 2px solid #ff00ff;
    border-right: none;
    border-radius: 0;
    min-height: 20px;
}}
QScrollBar::handle:vertical:hover, QScrollBar::handle:vertical:pressed {{
    background: #ff00ff;
    border-left: 2px solid #ff00ff;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{
    background: transparent;
    height: 16px;
}}
QScrollBar::handle:horizontal {{
    background: transparent;
    border-left: none;
    border-right: none;
    border-top: 2px solid #ff00ff;
    border-bottom: none;
    border-radius: 0;
    min-width: 20px;
}}
QScrollBar::handle:horizontal:hover, QScrollBar::handle:horizontal:pressed {{
    background: #ff00ff;
    border-top: 2px solid #ff00ff;
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
            color: {TEXT};
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
    """Register bundled fonts and apply the shared stylesheet to *app*.

    Call once from the visualizer's launcher function, immediately after
    creating the QApplication instance.
    """
    _register_fonts()
    app.setStyleSheet(_STYLESHEET)
    from PyQt5.QtGui import QFont
    app.setFont(QFont(FAMILY_UI, BASE_PT))


# ---------------------------------------------------------------------------
# GripSplitter — QSplitter with painted grip-dot handles
# ---------------------------------------------------------------------------

from PyQt5.QtWidgets import QSplitter, QSplitterHandle  # noqa: E402
from PyQt5.QtCore import Qt                              # noqa: E402
from PyQt5.QtGui import QPainter, QColor                # noqa: E402


class _GripHandle(QSplitterHandle):
    """Splitter handle drawn as a short column of grip dots."""

    _DOT_COLOUR   = QColor("#666666")
    _HOVER_COLOUR = QColor("#ff00ff")
    _DOT_R   = 2   # dot radius (px)
    _DOT_GAP = 6   # centre-to-centre spacing (px)
    _N_DOTS  = 5   # number of dots

    def __init__(self, orientation, parent):
        super().__init__(orientation, parent)
        self._hovered = False

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
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
