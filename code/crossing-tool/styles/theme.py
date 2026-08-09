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
  - Highlight   : yellow    (#ffff00)
  - Scrollbars  : same background as tab/panel chrome (TAB_BG) idle (thin
                              half-width indicator line), yellow full-width
                              fill while active (hovered, dragging, or
                              briefly after any scroll input). The
                              scrollbar's own footprint/width NEVER changes
                              between these two states — only the handle
                              drawn inside it does. See SCROLLBAR_* and
                              JumpScrollBar.
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
from typing import Optional

# ---------------------------------------------------------------------------
# Color tokens
# ---------------------------------------------------------------------------

BG           = "#808080"   # 50% grey — main window / widget background
PANEL_BG     = "#555555"   # slightly darker for control-panel areas
TAB_BG       = "#555555"   # panel/tab background (same as PANEL_BG)
INPUT_BG     = "#606060"   # input fields, list backgrounds
TITLE_BG     = "#4A4A4A"   # header/title background (slightly darker than PANEL_BG)
CELL_BG      = "#606060"   # table cell backgrounds (same as TITLE_BG for now)
BTN_BG       = "#606060"   # button face
BTN_HOVER    = "#8d8d8d"   # button hover (lighter than BG)
BTN_PRESSED  = "#505050"   # button pressed (darker)
BUTTON_HEIGHT = 24         # fixed height for all inspector/action buttons (px)
BUTTON_ICON_SIZE = max(12, BUTTON_HEIGHT - 10)  # canonical icon size for square toolbar/header buttons
BTN_H        = BUTTON_HEIGHT
BTN_ICON     = BUTTON_ICON_SIZE
# ── DEVELOPER NOTE — canonical, fine-tuned; do not change casually ────────
# Canonical scrollbar container thickness (px) — shared by the global QSS
# default (plain QScrollBar/QScrollArea/QTextEdit etc.) and JumpScrollBar, so
# every scrollbar in the app has the identical footprint. THIS NEVER CHANGES
# with hover/press/drag/activity state — only the *handle* drawn inside it
# does (see SCROLLBAR_HANDLE_IDLE_W below). Resizing the container itself on
# hover was tried once and reverted: it makes neighboring layout (splitter
# panes, inspector width reservations — see ScrollbarGutter, Inspector,
# shot_visualizer's minimum-width math) reflow/jump every time the cursor
# crosses a scrollbar, which reads as a jarring glitch. If you are a codebot
# tempted to make a scrollbar "thicker on hover" for affordance, do it by
# changing the HANDLE's look within this fixed footprint (as already done
# below), never by changing SCROLLBAR_W or adding a `:hover`/`:pressed` rule
# on QScrollBar:vertical/QScrollBar:horizontal that sets width/height.
SCROLLBAR_W  = 8
# Idle-state handle indicator thickness (px) — exactly half of SCROLLBAR_W.
# Drawn as a thin border-line flush to the outer edge of the (constant-width)
# scrollbar container. On hover/press/drag or within SCROLLBAR_ACTIVITY_MS of
# the last scroll input, the handle switches instead to a solid fill at the
# FULL container width (100% of the gutter) — see SCROLLBAR_IDLE_COLOR/ACCENT
# and JumpScrollBar._style(). Both states live inside the same fixed
# SCROLLBAR_W footprint; only the handle's own look changes, never the
# container's.
SCROLLBAR_HANDLE_IDLE_W = SCROLLBAR_W // 2
# How long (ms) a scrollbar keeps its active look after the last scroll
# input, if the cursor isn't still hovering/pressing it directly.
SCROLLBAR_ACTIVITY_MS = 500
# Scrollbar highlight when idle (not hovered/active/dragging) — the exact
# same token used for tab/panel background chrome (TAB_BG), so scrollbars
# visually recede into that chrome until activated; switches to ACCENT only
# while active (see QSS below + JumpScrollBar). Deliberately an alias, not a
# separate hardcoded value — if TAB_BG's tone changes, scrollbars should
# always follow it, never drift out of sync.
SCROLLBAR_IDLE_COLOR = TAB_BG
# Inspector grid contract (all visualizers):
# - Use edge-to-edge section bodies (no extra nested wrapper insets)
# - Use SECTION_GAP for panel/section interior spacing
# - Keep row rhythm consistent via BTN_H for controls in inspector rows
INSPECTOR_GAP = 3          # canonical inspector spacing/margins (px)
SECTION_GAP  = INSPECTOR_GAP
TEXT         = "#ffffff"   # primary text — white
TEXT_DIM     = "#909090"   # secondary / hint text
TRIANGLE     = "#bfbfbf"   # 75% grey — disclosure and dropdown indicators
BORDER       = "#ffffff"   # interactive element borders (buttons, inputs) — white
UI_BORDER    = "#404040"   # structural chrome borders (group boxes, frames) — 25% grey
SPLITTER     = "#737373"   # splitter drag handles — 45% grey (barely visible on BG)
ACCENT       = "#ffff00"   # selections, active, checked states
ACCENT_TEXT  = "#333333"   # text on ACCENT background (black on yellow; use #ffffff for dark accents)
ACCENT_FILL_ALPHA = 64     # alpha for accent-colored area fills (25% of 255)
CANVAS_BG    = "#3a3a3a"   # video / image display areas (dark so content pops)
TRIANGLE_LEFT = 8          # shared left edge for section/combo triangles
TRIANGLE_WIDTH = 14        # shared indicator footprint
TRIANGLE_TEXT_LEFT = 30    # left inset for text following a triangle
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
QPushButton:focus    {{ outline: none; }}

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
/* DEVELOPER NOTE (canonical, fine-tuned — do not change casually): the
   scrollbar's own footprint (width when vertical, height when horizontal)
   is ALWAYS SCROLLBAR_W and NEVER changes on hover/press/drag/activity —
   only the *handle* drawn inside it does (a thin SCROLLBAR_HANDLE_IDLE_W
   line when idle, a solid fill at the full SCROLLBAR_W width when
   hovered/pressed/active). Do NOT add a `:hover`/`:pressed` rule that
   changes width/height on QScrollBar:vertical or QScrollBar:horizontal
   themselves — that reintroduces a layout-reflow glitch (neighboring
   panes visibly jump every time the cursor crosses the scrollbar) that
   was deliberately fixed. Only style QScrollBar::handle:*. */
QScrollBar:vertical {{
    background: {CANVAS_BG};
    width: {SCROLLBAR_W}px;
}}
QScrollBar::handle:vertical {{
    background: transparent;
    border-top: none;
    border-bottom: none;
    border-left: {SCROLLBAR_HANDLE_IDLE_W}px solid {SCROLLBAR_IDLE_COLOR};
    border-right: none;
    border-radius: 0;
    min-height: 20px;
}}
QScrollBar::handle:vertical:hover, QScrollBar::handle:vertical:pressed {{
    background: {ACCENT};
    border-left: none;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{
    background: {CANVAS_BG};
    height: {SCROLLBAR_W}px;
}}
QScrollBar::handle:horizontal {{
    background: transparent;
    border-left: none;
    border-right: none;
    border-top: none;
    border-bottom: {SCROLLBAR_HANDLE_IDLE_W}px solid {SCROLLBAR_IDLE_COLOR};
    border-radius: 0;
    min-width: 20px;
}}
QScrollBar::handle:horizontal:hover, QScrollBar::handle:horizontal:pressed {{
    background: {ACCENT};
    border-bottom: none;
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
            background: {TAB_BG};
            border: none;
            gridline-color: {TAB_BG};
        }}
        QTableWidget::item {{
            background: {CELL_BG};
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


def action_button_stylesheet() -> str:
    """Return the shared per-button stylesheet used for inspector action buttons.

    Qt Style Sheets cascade: once ANY ancestor of a widget has its own
    (even minimal, bare-property) stylesheet, that ancestor's local
    stylesheet takes precedence over the application-wide stylesheet for
    that widget's descendants — including a generic app-wide
    ``QPushButton {...}`` rule. Any button nested inside a styled container
    (e.g. a CollapsibleSection body, a colored panel) therefore needs this
    stylesheet applied directly to *itself* to reliably get the canonical
    background/hover/pressed/checked/disabled look, matching the
    ``action_btn_style`` / ``_btn_style()`` convention already used by the
    Book and Illustration Visualizers.
    """
    return (
        f"QPushButton {{"
        f"  background-color: {BTN_BG}; color: {TEXT};"
        f"  border: none; border-radius: 3px; padding: 0 8px;"
        f"  min-height: {BTN_H}px; max-height: {BTN_H}px;"
        # Match the CollapsibleSection title font weight so button text
        # feels visually consistent with the rest of the inspector chrome.
        f"  font-family: '{FAMILY_UI}'; font-weight: {WEIGHT_UI};"
        f"}}"
        f"QPushButton:hover    {{ background-color: {ACCENT}; color: {ACCENT_TEXT}; }}"
        # Invert the hover foreground/background while the button is pressed:
        # the button background becomes the accent text color and the
        # foreground becomes the accent color so the pressed state reads
        # as an inversion of the hover state.
        f"QPushButton:pressed  {{ background-color: {ACCENT_TEXT}; color: {ACCENT}; }}"
        f"QPushButton:checked  {{ background-color: {ACCENT}; color: {ACCENT_TEXT}; }}"
        f"QPushButton:disabled {{ background-color: {BTN_BG};"
        f" color: rgba(255,255,255,0.15); }}"
    )



def tab_strip_stylesheet() -> str:
    """Return the shared QTabWidget/QTabBar stylesheet used by inspector tab strips.

    This is the canonical "workspace tabs" look (dark CANVAS_BG tab bar, TAB_BG
    tab pane, dim inactive labels) used by the Book Visualizer's Book/Engravings
    tabs. Any visualizer inspector that wraps its content in a QTabWidget
    should use this instead of inventing its own tab styling, so the color
    scheme stays identical across visualizers even when a tab strip has only
    a single tab.
    """
    return (
        f"QTabWidget {{ background: {CANVAS_BG}; border: none; }}"
        f"QTabWidget::pane {{ background: {TAB_BG}; border: none; }}"
        f"QTabBar {{ background: {CANVAS_BG}; border: none; }}"
        f"QTabBar::tab {{"
        f" background: {CANVAS_BG}; color: {TEXT_DIM};"
        f" padding: 2px 16px; border: none;"
        f" font-family: '{FAMILY_UI}'; font-size: {BASE_PT}pt;"
        f" font-weight: {WEIGHT_UI};"
        f" min-height: 20px;"
        f" min-width: 0px;"
        f"}}"
        f"QTabBar::tab:selected {{ background: {TAB_BG}; color: {TEXT}; border: none; }}"
        f"QTabBar::tab:focus {{ outline: none; }}"
        f"QTabBar::tab:hover {{ background: {ACCENT}; color: {ACCENT_TEXT}; }}"
        f"QTabBar::tab:selected:hover {{ background: {TAB_BG}; color: {TEXT}; }}"
    )


_COMBO_INDICATOR_MARKER = "/* crossing-combo-indicator */"


def combo_indicator_stylesheet() -> str:
    """Return the shared left-side triangle treatment for combo boxes."""
    return (
        f"{_COMBO_INDICATOR_MARKER}"
        f"QComboBox {{ padding-left: {TRIANGLE_TEXT_LEFT}px; }}"
        f"QComboBox::drop-down {{"
        f" subcontrol-origin: padding; subcontrol-position: center left;"
        f" width: 0px; border: none;"
        f"}}"
        f"QComboBox::down-arrow {{ image: none; width: 0px; height: 0px; }}"
    )


class _ComboIndicatorFilter:
    """Factory namespace for keeping indicator QSS on every combo box."""

    @staticmethod
    def create(parent):
        from PyQt5.QtCore import QEvent, QObject, Qt
        from PyQt5.QtWidgets import QComboBox, QLabel

        class ComboIndicatorFilter(QObject):
            @staticmethod
            def _ensure_indicator(combo):
                indicator = combo.findChild(QLabel, "crossingComboIndicator")
                if indicator is None:
                    indicator = QLabel("▶", combo)
                    indicator.setObjectName("crossingComboIndicator")
                    indicator.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                    indicator.setAlignment(Qt.AlignCenter)
                    indicator.setStyleSheet(
                        f"background: transparent; color: {TRIANGLE}; border: none;"
                        f" font-family: '{FAMILY_UI}'; font-size: {BASE_PT}pt;"
                    )
                    indicator.show()
                indicator.setGeometry(
                    TRIANGLE_LEFT, 0, TRIANGLE_WIDTH, combo.height()
                )
                indicator.raise_()

            def eventFilter(self, watched, event):  # noqa: N802
                if isinstance(watched, QComboBox):
                    if event.type() in (QEvent.Polish, QEvent.StyleChange):
                        if (
                            not watched.property("crossingComboIndicatorApplying")
                            and _COMBO_INDICATOR_MARKER not in watched.styleSheet()
                        ):
                            watched.setProperty("crossingComboIndicatorApplying", True)
                            watched.setStyleSheet(
                                watched.styleSheet() + combo_indicator_stylesheet()
                            )
                            watched.setProperty("crossingComboIndicatorApplying", False)
                        self._ensure_indicator(watched)
                    elif event.type() == QEvent.Resize:
                        self._ensure_indicator(watched)
                return False

        return ComboIndicatorFilter(parent)


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
    # the Active group is yellow and the popup still shows the system default.
    _pal = app.palette()
    _pal.setColor(QPalette.All, QPalette.Highlight,       QColor(ACCENT))
    _pal.setColor(QPalette.All, QPalette.HighlightedText, QColor(ACCENT_TEXT))
    app.setPalette(_pal)
    combo_indicator_filter = _ComboIndicatorFilter.create(app)
    app.installEventFilter(combo_indicator_filter)
    app._crossing_combo_indicator_filter = combo_indicator_filter


# ---------------------------------------------------------------------------
# GripSplitter — QSplitter with painted grip-dot handles
# ---------------------------------------------------------------------------

from PyQt5.QtWidgets import QSplitter, QSplitterHandle  # noqa: E402
from PyQt5.QtCore import Qt, pyqtSignal                  # noqa: E402
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
        self._dragged    = False  # set once a real drag move is observed
        self._last_right_size = 0 # right-pane size observed during drag
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
            self._dragged = False
            sp = self.splitter()
            idx = self._handle_index()
            if idx >= 0:
                sizes = sp.sizes()
                self._last_right_size = sizes[idx] if idx < len(sizes) else 0
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._press_pos is not None and (event.buttons() & Qt.LeftButton):
            if (event.pos() - self._press_pos).manhattanLength() > 0:
                self._dragged = True
        super().mouseMoveEvent(event)
        if self._dragged:
            self._maybe_snap_right_pane_closed()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._press_pos is not None:
            if not self._dragged and (event.pos() - self._press_pos).manhattanLength() <= self._CLICK_THRESHOLD:
                self._toggle_right_pane()
        self._press_pos = None
        self._dragged = False
        self._last_right_size = 0
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

    @staticmethod
    def _is_fixed_pane(sp, i: int) -> bool:
        """Return True if pane *i* has a hard fixed width (e.g. via setFixedWidth).

        Such a pane (like a content-driven, non-resizable side panel) cannot
        actually absorb or give up arbitrary space, so it must be skipped when
        looking for a partner pane to exchange space with — otherwise the
        requested size gets silently clamped by Qt and leaves an unaccounted
        dead-space gap instead of being handed off to a pane that can use it.
        """
        w = sp.widget(i)
        return w is not None and w.minimumWidth() >= w.maximumWidth()

    def _maybe_snap_right_pane_closed(self) -> None:
        """Collapse the right pane as soon as a drag reaches its minimum width."""
        sp = self.splitter()
        if not sp.property("snap_right_pane_on_drag"):
            return
        idx = self._handle_index()
        if idx < 0:
            return

        sizes = sp.sizes()
        if idx >= len(sizes):
            return

        right_size = sizes[idx]
        if right_size <= 0:
            self._last_right_size = 0
            return

        w_min = getattr(sp.widget(idx), "minimumWidth", lambda: 0)()
        collapse_at = max(1, w_min)
        if right_size <= collapse_at and right_size < self._last_right_size:
            self._dragged = True
            self._toggle_right_pane()
            self._last_right_size = 0
            return

        self._last_right_size = right_size

    def _toggle_right_pane(self) -> None:
        """Collapse or expand the pane immediately to the right of this handle."""
        sp  = self.splitter()
        idx = self._handle_index()
        if idx < 0:
            return
        sizes = sp.sizes()
        currently_collapsed = idx >= len(sizes) or sizes[idx] == 0
        self.set_collapsed(not currently_collapsed)

    def set_collapsed(self, collapsed: bool) -> None:
        """Collapse (True) or expand (False) the pane to the right of this
        handle, using the same space-exchange logic as a manual click —
        callable programmatically so code can drive the exact same
        collapse/expand behaviour a user click would (e.g. restoring a
        persisted collapsed state). Emits the splitter's `paneToggled`
        signal exactly like a click does, so listeners can't tell the
        difference.

        Space is always exchanged with the nearest visible, resizable pane to
        the left — already-collapsed neighbours and hard fixed-width panes
        (which cannot actually grow/shrink to absorb the change) are skipped
        in favour of the next pane further left.
        """
        sp  = self.splitter()
        idx = self._handle_index()
        if idx < 0:
            return
        sizes      = list(sp.sizes())
        right_size = sizes[idx]

        if collapsed == (right_size == 0):
            return  # already in the requested state

        # Find the nearest left pane that is currently visible (size > 0)
        # and actually resizable (not a hard fixed-width pane).
        left_idx = idx - 1
        while left_idx >= 0 and (
            sizes[left_idx] == 0 or self._is_fixed_pane(sp, left_idx)
        ):
            left_idx -= 1

        if left_idx < 0:
            return   # nothing visible & resizable to exchange space with

        if not collapsed:
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

        # Notify any interested listener (opt-in — e.g. persisting one
        # specific pane's collapsed state) that this pane's collapsed state
        # changed. Splitters with nothing connected are unaffected.
        paneToggled = getattr(sp, "paneToggled", None)
        if paneToggled is not None:
            paneToggled.emit(idx, not collapsed)

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

    # Emitted whenever a pane's collapsed state changes, whether via a
    # handle click/drag-snap or a programmatic `set_pane_collapsed()` call —
    # (pane_index, now_expanded). Purely opt-in: splitters with no connected
    # listeners behave exactly as before.
    paneToggled = pyqtSignal(int, bool)

    def createHandle(self):
        return _GripHandle(self.orientation(), self)

    def set_pane_collapsed(self, index: int, collapsed: bool) -> None:
        """Programmatically collapse/expand the pane at *index* using the
        same space-exchange logic as clicking the handle to its immediate
        left (that handle owns the collapse logic for this pane).
        """
        handle = self.handle(index)
        if isinstance(handle, _GripHandle):
            handle.set_collapsed(collapsed)

    def is_pane_collapsed(self, index: int) -> bool:
        sizes = self.sizes()
        return 0 <= index < len(sizes) and sizes[index] == 0


# ---------------------------------------------------------------------------
# JumpScrollBar — vertical scrollbar with hover highlight + click-to-jump
# ---------------------------------------------------------------------------

from PyQt5.QtWidgets import QApplication, QScrollBar, QStyle, QStyleOptionSlider  # noqa: E402
from PyQt5.QtGui import QCursor                                      # noqa: E402
from PyQt5.QtCore import pyqtSignal, QTimer                          # noqa: E402


class JumpScrollBar(QScrollBar):
    """Scrollbar (vertical or horizontal) with UX improvements over the default:

    1. Idle, the handle is a thin SCROLLBAR_HANDLE_IDLE_W (half of
       SCROLLBAR_W) indicator line using the same background as tab/panel
       chrome (SCROLLBAR_IDLE_COLOR, aliased to TAB_BG). It becomes
       "active" — ACCENT-colored, expanding to a solid fill at the FULL
       SCROLLBAR_W width (100% of the gutter) — while the cursor is
       directly over it, while its handle is being dragged, or for
       SCROLLBAR_ACTIVITY_MS after ANY value change for any reason (mouse
       wheel over the scrolled content, trackpad/tablet scroll gestures,
       keyboard, click-to-jump, programmatic scrolling — value changes are
       all that matters, not how they happened). It shrinks back to the
       idle line once that activity window elapses, unless still
       hovered/dragging. The scrollbar's own footprint (SCROLLBAR_W) NEVER
       changes between these two states — only the handle drawn inside it
       does; changing the container's own size on hover was tried once and
       reverted because it makes neighboring layout visibly jump/reflow.
    2. Clicking in the track (not on the handle) jumps the viewport to that
       position instantly instead of doing a page-step.  Click+drag is also
       supported for continuous scrubbing.
    3. ``mousePressed`` / ``mouseReleased`` fire on every left-button
       press/release (both the click-to-jump path and ordinary handle
       dragging), so callers can pause a video/animation while scrubbing and
       resume it afterwards.

    Pass a ``pageStep`` that reflects the size of the "current unit" (e.g. one
    shot's frame span) so the rendered handle length communicates how much of
    the total range that unit occupies — the same idea as the Book
    Visualizer's bottom position bar, generalised to a real scrollbar. This is
    the shared horizontal timeline-scrubber primitive used by the Shotlist
    Visualizer's Browser.
    """

    mousePressed  = pyqtSignal()
    mouseReleased = pyqtSignal()

    def __init__(self, orientation: Qt.Orientation = Qt.Vertical, parent=None) -> None:
        super().__init__(orientation, parent)
        self._drag_active = False
        self._active = False
        # Enable hover tracking so enter/leave events fire reliably.
        try:
            self.setMouseTracking(True)
            self.setAttribute(self.WA_Hover, True)
        except Exception:
            pass
        # Ensure the physical scrollbar thickness matches the canonical
        # token so it is consistent across all visualizers. Use fixed size
        # along the scroll axis to avoid platform defaults overriding the
        # stylesheet width. This NEVER changes afterward — not even in
        # _set_active() — only the handle's own look does.
        try:
            if orientation == Qt.Vertical:
                self.setFixedWidth(SCROLLBAR_W)
            else:
                self.setFixedHeight(SCROLLBAR_W)
        except Exception:
            pass
        self.setStyleSheet(self._style(active=False))

        # Activity timer — restarted on every value change (see
        # _on_value_changed); reverts to idle after SCROLLBAR_ACTIVITY_MS of
        # no further changes, unless still hovered/dragging.
        self._activity_timer = QTimer(self)
        self._activity_timer.setSingleShot(True)
        self._activity_timer.timeout.connect(self._on_activity_timeout)
        self.valueChanged.connect(self._on_value_changed)

    def _style(self, active: bool) -> str:
        horiz        = self.orientation() == Qt.Horizontal
        orient       = "horizontal" if horiz else "vertical"
        size_prop    = "height" if horiz else "width"
        # Horizontal bars sit below their content (e.g. the Shotlist Browser's
        # timeline scrubber), so the idle/collapsed indicator line should
        # touch the bottom edge, not float at the top. Vertical bars keep the
        # existing left-edge indicator (adjacent to the content they scroll).
        border_side  = "border-bottom" if horiz else "border-left"
        min_len_prop = "min-width" if horiz else "min-height"
        handle_bg    = ACCENT if active else "transparent"
        # Idle: a thin SCROLLBAR_HANDLE_IDLE_W (half of SCROLLBAR_W) border
        # line. Active: no border needed — the ACCENT background above
        # already fills the handle's full width/height (100% of the
        # gutter), since the container itself is always SCROLLBAR_W.
        border_rule  = "none" if active else f"{SCROLLBAR_HANDLE_IDLE_W}px solid {SCROLLBAR_IDLE_COLOR}"
        # Ensure the scroll bar itself and its groove have no border so it
        # visually sits flush with adjacent chrome. Note {size_prop} below is
        # always SCROLLBAR_W — the container's footprint never changes
        # between idle/active; only the handle rule above does.
        return (
            f"QScrollBar:{orient} {{ background: {CANVAS_BG}; {size_prop}: {SCROLLBAR_W}px; border: none; }}"
            f"QScrollBar::groove:{orient} {{ background: transparent; border: none; }}"
            f"QScrollBar::handle:{orient} {{"
            f"    background: {handle_bg};"
            f"   {border_side}: {border_rule};"
            f"    border-radius: 0; {min_len_prop}: 20px; }}"
            f"QScrollBar::add-line:{orient}, QScrollBar::sub-line:{orient} {{ {size_prop}: 0; border: none; }}"
            f"QScrollBar::add-page:{orient}, QScrollBar::sub-page:{orient} {{ background: none; }}"
        )

    def _set_active(self, active: bool) -> None:
        """Switch between the idle (thin half-width line) and active
        (ACCENT full-width fill) handle look. No-op if already in the
        requested state. Never touches the scrollbar's own fixed
        SCROLLBAR_W footprint — only the stylesheet's handle rule."""
        if active == self._active:
            return
        self._active = active
        self.setStyleSheet(self._style(active))

    def _on_value_changed(self, _value: int) -> None:
        # Any value change — wheel, trackpad/tablet gesture, keyboard,
        # click-to-jump, drag-scrub, or programmatic — counts as "activity"
        # and (re)starts the decay window.
        self._set_active(True)
        self._activity_timer.start(SCROLLBAR_ACTIVITY_MS)

    def _cursor_over_bar(self) -> bool:
        return self.rect().contains(self.mapFromGlobal(QCursor.pos()))

    def _left_button_down(self) -> bool:
        return bool(QApplication.mouseButtons() & Qt.LeftButton)

    def _on_activity_timeout(self) -> None:
        # Splitter hide/show and geometry changes can prevent the matching
        # release/leave event from reaching this widget. Reconcile against
        # live input state so stale Qt hover/drag flags cannot pin ACCENT on.
        if self._drag_active and not self._left_button_down():
            self._drag_active = False
            self.releaseMouse()
            self.mouseReleased.emit()
        if not self._drag_active and not self._cursor_over_bar():
            self._set_active(False)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # If the scrollbar becomes visible while the cursor is already over it
        # (common with ScrollBarAsNeeded), Qt won't fire enterEvent.  Apply the
        # active style immediately so the user gets visual feedback right away.
        if self._cursor_over_bar():
            self._set_active(True)

    def _groove(self):
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(
            QStyle.CC_ScrollBar, opt, QStyle.SC_ScrollBarGroove, self
        )
        return groove, opt.upsideDown

    def _pos_and_length(self, pos, groove):
        """Return (offset-into-groove, groove-length) along the scroll axis."""
        if self.orientation() == Qt.Horizontal:
            return pos.x() - groove.x(), groove.width()
        return pos.y() - groove.y(), groove.height()

    def enterEvent(self, event) -> None:
        self._set_active(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        # Leave the active look in place if a scroll-activity decay is still
        # pending (e.g. the cursor moved off the bar right after a wheel
        # scroll) — the activity timer will revert it once that window
        # elapses. Otherwise, behave as before: revert immediately.
        if not self._drag_active and not self._activity_timer.isActive():
            self._set_active(False)
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.mousePressed.emit()   # must fire before any value change
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            handle_rect = self.style().subControlRect(
                QStyle.CC_ScrollBar, opt, QStyle.SC_ScrollBarSlider, self
            )
            if not handle_rect.contains(event.pos()):
                groove, upside_down = self._groove()
                pos, length = self._pos_and_length(event.pos(), groove)
                value = QStyle.sliderValueFromPosition(
                    self.minimum(), self.maximum(), pos, length, upside_down,
                )
                self.setValue(value)
                self._drag_active = True
                self.grabMouse()
                return
        # Ensure focus so subsequent mouseMove events are routed to us.
        try:
            self.setFocus()
        except Exception:
            pass
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_active:
            local = self.mapFromGlobal(QCursor.pos())
            groove, upside_down = self._groove()
            pos, length = self._pos_and_length(local, groove)
            value = QStyle.sliderValueFromPosition(
                self.minimum(), self.maximum(), pos, length, upside_down
            )
            self.setValue(value)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._drag_active and event.button() == Qt.LeftButton:
            self._drag_active = False
            self.releaseMouse()
            # Dragging changes value continuously, so the activity timer is
            # normally still running here — let it decay naturally instead
            # of snapping back to idle the instant the cursor leaves.
            if not self._activity_timer.isActive() and not self._cursor_over_bar():
                self._set_active(False)
            self.mouseReleased.emit()
            return
        super().mouseReleaseEvent(event)
        if event.button() == Qt.LeftButton:
            self.mouseReleased.emit()


# ---------------------------------------------------------------------------
# Window geometry persistence helpers
# ---------------------------------------------------------------------------

def save_window_geometry(
    win, key: str, panel_hidden: bool = False, normal_geometry=None,
) -> None:
    """Save *win*'s current screen geometry to prefs under *key*.

    *panel_hidden* records whether the window's inspector/side panel was
    hidden (toggled via plain Tab) so it can be restored in the same state
    next time this window is opened. Callers with no such panel concept
    can omit it (defaults to False, i.e. "visible").

    *normal_geometry*, if given, is an (x, y, w, h) tuple persisted as the
    windowed-mode geometry INSTEAD of `win.geometry()`. Pass this when *win*
    is currently fullscreen: `win.geometry()` returns the screen-covering
    fullscreen rect in that case, which would otherwise clobber the
    previously-known windowed size/position the next time the window is
    un-fullscreened. Callers that don't track a window's pre-fullscreen
    geometry themselves can omit this; the saved windowed geometry will
    simply be whatever `win.geometry()` returns at save time.
    """
    from tool import prefs as _prefs
    try:
        is_fs = bool(win.isFullScreen())
    except Exception:
        is_fs = False
    if normal_geometry is not None:
        x, y, w, h = normal_geometry
    else:
        g = win.geometry()
        x, y, w, h = g.x(), g.y(), g.width(), g.height()
    # Never persist a size larger than the actual screen — a transient
    # window-manager/DPI-scaling glitch reporting a bogus geometry must not
    # get written out verbatim, or the window would restore stuck at that
    # unusable size next launch (see restore_window_geometry()'s matching
    # clamp).
    try:
        from PyQt5.QtWidgets import QApplication
        screen = QApplication.primaryScreen().availableGeometry()
        w = max(200, min(w, screen.width()))
        h = max(150, min(h, screen.height()))
    except Exception:
        pass
    _prefs.set(key, [
        x, y, w, h,
        1 if is_fs else 0, 1 if panel_hidden else 0,
    ])


def restore_window_geometry(win, key: str):
    """Restore *win*'s windowed geometry from prefs.  No-op if nothing was
    saved yet.

    Clamps the position so the window is never placed fully off-screen.
    Always sets the windowed (non-fullscreen) geometry — callers that
    should actually start fullscreen must call `win.showFullScreen()`
    themselves based on the returned flag rather than relying on this
    function to do it (see `is_fullscreen` below); calling `showFullScreen()`
    only after the window has already been shown normal is what causes the
    "opens maximized instead of truly fullscreen" bug this return value
    exists to let callers avoid.

    Returns a `(panel_hidden, is_fullscreen)` tuple:
      - `panel_hidden` is the saved panel-hidden flag (True/False), or None
        if nothing was saved yet / the stored format predates it.
      - `is_fullscreen` is True if the window should start fullscreen.
    Returns `(None, False)` if nothing was saved yet.
    """
    from tool import prefs as _prefs
    geom = _prefs.get(key)
    # Support legacy stored geometry as [x,y,w,h], [x,y,w,h,fullscreen_flag],
    # or the current [x,y,w,h,fullscreen_flag,panel_hidden_flag] format.
    if not (isinstance(geom, (list, tuple)) and len(geom) in (4, 5, 6)):
        return None, False
    from PyQt5.QtWidgets import QApplication
    x, y, w, h = (int(v) for v in geom[:4])
    screen = QApplication.primaryScreen().availableGeometry()
    # Clamp width/height to the actual screen size (with a sane floor) before
    # positioning. A saved size larger than the screen — e.g. from a
    # transient window-manager/DPI-scaling glitch that got persisted — would
    # otherwise leave the window effectively stuck: far too large to see its
    # own edges/title bar controls, with no way to drag it back to a
    # reasonable size.
    w = max(200, min(w, screen.width()))
    h = max(150, min(h, screen.height()))
    x = max(screen.left(), min(x, screen.right()  - 100))
    y = max(screen.top(),  min(y, screen.bottom() - 100))
    win.setGeometry(x, y, w, h)
    is_fullscreen = False
    try:
        is_fullscreen = bool(len(geom) >= 5 and int(geom[4]))
    except Exception:
        is_fullscreen = False
    panel_hidden = None
    if len(geom) == 6:
        try:
            panel_hidden = bool(int(geom[5]))
        except Exception:
            panel_hidden = None
    return panel_hidden, is_fullscreen


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
    visualizers.  Both ``book_visualizer`` and ``illustration_visualizer``
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
