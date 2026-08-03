#!/usr/bin/env python3
"""Palette Visualizer — browse per-shot foreground/background colour palettes.

Launched via:
    crossing visualizer palette
    crossing visualizer palette --media gameplay

Layout (canonical WindowVisualizer shell):
  BROWSER   — scrollable, zoomable grid of shot colour swatches
  INSPECTOR — movie selector, info, warnings/palette-strip toggles,
              Export PDF, zoom

Each swatch is a filled rectangle in the shot's background colour, with the
best-frame number printed in the shot's foreground colour.  Swatches are
sized at a fixed 16 × 9 aspect ratio and reflow to best fill the browser
area, the same way Metadata's thumbnail grid does — see
`visualizers.components.aspect_grid.AspectGridWidget`.

Keyboard:
  Home          — previous title
  End           — next title
  Tab           — show/hide inspector
  Shift+Tab     — toggle fullscreen
  Ctrl+wheel / Ctrl+Plus/Minus/0 — zoom the swatch grid
  Escape / Ctrl+Q / Ctrl+W — close
  Ctrl+P        — export the current movie's palette as a PDF
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from styles import theme

from PyQt5.QtCore import Qt, QEvent, QThread, QTimer, pyqtSignal
from tool.shortcuts import KEY_PREV_TITLE, KEY_NEXT_TITLE
from visualizers.window_visualizer import WindowVisualizer
from visualizers.components.aspect_grid import AspectGridWidget
from visualizers.components.zoom_manager import ZoomManager
from visualizers.components.inspector import Inspector
from visualizers.components.tab_panel import TabPanel
from visualizers.components.combo_popup import attach_combo_popup
from visualizers.components.metadata_block import MetadataBlock
from visualizers.components.sweep_bar import SweepBar
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
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
from PyQt5.QtGui import QColor, QPainter

if "QT_QPA_PLATFORM_PLUGIN_PATH" in os.environ:
    del os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"]


# ---------------------------------------------------------------------------
# Background worker: palette JSON loader
# ---------------------------------------------------------------------------

class PaletteLoaderWorker(QThread):
    """Loads palette JSON files one-by-one from disk in a background thread.

    Signals
    -------
    palette_ready(label, data)
        Emitted for each successfully loaded palette JSON, in sorted order.
    finished_signal(total_count)
        Emitted when every file has been processed.
    """

    palette_ready   = pyqtSignal(str, dict)  # (display_label, data_dict)
    finished_signal = pyqtSignal(int)        # total count

    def __init__(self, palette_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self._palette_dir = palette_dir
        self._cancelled   = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        count = 0
        for json_path in sorted(self._palette_dir.glob("*.json")):
            if self._cancelled:
                break
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            movie = data.get("movie", {})
            title = movie.get("title") or json_path.stem
            year  = movie.get("year")
            label = f"{title} ({year})" if year else title
            self.palette_ready.emit(label, data)
            count += 1
        self.finished_signal.emit(count)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ASPECT = 16 / 9  # swatch width : height ratio
_GAP    = 4       # px — gap between swatches
_MARGIN = 10      # px — grid outer margin

# Zoom range/step mirror Metadata's browser page (see metadata_visualizer.py)
# so Ctrl+wheel / Ctrl+Plus/Minus/0 feel identical across visualizers.
_ZOOM_MIN     = 0.60
_ZOOM_MAX     = 3.00
_ZOOM_STEP    = 0.20
_ZOOM_DEFAULT = 1.00


def _zoom_key(media_type: str) -> str:
    return f"palette_browser_zoom_{media_type}"


# Rows shown in the Info section's two-column (tag / info) table — see
# `create_inspector()`.  "status" carries transient loading/export messages;
# the rest is the per-movie summary that used to live in a status label
# below the movie combo.
_INFO_ROWS = ["status", "shots", "method", "created"]


# ---------------------------------------------------------------------------
# Debug helpers
# ---------------------------------------------------------------------------

def _colour_debug_str(colour: dict) -> str:
    """One-line summary of a colour dict for tooltip display.

    Works with both the legacy format (``{"rgb": [...]}``)
    and the richer LAB format that also contains ``"luminance"`` and
    ``"chroma"``.
    """
    rgb = colour.get("rgb")
    if not rgb:
        return "—"
    line = f"rgb({rgb[0]},{rgb[1]},{rgb[2]})"
    lum   = colour.get("luminance")
    chrom = colour.get("chroma")
    lab   = colour.get("lab")
    if lum is not None and chrom is not None:
        line += f"  L={lum:.2f} C={chrom:.2f}"
    if lab:
        line += f"  lab=[{lab[0]},{lab[1]},{lab[2]}]"
    return line


# ---------------------------------------------------------------------------
# Shot swatch widget
# ---------------------------------------------------------------------------

class _ShotCell(QWidget):
    """A single shot swatch: a background-coloured rectangle with the
    best-frame number (or shot index) drawn in the foreground colour."""

    def __init__(
        self,
        shot: dict,
        project_path: str,
        filename: str,
        media_type: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._shot         = shot
        self._project_path = project_path
        self._filename     = filename
        self._media_type   = media_type
        self.setCursor(Qt.PointingHandCursor)
        self._show_warnings: bool = False  # toggled externally; off by default
        self._show_palette:  bool = False  # toggled externally; off by default

        # Warning state derived from diagnostics
        diag = shot.get("diagnostics", {})
        self._near_black_pair   = bool(diag.get("near_black_pair", False))
        self._rescue_applied    = bool(diag.get("rescue_applied",  False))
        self._warn_low_contrast = (
            self._near_black_pair
            or self._rescue_applied
            or diag.get("fg_bg_delta_e", 100.0) < 15.0
        )

        # Palette data (present with the figure-ground pipeline)
        self._fg_palette: list = shot.get("foreground", {}).get("palette", [])
        self._bg_palette: list = shot.get("background", {}).get("palette", [])

        # Build a tooltip with colour values and perceptual metadata for debugging
        bg = shot.get("background", {})
        fg = shot.get("foreground", {})
        idx = shot.get("shot_index", "?")
        shot_id = shot.get("shot_id", "")
        start_t = shot.get("start_time", "")
        method  = shot.get("method", "")
        tt_bg = "bg: " + _colour_debug_str(bg)
        tt_fg = "fg: " + _colour_debug_str(fg)
        tip_parts = [f"Shot {idx}", shot_id, start_t, tt_bg, tt_fg]
        if method:
            tip_parts.append(f"[{method}]")
        if diag:
            de      = diag.get("fg_bg_delta_e")
            rescued = diag.get("rescue_applied")
            reason  = diag.get("rescue_reason")
            nb      = diag.get("near_black_pair")
            if de is not None:
                tip_parts.append(f"\u0394E={de:.1f}")
            if nb:
                tip_parts.append("⚠ near-black pair")
            if rescued:
                tip_parts.append(f"⚠ rescue: {reason}")
            # Figure-ground diagnostics
            m_used  = diag.get("method_used")
            seg     = diag.get("segmentation_used")
            fl      = diag.get("fallback_level")
            if m_used:
                tip_parts.append(f"method: {m_used}")
            if seg:
                tip_parts.append(f"segmentation: {seg}")
            if fl is not None:
                tip_parts.append(f"fallback_level: {fl}")
        self.setToolTip("\n".join(tip_parts))

    def set_warnings_visible(self, visible: bool) -> None:
        if self._show_warnings != visible:
            self._show_warnings = visible
            self.update()

    def set_palette_visible(self, visible: bool) -> None:
        if self._show_palette != visible:
            self._show_palette = visible
            self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self._filename:
            from visualizers.shot_visualizer import open_at_shot
            open_at_shot(
                self._project_path,
                self._filename,
                self._media_type,
                shot_id=self._shot.get("shot_id", ""),
            )
        super().mousePressEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        shot = self._shot
        bg_rgb = shot.get("background", {}).get("rgb") or [60, 60, 60]
        fg_rgb = shot.get("foreground", {}).get("rgb") or [180, 180, 180]

        bg_color = QColor(bg_rgb[0], bg_rgb[1], bg_rgb[2])
        fg_color = QColor(fg_rgb[0], fg_rgb[1], fg_rgb[2])

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), bg_color)

        # Japanese-flag ratio: circle diameter = 3/5 of cell height
        diameter = int(self.height() * 3 / 5)
        cx = self.rect().center().x()
        cy = self.rect().center().y()
        painter.setBrush(fg_color)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(cx - diameter // 2, cy - diameter // 2, diameter, diameter)

        # Warning badge: small filled dot in the top-right corner.
        # Orange = rescue was applied; yellow = near-black or low ΔE (unfixed).
        if self._warn_low_contrast and self._show_warnings:
            dot_r = max(3, self.height() // 8)
            dot_x = self.rect().right() - dot_r * 2 - 2
            dot_y = self.rect().top() + 2
            badge_color = (
                QColor(255, 140,  0)   # orange — rescue was applied
                if self._rescue_applied else
                QColor(220, 200, 50)   # yellow — near-black or low ΔE
            )
            painter.setBrush(badge_color)
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(dot_x, dot_y, dot_r * 2, dot_r * 2)

        # Palette strip: fg palette chips in a thin bar at the bottom.
        # Only drawn when the "Show palette strip" checkbox is active and
        # palette data is present (figure-ground pipeline).
        if self._show_palette and self._fg_palette:
            n = len(self._fg_palette)
            strip_h = max(4, self.height() // 8)
            chip_w  = max(1, self.width() // max(1, n))
            y_strip = self.rect().bottom() - strip_h
            for ci, col in enumerate(self._fg_palette):
                rgb = col.get("rgb") if isinstance(col, dict) else None
                if not rgb:
                    continue
                chip_color = QColor(int(rgb[0]), int(rgb[1]), int(rgb[2]))
                painter.fillRect(ci * chip_w, y_strip, chip_w, strip_h, chip_color)

        painter.end()


# ---------------------------------------------------------------------------
# Browser page: scrollable, zoomable grid of shot swatches for one movie
# ---------------------------------------------------------------------------

class _PaletteBrowserPage(QWidget):
    """Owns the shot-swatch grid for the currently selected movie.

    Composed from the same shared building blocks Metadata's browser page
    uses — `AspectGridWidget` for the best-fit grid reflow (extracted from
    what used to be this file's own `_GridWidget._reflow()`, near-identical
    to Flipbook's) and `ZoomManager` for zoom state/persistence/Ctrl+wheel
    and Ctrl+Plus/Minus/0 handling. See visualizers/components/
    {aspect_grid,zoom_manager}.py.
    """

    def __init__(self, media_type: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._media_type = media_type
        self._cells: list[_ShotCell] = []
        self._show_warnings: bool = False
        self._show_palette:  bool = False

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
    def load_shots(
        self,
        shots: list[dict],
        project_path: str = "",
        filename: str = "",
        media_type: str = "movie",
    ) -> None:
        cells = []
        for shot in shots:
            cell = _ShotCell(shot, project_path, filename, media_type)
            cell.set_warnings_visible(self._show_warnings)
            cell.set_palette_visible(self._show_palette)
            cells.append(cell)
        self._cells = cells
        self._grid_widget.set_cells(cells)

    def set_warnings_visible(self, visible: bool) -> None:
        self._show_warnings = visible
        for cell in self._cells:
            cell.set_warnings_visible(visible)

    def set_palette_visible(self, visible: bool) -> None:
        self._show_palette = visible
        for cell in self._cells:
            cell.set_palette_visible(visible)

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

class PaletteVisualizerWindow(WindowVisualizer):
    """Main window for the Palette Visualizer."""

    def __init__(
        self,
        project_path: str,
        media_type: str = "movie",
    ) -> None:
        self._project_path = project_path
        self._media_type   = media_type

        # List of (display_label, palette_data) tuples, one per loaded JSON
        self._palettes: list[tuple[str, dict]] = []
        self._current_idx: int = 0
        self._updating_combo: bool = False
        self._loader: Optional[PaletteLoaderWorker] = None

        super().__init__(pref_key="window_palette")
        self.setWindowTitle("Palette")
        self.setMinimumSize(600, 400)
        self.resize(1200, 700)

        self._start_loading()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._loader is not None and self._loader.isRunning():
            self._loader.cancel()
            self._loader.wait()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # WindowVisualizer hooks

    def create_browser(self) -> QWidget:
        self._browser_page = _PaletteBrowserPage(self._media_type)
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

        movie_sec = panel.add_section("Movie", movie_wrap, pref_key="palette_section_movie")

        # Accent sweep-bar shown on the section title while palettes are
        # loading in the background — same loading behavior as Illustration's
        # Silhouettes/Engravings tabs, visible even when the section is
        # collapsed.
        self._loading_bar = SweepBar(self)
        self._loading_timer = QTimer(self)
        self._loading_timer.setInterval(20)   # ~50 fps
        self._loading_timer.timeout.connect(self._loading_bar.tick)
        movie_sec.set_subbar(self._loading_bar)

        # ── Info section ─────────────────────────────────────────────────
        info_wrap = QWidget()
        info_layout = QVBoxLayout(info_wrap)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(0)
        self._info_block = MetadataBlock(_INFO_ROWS)
        info_layout.addWidget(self._info_block)
        panel.add_section("Info", info_wrap, pref_key="palette_section_info")

        # ── Display section ──────────────────────────────────────────────
        display_wrap = QWidget()
        display_layout = QVBoxLayout(display_wrap)
        display_layout.setContentsMargins(0, 0, 0, 0)
        display_layout.setSpacing(theme.SECTION_GAP)

        self._warn_checkbox = QCheckBox("Dark-scene warnings")
        self._warn_checkbox.setChecked(False)
        self._warn_checkbox.setFixedHeight(theme.BTN_H)
        self._warn_checkbox.setFocusPolicy(Qt.NoFocus)
        self._warn_checkbox.setToolTip(
            "Show a coloured dot on shots where the palette extraction\n"
            "was difficult (very dark frame or low contrast).\n"
            "Orange = a rescue pass was applied.  Yellow = still low contrast."
        )
        self._warn_checkbox.setStyleSheet(
            f"QCheckBox {{ color: {theme.TEXT};"
            f" min-height: {theme.BTN_H}px; max-height: {theme.BTN_H}px; padding: 0px; }}"
            f"QCheckBox::indicator {{ width: {theme.BTN_ICON}px; height: {theme.BTN_ICON}px; }}"
        )
        self._warn_checkbox.toggled.connect(self._grid_warnings_toggle)
        display_layout.addWidget(self._warn_checkbox)

        self._palette_checkbox = QCheckBox("Show palette strip")
        self._palette_checkbox.setChecked(False)
        self._palette_checkbox.setFixedHeight(theme.BTN_H)
        self._palette_checkbox.setFocusPolicy(Qt.NoFocus)
        self._palette_checkbox.setToolTip(
            "Show a thin strip of palette colours at the bottom of each swatch.\n"
            "Only visible for shots indexed with the figure-ground pipeline."
        )
        self._palette_checkbox.setStyleSheet(
            f"QCheckBox {{ color: {theme.TEXT};"
            f" min-height: {theme.BTN_H}px; max-height: {theme.BTN_H}px; padding: 0px; }}"
            f"QCheckBox::indicator {{ width: {theme.BTN_ICON}px; height: {theme.BTN_ICON}px; }}"
        )
        self._palette_checkbox.toggled.connect(self._grid_palette_toggle)
        display_layout.addWidget(self._palette_checkbox)

        panel.add_section("Display", display_wrap, pref_key="palette_section_display")

        # ── Tools section (zoom + export) ───────────────────────────────
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
        self._export_btn.setToolTip("Render the current movie's palette swatches as a PDF")
        self._export_btn.clicked.connect(self._on_export_pdf)
        tools_layout.addWidget(self._export_btn)

        panel.add_section("Tools", tools_wrap, pref_key="palette_section_tools")

        self._inspector = Inspector()
        self._inspector.add_tab(panel, "Palette")
        return self._inspector

    # ------------------------------------------------------------------
    # Data loading

    def _start_loading(self) -> None:
        palette_dir = (
            Path(self._project_path)
            / "data" / "palettes" / self._media_type
        )
        if not palette_dir.exists():
            self._info_block.set(
                "status", "No palette cache found. Run: crossing index palette create --all"
            )
            return

        self._loading_bar.start()
        self._loading_timer.start()
        self._info_block.set("status", "Loading palettes…")

        self._loader = PaletteLoaderWorker(palette_dir, parent=self)
        self._loader.palette_ready.connect(self._on_palette_ready)
        self._loader.finished_signal.connect(self._on_load_done)
        self._loader.start()

    def _on_palette_ready(self, label: str, data: dict) -> None:
        """Slot: one palette JSON has been loaded by the worker."""
        self._palettes.append((label, data))
        self._updating_combo = True
        self._combo.addItem(label)
        self._updating_combo = False

        # Show the very first palette as soon as it arrives
        if len(self._palettes) == 1:
            self._show_movie(0)

    def _on_load_done(self, count: int) -> None:
        """Slot: all palette JSONs have been loaded."""
        self._loading_timer.stop()
        self._loading_bar.stop()
        if count == 0:
            self._info_block.set("status", "No palette files found.")

    # ------------------------------------------------------------------
    # Display

    def _show_movie(self, idx: int) -> None:
        if not self._palettes or idx < 0 or idx >= len(self._palettes):
            return

        self._current_idx = idx
        label, data = self._palettes[idx]
        shots    = data.get("shots", [])
        summary  = data.get("summary", {})
        processed = summary.get("processed", 0)
        total     = summary.get("shot_count", len(shots))
        created   = data.get("created_at", "")[:10]
        method_label = data.get("method", "border_center_dominant")

        self._info_block.load({
            "status":  "—",
            "shots":   f"{processed}/{total}",
            "method":  method_label,
            "created": created,
        })

        self._browser_page.load_shots(
            shots,
            self._project_path,
            data.get("movie", {}).get("filename", ""),
            self._media_type,
        )

        self._updating_combo = True
        self._combo.setCurrentIndex(idx)
        self._updating_combo = False

    # ------------------------------------------------------------------
    # Signal handlers

    def _on_combo_changed(self, idx: int) -> None:
        if self._updating_combo:
            return
        self._show_movie(idx)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """Intercept Home/End/PgUp/PgDn on the movie combo to override
        native QComboBox jump-to-first/last and cycling behaviour."""
        if obj is self._combo and event.type() == QEvent.KeyPress:
            key = event.key()
            if key == Qt.Key_Home:
                if self._current_idx > 0:
                    self._show_movie(self._current_idx - 1)
                return True
            if key == Qt.Key_End:
                if self._current_idx < len(self._palettes) - 1:
                    self._show_movie(self._current_idx + 1)
                return True
            if key in (Qt.Key_PageUp, Qt.Key_PageDown):
                return True  # PgUp/PgDn are not used in this visualizer
        return super().eventFilter(obj, event)

    def _grid_warnings_toggle(self, checked: bool) -> None:
        self._browser_page.set_warnings_visible(checked)

    def _grid_palette_toggle(self, checked: bool) -> None:
        self._browser_page.set_palette_visible(checked)

    # ------------------------------------------------------------------
    # PDF export

    def _on_export_pdf(self) -> None:
        if not self._palettes:
            return
        idx = self._current_idx
        label, data = self._palettes[idx]
        shots = data.get("shots", [])
        if not shots:
            return

        from generators.palette import get_palette_output_path, export_palette_pdf

        movie = data.get("movie", {})
        filename = movie.get("filename", "")
        title    = movie.get("title", label)
        output_path = get_palette_output_path(self._project_path, filename)

        self._export_btn.setEnabled(False)
        self._info_block.set("status", "Exporting PDF…")
        QApplication.processEvents()

        try:
            export_palette_pdf(shots, output_path, title=title, verbose=False)
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
            if self._current_idx < len(self._palettes) - 1:
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
    """Create QApplication (if needed) and open the palette visualizer."""
    from visualizers.launcher import run_visualizer_window
    run_visualizer_window(
        "palette",
        lambda: PaletteVisualizerWindow(project_path, media_type=media_type),
    )


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--media", default="movie")
    parsed = ap.parse_args()
    run_visualizer(parsed.project, media_type=parsed.media)
