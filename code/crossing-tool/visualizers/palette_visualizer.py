#!/usr/bin/env python3
"""Palette Visualizer — browse per-shot foreground/background colour palettes.

Launched via:
    crossing visualizer palette
    crossing visualizer palette --media gameplay

Layout:
  TOP  — movie selector dropdown + shot-count status label
  MAIN — scrollable grid of shot colour swatches

Each swatch is a filled rectangle in the shot's background colour, with the
best-frame number printed in the shot's foreground colour.  Swatches are
sized at the default 16 × 9 aspect ratio and reflow automatically when the
window is resized.

Keyboard:
  Home          — previous movie
  End           — next movie
  Escape / Ctrl+Q / Ctrl+W — close
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from styles import theme
from styles.theme import save_window_geometry, restore_window_geometry

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from PyQt5.QtGui import QColor, QPainter

if "QT_QPA_PLATFORM_PLUGIN_PATH" in os.environ:
    del os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ASPECT = 16 / 9  # swatch width : height ratio
_GAP    = 4       # px — gap between swatches
_MARGIN = 10      # px — grid outer margin


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
# Grid container (manual flow layout)
# ---------------------------------------------------------------------------

class _GridWidget(QWidget):
    """Holds shot cells in a wrap-around grid that reflows on resize."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._cells: list[_ShotCell] = []
        self._show_warnings: bool = False
        self._show_palette:  bool = False

    def load_shots(
        self,
        shots: list[dict],
        project_path: str = "",
        filename: str = "",
        media_type: str = "movies",
    ) -> None:
        for cell in self._cells:
            cell.setParent(None)  # type: ignore[arg-type]
            cell.deleteLater()
        self._cells = []

        for shot in shots:
            cell = _ShotCell(shot, project_path, filename, media_type, self)
            cell.set_warnings_visible(self._show_warnings)
            cell.set_palette_visible(self._show_palette)
            cell.show()
            self._cells.append(cell)

        self._reflow()

    def _reflow(self) -> None:
        n = len(self._cells)
        if n == 0:
            return

        W = max(1, self.width()  - 2 * _MARGIN)
        H = max(1, self.height() - 2 * _MARGIN)

        # Find the column count that maximises cell area while fitting all
        # shots inside the available W × H area at a fixed aspect ratio.
        best_area = 0.0
        best_cols = 1
        best_cw   = 0.0
        best_ch   = 0.0

        for cols in range(1, n + 1):
            rows = math.ceil(n / cols)
            # Width-first: fill columns across W
            cw = (W - (cols - 1) * _GAP) / cols
            ch = cw / _ASPECT
            # If the resulting rows don't fit vertically, scale from height
            if rows * ch + (rows - 1) * _GAP > H:
                ch = (H - (rows - 1) * _GAP) / rows
                cw = ch * _ASPECT
            if cw <= 0 or ch <= 0:
                continue
            area = cw * ch
            if area > best_area:
                best_area = area
                best_cols = cols
                best_cw   = cw
                best_ch   = ch

        if best_area <= 0:
            return

        cell_w = max(1, int(best_cw))
        cell_h = max(1, int(best_ch))
        rows   = math.ceil(n / best_cols)

        # Centre the grid in the available area
        grid_w = best_cols * cell_w + (best_cols - 1) * _GAP
        grid_h = rows      * cell_h + (rows      - 1) * _GAP
        x0 = _MARGIN + (W - grid_w) // 2
        y0 = _MARGIN + (H - grid_h) // 2

        for i, cell in enumerate(self._cells):
            row, col = divmod(i, best_cols)
            x = x0 + col * (cell_w + _GAP)
            y = y0 + row * (cell_h + _GAP)
            cell.setGeometry(x, y, cell_w, cell_h)

    def set_warnings_visible(self, visible: bool) -> None:
        self._show_warnings = visible
        for cell in self._cells:
            cell.set_warnings_visible(visible)

    def set_palette_visible(self, visible: bool) -> None:
        self._show_palette = visible
        for cell in self._cells:
            cell.set_palette_visible(visible)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._reflow()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class PaletteVisualizerWindow(QMainWindow):
    """Main window for the Palette Visualizer."""

    def __init__(
        self,
        project_path: str,
        media_type: str = "movies",
    ) -> None:
        super().__init__()
        self.setWindowTitle("Crossing — Palette Visualizer")
        self._project_path = project_path
        self._media_type   = media_type

        # List of (display_label, palette_data) tuples, one per loaded JSON
        self._palettes: list[tuple[str, dict]] = []
        self._current_idx: int = 0
        self._updating_combo: bool = False

        self._build_ui()
        self._load_palettes()
        restore_window_geometry(self, "window_palette")

    def closeEvent(self, event) -> None:  # noqa: N802
        save_window_geometry(self, "window_palette")
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # UI construction

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)

        vbox = QVBoxLayout(central)
        vbox.setContentsMargins(0, 6, 0, 0)
        vbox.setSpacing(4)

        # Top bar: label + combo + status
        bar = QHBoxLayout()
        bar.setContentsMargins(10, 0, 10, 0)
        bar.setSpacing(6)

        lbl = QLabel("Movie:")
        lbl.setStyleSheet(
            f"color: {theme.TEXT}; font-family: '{theme.FAMILY_UI}';"
            f" font-size: {theme.BASE_PT}pt;"
        )
        bar.addWidget(lbl)

        self._combo = QComboBox()
        self._combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._combo.currentIndexChanged.connect(self._on_combo_changed)
        bar.addWidget(self._combo, 1)

        self._status_label = QLabel("")
        self._status_label.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-family: '{theme.FAMILY_UI}';"
            f" font-size: {theme.BASE_PT}pt;"
        )
        bar.addWidget(self._status_label)

        self._warn_checkbox = QCheckBox("Dark-scene warnings")
        self._warn_checkbox.setChecked(False)
        self._warn_checkbox.setToolTip(
            "Show a coloured dot on shots where the palette extraction\n"
            "was difficult (very dark frame or low contrast).\n"
            "Orange = a rescue pass was applied.  Yellow = still low contrast."
        )
        self._warn_checkbox.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-family: '{theme.FAMILY_UI}';"
            f" font-size: {theme.BASE_PT}pt;"
        )
        self._warn_checkbox.toggled.connect(self._grid_warnings_toggle)
        bar.addWidget(self._warn_checkbox)

        self._palette_checkbox = QCheckBox("Show palette strip")
        self._palette_checkbox.setChecked(False)
        self._palette_checkbox.setToolTip(
            "Show a thin strip of palette colours at the bottom of each swatch.\n"
            "Only visible for shots indexed with the figure-ground pipeline."
        )
        self._palette_checkbox.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-family: '{theme.FAMILY_UI}';"
            f" font-size: {theme.BASE_PT}pt;"
        )
        self._palette_checkbox.toggled.connect(self._grid_palette_toggle)
        bar.addWidget(self._palette_checkbox)

        vbox.addLayout(bar)

        self._grid = _GridWidget()
        self._grid.setStyleSheet(f"background: {theme.CANVAS_BG};")
        self._grid.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        vbox.addWidget(self._grid, 1)

        self.setMinimumSize(600, 400)
        self.resize(1200, 700)

    # ------------------------------------------------------------------
    # Data loading

    def _load_palettes(self) -> None:
        palette_dir = (
            Path(self._project_path)
            / "data" / "palettes" / self._media_type
        )
        if not palette_dir.exists():
            self._status_label.setText(
                "No palette cache found. Run: crossing index palette create --all"
            )
            return

        palettes: list[tuple[str, dict]] = []
        for json_path in sorted(palette_dir.glob("*.json")):
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            movie = data.get("movie", {})
            title = movie.get("title") or json_path.stem
            year  = movie.get("year")
            label = f"{title} ({year})" if year else title
            palettes.append((label, data))

        self._palettes = palettes

        self._updating_combo = True
        self._combo.clear()
        for label, _ in palettes:
            self._combo.addItem(label)
        self._updating_combo = False

        if palettes:
            self._show_movie(0)
        else:
            self._status_label.setText("No palette files found.")

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

        self._status_label.setText(
            f"{processed}/{total} shots with palette  ·  {method_label}  ·  {created}"
        )

        self._grid.load_shots(
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

    def _grid_warnings_toggle(self, checked: bool) -> None:
        self._grid.set_warnings_visible(checked)

    def _grid_palette_toggle(self, checked: bool) -> None:
        self._grid.set_palette_visible(checked)

    # ------------------------------------------------------------------
    # Keyboard navigation

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        mod = event.modifiers()

        if key == Qt.Key_Escape or (
            key in (Qt.Key_Q, Qt.Key_W) and mod & Qt.ControlModifier
        ):
            self.close()
            return

        if key == Qt.Key_Home:
            if self._current_idx > 0:
                self._show_movie(self._current_idx - 1)
        elif key == Qt.Key_End:
            if self._current_idx < len(self._palettes) - 1:
                self._show_movie(self._current_idx + 1)
        else:
            super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# Public launcher
# ---------------------------------------------------------------------------

def run_visualizer(
    project_path: str,
    media_type: str = "movies",
) -> None:
    """Create QApplication (if needed) and open the palette visualizer."""
    app = QApplication.instance() or QApplication(sys.argv)
    theme.apply_theme(app)
    win = PaletteVisualizerWindow(project_path, media_type=media_type)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--media", default="movies")
    parsed = ap.parse_args()
    run_visualizer(parsed.project, media_type=parsed.media)
