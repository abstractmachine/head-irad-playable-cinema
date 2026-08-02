#!/usr/bin/env python3
"""Flipbook Visualizer — browse cinematic motif flipbook pages.

Launched via:
    crossing visualizer flipbook
    crossing visualizer flipbook --media gameplay

Layout (canonical WindowVisualizer shell):
  BROWSER   — scrollable, zoomable grid of page thumbnails (bg fill + motif word)
  INSPECTOR — movie selector, info, Export PDF, zoom

Each page thumbnail is:
  - filled with the shot's palette background color
  - motif word centered in the shot's palette foreground color
  - typed in Libre Clarendon Normal Medium

Covers (front and back) are included as the first and last pages.

Keyboard:
  Home          — previous title
  End           — next title
  Tab           — show/hide inspector
  Shift+Tab     — toggle fullscreen
  Ctrl+wheel / Ctrl+Plus/Minus/0 — zoom the page grid
  Escape / Ctrl+Q / Ctrl+W — close
  Ctrl+P        — export PDF for current movie
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from styles import theme

from PyQt5.QtCore import Qt, QEvent, QSize
from tool.shortcuts import KEY_PREV_TITLE, KEY_NEXT_TITLE
from visualizers.window_visualizer import WindowVisualizer
from visualizers.components.aspect_grid import AspectGridWidget
from visualizers.components.zoom_manager import ZoomManager
from visualizers.components.inspector import Inspector
from visualizers.components.tab_panel import TabPanel
from visualizers.components.combo_popup import attach_combo_popup
from visualizers.components.metadata_block import MetadataBlock
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from PyQt5.QtGui import QColor, QFont, QFontDatabase, QFontMetrics, QPainter

if "QT_QPA_PLATFORM_PLUGIN_PATH" in os.environ:
    del os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ASPECT = 16 / 9      # page width : height ratio  (cinema widescreen)
_GAP    = 6           # px — gap between page thumbnails
_MARGIN = 12          # px — grid outer margin

# Zoom range/step mirror Metadata's browser page (see metadata_visualizer.py)
# so Ctrl+wheel / Ctrl+Plus/Minus/0 feel identical across visualizers.
_ZOOM_MIN     = 0.60
_ZOOM_MAX     = 3.00
_ZOOM_STEP    = 0.20
_ZOOM_DEFAULT = 1.00


def _zoom_key(media_type: str) -> str:
    return f"flipbook_browser_zoom_{media_type}"


# Rows shown in the Info section's two-column (tag / info) table — see
# `create_inspector()`.  "status" carries transient loading/export messages;
# the rest is the per-movie summary that used to live in a status label
# below the movie combo.
_INFO_ROWS = ["status", "shots", "motifs", "pages"]


# ---------------------------------------------------------------------------
# Font loading
# ---------------------------------------------------------------------------

_FONTS_DIR   = Path(__file__).parent.parent / "styles" / "fonts"
_LC_DIR      = _FONTS_DIR / "libre_clarendon" / "fonts"
_FONT_FAMILY = None  # populated by _load_flipbook_font() below


def _load_flipbook_font() -> str:
    """Load Libre Clarendon variants into QFontDatabase and return the family name.

    Falls back to the system default if the font files are not found.
    Called once at module import time and cached in _FONT_FAMILY.
    """
    db = QFontDatabase()
    family = ""
    for path in [
        str(_LC_DIR / "LibreClarendonNormal-162Bold.otf"),
        str(_LC_DIR / "LibreClarendonNormal-68Regular.otf"),
        str(_LC_DIR / "LibreClarendonNormal-110Medium.otf"),
        str(_LC_DIR / "LibreClarendonNormal-42Light.otf"),
        str(_FONTS_DIR / "Hanken_Grotesk" / "HankenGrotesk-VariableFont_wght.ttf"),
    ]:
        if Path(path).exists():
            font_id = db.addApplicationFont(path)
            if font_id != -1:
                families = db.applicationFontFamilies(font_id)
                if families and not family:
                    family = families[0]
    return family  # Qt will use the default system font if empty


# ---------------------------------------------------------------------------
# Page thumbnail widget
# ---------------------------------------------------------------------------

class _PageCell(QWidget):
    """A single flipbook page thumbnail.

    Fills the cell with the page background color, then draws the motif word
    (or cover text) centered in the foreground color using Libre Clarendon.
    """

    def __init__(
        self,
        page: dict,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._page = page

        # Tooltip shows shot index and time
        kind  = page.get("kind", "shot")
        idx   = page.get("shot_index")
        start = page.get("start_time", "")
        text  = page.get("text", "")

        if kind == "cover_front":
            film_motif = page.get("film_motif") or {}
            motif_val  = film_motif.get("value", "").strip()
            if motif_val and motif_val != text:
                tip = f"Front cover\n{text}\n(film motif: {motif_val})"
            else:
                tip = f"Front cover\n{text}"
        elif kind == "cover_back":
            back_title = page.get("back_title", text)
            back_year  = page.get("back_year", "")
            display    = f"{back_title}, {back_year}" if back_year else back_title
            tip = f"Back cover\n{display}"
        else:
            parts = []
            if idx is not None:
                parts.append(f"Shot {idx}")
            if start:
                parts.append(start)
            if text:
                parts.append(f"\"{text}\"")
            tip = "\n".join(parts)
        self.setToolTip(tip)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(int(120 * _ASPECT), 120)

    def paintEvent(self, _event) -> None:  # noqa: N802
        page = self._page
        bg_raw = page.get("bg_rgb") or [60, 60, 60]
        fg_raw = page.get("fg_rgb") or [180, 180, 180]
        text   = str(page.get("text") or "")

        bg_color = QColor(int(bg_raw[0]), int(bg_raw[1]), int(bg_raw[2]))
        fg_color = QColor(int(fg_raw[0]), int(fg_raw[1]), int(fg_raw[2]))

        painter = QPainter(self)
        painter.setRenderHint(QPainter.TextAntialiasing)
        painter.fillRect(self.rect(), bg_color)

        kind   = page.get("kind", "shot")
        cell_w = self.width()
        cell_h = self.height()

        if text:
            # Start with a pixel size of 40% of cell height, then scale
            # down proportionally if the word is wider than 80% of cell width.
            target_px = max(6, int(cell_h * 0.40))
            font = QFont(_FONT_FAMILY or "")
            font.setPixelSize(target_px)

            avail_w = int(cell_w * 0.80)
            fm = QFontMetrics(font)
            tw = fm.horizontalAdvance(text)

            if tw > 0 and tw > avail_w:
                scaled_px = max(6, int(target_px * avail_w / tw))
                font.setPixelSize(scaled_px)
                fm = QFontMetrics(font)
                tw = fm.horizontalAdvance(text)

            th = fm.height()
            x = (cell_w - tw) // 2
            y = (cell_h - th) // 2 + fm.ascent()

            painter.setFont(font)
            painter.setPen(fg_color)
            painter.drawText(x, y, text)

        painter.end()


# ---------------------------------------------------------------------------
# Browser page: scrollable, zoomable grid of page thumbnails for one movie
# ---------------------------------------------------------------------------

class _FlipbookBrowserPage(QWidget):
    """Owns the page-thumbnail grid for the currently selected movie.

    Composed from the same shared building blocks Metadata's browser page
    uses — `AspectGridWidget` for the best-fit grid reflow (extracted from
    what used to be this file's own `_GridWidget._reflow()`, near-identical
    to Palette's) and `ZoomManager` for zoom state/persistence/Ctrl+wheel
    and Ctrl+Plus/Minus/0 handling. See visualizers/components/
    {aspect_grid,zoom_manager}.py.
    """

    def __init__(self, media_type: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._media_type = media_type
        self._cells: list[_PageCell] = []

        from tool import prefs as _prefs
        initial_zoom = float(_prefs.get(_zoom_key(media_type), _ZOOM_DEFAULT) or _ZOOM_DEFAULT)
        self._zoom_manager = ZoomManager(
            self,
            initial_zoom,
            _ZOOM_MIN,
            _ZOOM_MAX,
            _ZOOM_STEP,
            persist_cb=lambda v: _prefs.set(_zoom_key(self._media_type), v),
        )

        self.setFocusPolicy(Qt.StrongFocus)
        self.setStyleSheet(f"QWidget {{ background: {theme.CANVAS_BG}; }}")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setFocusPolicy(Qt.NoFocus)
        self._scroll.setStyleSheet(f"QScrollArea {{ background: {theme.CANVAS_BG}; border: none; }}")
        self._scroll.setVerticalScrollBar(theme.JumpScrollBar())
        self._scroll.viewport().installEventFilter(self)

        self._grid_widget = AspectGridWidget(aspect=_ASPECT, gap=_GAP, margin=_MARGIN)
        self._grid_widget.setStyleSheet(f"background: {theme.CANVAS_BG};")
        self._grid_widget.set_zoom(self._zoom_manager.zoom())
        self._scroll.setWidget(self._grid_widget)
        outer.addWidget(self._scroll)

    # ------------------------------------------------------------------ zoom
    def zoom_manager(self) -> ZoomManager:
        return self._zoom_manager

    def request_reflow(self) -> None:
        """Called by ZoomManager (as a fallback hook) after a zoom change."""
        self._grid_widget.set_zoom(self._zoom_manager.zoom())

    # ------------------------------------------------------------------ data
    def load_pages(self, pages: list[dict]) -> None:
        cells = []
        for page in pages:
            cell = _PageCell(page)
            cells.append(cell)
        self._cells = cells
        self._grid_widget.set_cells(cells)

    # ------------------------------------------------------------ zoom input
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self._scroll.viewport() and event.type() == QEvent.Wheel:
            if self._zoom_manager.handle_wheel_event(event):
                return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if self._zoom_manager.handle_key_event(event, _ZOOM_DEFAULT):
            return
        super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class FlipbookVisualizerWindow(WindowVisualizer):
    """Main window for the Flipbook Visualizer."""

    def __init__(
        self,
        project_path: str,
        media_type: str = "movie",
    ) -> None:
        self._project_path = project_path
        self._media_type   = media_type

        # List of (display_label, flipbook_data) tuples
        self._books: list[tuple[str, dict]] = []
        self._current_idx: int = 0
        self._updating_combo: bool = False

        super().__init__(pref_key="window_flipbook")
        self.setWindowTitle("Crossing — Flipbook Visualizer")
        self.setMinimumSize(640, 400)
        self.resize(1200, 700)

        self._load_all_movies()

    # ------------------------------------------------------------------
    # WindowVisualizer hooks

    def create_browser(self) -> QWidget:
        self._browser_page = _FlipbookBrowserPage(self._media_type)
        return self._browser_page

    def create_inspector(self) -> QWidget:
        panel = TabPanel()

        # ── Movie section ────────────────────────────────────────────────
        movie_wrap = QWidget()
        movie_layout = QVBoxLayout(movie_wrap)
        movie_layout.setContentsMargins(0, 0, 0, 0)
        movie_layout.setSpacing(theme.SECTION_GAP)

        self._combo = QComboBox()
        self._combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._combo.setFocusPolicy(Qt.NoFocus)
        self._combo.setMaxVisibleItems(10)
        self._combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLength)
        attach_combo_popup(self._combo)
        self._combo.setStyleSheet(
            f"QComboBox {{ background: {theme.BTN_BG}; color: {theme.TEXT};"
            f" border: none; border-radius: 3px; padding: 0px 6px;"
            f" min-height: {theme.BTN_H}px; max-height: {theme.BTN_H}px; }}"
            f"QComboBox::drop-down {{ border: none; }}"
        )
        self._combo.currentIndexChanged.connect(self._on_combo_changed)
        self._combo.installEventFilter(self)
        movie_layout.addWidget(self._combo)

        panel.add_section("Movie", movie_wrap, pref_key="flipbook_section_movie")

        # ── Info section ─────────────────────────────────────────────────
        info_wrap = QWidget()
        info_layout = QVBoxLayout(info_wrap)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(0)
        self._info_block = MetadataBlock(_INFO_ROWS)
        info_layout.addWidget(self._info_block)
        panel.add_section("Info", info_wrap, pref_key="flipbook_section_info")

        # ── Tools section (zoom + export) ──────────────────────────────
        tools_wrap = QWidget()
        tools_layout = QVBoxLayout(tools_wrap)
        tools_layout.setContentsMargins(0, 0, 0, 0)
        tools_layout.setSpacing(theme.SECTION_GAP)

        action_btn_style = theme.action_button_stylesheet()

        zoom_row = QHBoxLayout()
        zoom_row.setContentsMargins(0, 0, 0, 0)
        zoom_row.setSpacing(theme.SECTION_GAP)

        zoom_out_btn = QPushButton("Zoom Out")
        zoom_out_btn.setStyleSheet(action_btn_style)
        zoom_out_btn.setFocusPolicy(Qt.NoFocus)
        zoom_out_btn.clicked.connect(
            lambda: self._browser_page.zoom_manager().change_zoom(-_ZOOM_STEP)
        )
        zoom_row.addWidget(zoom_out_btn, 1)

        zoom_in_btn = QPushButton("Zoom In")
        zoom_in_btn.setStyleSheet(action_btn_style)
        zoom_in_btn.setFocusPolicy(Qt.NoFocus)
        zoom_in_btn.clicked.connect(
            lambda: self._browser_page.zoom_manager().change_zoom(_ZOOM_STEP)
        )
        zoom_row.addWidget(zoom_in_btn, 1)

        tools_layout.addLayout(zoom_row)

        self._export_btn = QPushButton("Export PDF")
        self._export_btn.setStyleSheet(action_btn_style)
        self._export_btn.setFocusPolicy(Qt.NoFocus)
        self._export_btn.setToolTip("Render the current movie's flipbook as a PDF")
        self._export_btn.clicked.connect(self._on_export_pdf)
        tools_layout.addWidget(self._export_btn)

        panel.add_section("Tools", tools_wrap, pref_key="flipbook_section_tools")

        self._inspector = Inspector()
        self._inspector.add_tab(panel, "Flipbook")
        return self._inspector

    # ------------------------------------------------------------------
    # Data loading

    def _load_all_movies(self) -> None:
        from data.metadata import get_metadata
        from data.annotate import get_annotation_json_path

        meta_entries = get_metadata(self._project_path, media_type=self._media_type)

        books: list[tuple[str, dict]] = []
        for meta in meta_entries:
            filename = meta.get("filename")
            if not filename:
                continue
            json_path = get_annotation_json_path(
                self._project_path, filename, self._media_type,
            )
            if not json_path.exists():
                continue

            title = meta.get("title") or Path(filename).stem
            year  = meta.get("year")
            label = f"{title} ({year})" if year else title
            books.append((label, {"filename": filename, "loaded": False}))

        self._books = books
        self._updating_combo = True
        self._combo.clear()
        for label, _ in books:
            self._combo.addItem(label)
        self._updating_combo = False

        if books:
            self._show_movie(0)
        else:
            self._info_block.set(
                "status", "No annotated movies found. Run: crossing annotate shot --all"
            )

    def _ensure_loaded(self, idx: int) -> bool:
        """Lazy-load flipbook data for books[idx] if not yet done."""
        if idx < 0 or idx >= len(self._books):
            return False
        label, data = self._books[idx]
        if data.get("loaded"):
            return True

        filename = data.get("filename", "")
        try:
            from generators.flipbook import load_flipbook_data
            fb = load_flipbook_data(
                self._project_path, filename, self._media_type,
            )
            data.update(fb)
            data["loaded"] = True
            self._books[idx] = (label, data)
            return True
        except FileNotFoundError as exc:
            self._info_block.set("status", str(exc))
            return False
        except Exception as exc:
            self._info_block.set("status", f"Error: {exc}")
            return False

    # ------------------------------------------------------------------
    # Display

    def _show_movie(self, idx: int) -> None:
        if not self._books or idx < 0 or idx >= len(self._books):
            return

        self._current_idx = idx
        if not self._ensure_loaded(idx):
            return

        label, data = self._books[idx]
        pages     = data.get("pages", [])
        n_shots   = sum(1 for p in pages if p.get("kind") == "shot")
        n_total   = len(pages)
        has_motif = sum(
            1 for p in pages
            if p.get("kind") == "shot" and p.get("motif", "") not in ("—", "", None)
        )

        self._info_block.load({
            "status": "—",
            "shots":  str(n_shots),
            "motifs": str(has_motif),
            "pages":  str(n_total),
        })
        self._browser_page.load_pages(pages)

        self._updating_combo = True
        self._combo.setCurrentIndex(idx)
        self._updating_combo = False

    # ------------------------------------------------------------------
    # PDF export

    def _on_export_pdf(self) -> None:
        if not self._books:
            return
        idx = self._current_idx
        if not self._ensure_loaded(idx):
            return

        label, data = self._books[idx]
        filename = data.get("filename", "")
        title    = data.get("title", label)
        pages    = data.get("pages", [])

        from generators.flipbook import get_flipbook_output_path, export_flipbook_pdf

        output_path = get_flipbook_output_path(self._project_path, filename)

        self._export_btn.setEnabled(False)
        self._info_block.set("status", "Exporting PDF…")
        QApplication.processEvents()

        try:
            export_flipbook_pdf(pages, output_path, title=title, verbose=False)
            self._info_block.set("status", f"Saved: {output_path}")

            # Try to open the PDF in the desktop viewer
            try:
                import subprocess
                subprocess.Popen(["xdg-open", str(output_path)])
            except Exception:
                pass

        except Exception as exc:
            QMessageBox.critical(
                self, "Export failed",
                f"Could not save PDF:\n{exc}",
            )
            self._info_block.set("status", "Export failed.")
        finally:
            self._export_btn.setEnabled(True)

    # ------------------------------------------------------------------
    # Signal handlers

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """Intercept Home/End/PgUp/PgDn on the movie combo."""
        if event.type() == QEvent.KeyPress:
            if obj is self._combo:
                key = event.key()
                if key == Qt.Key_Home:
                    if self._current_idx > 0:
                        self._show_movie(self._current_idx - 1)
                    return True
                if key == Qt.Key_End:
                    if self._current_idx < len(self._books) - 1:
                        self._show_movie(self._current_idx + 1)
                    return True
                if key in (Qt.Key_PageUp, Qt.Key_PageDown):
                    return True  # PgUp/PgDn are not used in this visualizer
        return super().eventFilter(obj, event)

    def _on_combo_changed(self, idx: int) -> None:
        if self._updating_combo:
            return
        self._show_movie(idx)

    # ------------------------------------------------------------------
    # Keyboard navigation
    #
    # Escape / Ctrl+Q/W / Tab (show-hide inspector) / Shift+Tab (fullscreen)
    # are handled by WindowVisualizer.keyPressEvent(); Ctrl+P (export) and
    # Home/End (previous/next title) are specific to this visualizer.

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        mod = event.modifiers()

        if key == Qt.Key_P and mod & Qt.ControlModifier:
            self._on_export_pdf()
            return

        if key == KEY_PREV_TITLE:
            if self._current_idx > 0:
                self._show_movie(self._current_idx - 1)
            return
        if key == KEY_NEXT_TITLE:
            if self._current_idx < len(self._books) - 1:
                self._show_movie(self._current_idx + 1)
            return
        super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# Public launcher
# ---------------------------------------------------------------------------

def run_visualizer(
    project_path: str,
    media_type: str = "movie",
) -> None:
    """Create QApplication (if needed) and open the flipbook visualizer."""
    from visualizers.launcher import run_visualizer_window

    def _build() -> FlipbookVisualizerWindow:
        global _FONT_FAMILY
        _FONT_FAMILY = _load_flipbook_font()
        return FlipbookVisualizerWindow(project_path, media_type=media_type)

    run_visualizer_window("flipbook", _build)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--media", default="movie")
    parsed = ap.parse_args()
    run_visualizer(parsed.project, media_type=parsed.media)
