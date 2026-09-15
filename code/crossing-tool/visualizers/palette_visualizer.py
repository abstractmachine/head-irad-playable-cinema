#!/usr/bin/env python3
"""Palette Visualizer — generate, review, correct and author two-colour palettes.

Launched via:
    crossing visualizer palette
    crossing visualizer palette --media gameplay

Layout (canonical WindowVisualizer shell):
  BROWSER   — All Frames grid, or one Single Frame, plus the shared
              Shotlist timeline scrubber along the bottom
  INSPECTOR — media selector, view modes, generation controls, proposal
              choices 1-4, review actions, manual figure/background authoring

Every persistent mutation goes through ``services.palette_review`` — the same
module the CLI calls. Nothing here decides what a palette is.

Keyboard:
  C             — Create Palette for the current frame (this key means nothing else)
  S / A         — Single Frame / All Frames
  1 2 3 4       — accept that proposal and advance
  P / I         — toggle palette / image display (independent)
  F / B         — assign the current manual selection to figure / background
  Right / Left  — next / previous frame
  Delete/Backspace — reset this frame's accepted/manual palette
  Ctrl+wheel, Ctrl+Plus/Minus/0 — zoom the All Frames grid
  Tab / Shift+Tab / Escape — inspector / fullscreen / close

Create All Palettes and Clear All have NO keyboard shortcut by design; they are
Inspector buttons (and CLI commands) rather than rapid-review keystrokes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from styles import theme

if "QT_QPA_PLATFORM_PLUGIN_PATH" in os.environ:
    del os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"]

import numpy as np
from PyQt5.QtCore import Qt, QEvent, QPoint, QRect, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QBrush, QColor, QImage, QPainter, QPen, QPixmap, QPolygon
from PyQt5.QtWidgets import (
    QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

from data import palette_review as store
from visualizers.window_visualizer import WindowVisualizer
from visualizers.components.aspect_grid import AspectGridWidget
from visualizers.components.combo_popup import style_canonical_combo
from visualizers.components.inspector import Inspector
from visualizers.components.metadata_block import MetadataBlock, status_label_stylesheet
from visualizers.components.sweep_bar import SweepBar
from visualizers.components.tab_panel import TabPanel
from visualizers.components.timeline_scrubber import TimelineHitArea, TimelineScrollBar
from visualizers.components.zoom_manager import ZoomManager

_ASPECT = 16 / 9
_GAP, _MARGIN = 4, 10
_ZOOM_MIN, _ZOOM_MAX, _ZOOM_STEP, _ZOOM_DEFAULT = 0.60, 3.00, 0.20, 1.00
# Scrub grab target, in SCROLLBAR_W units. Shotlist's default of 15 leaves a
# tall empty band under a browser grid; four is a normal control strip.
SCRUB_BARS = 4

MODE_ALL = "all"
MODE_SINGLE = "single"
CHOICE_KEYS = ("1", "2", "3", "4")

# How the manual SAM/pipette interaction works. Kept as hover help rather than
# a paragraph in the Inspector — see the Inspector prose rule in agents.md.
MANUAL_HELP = (
    "Click the frame to segment it\n"
    "Click a region to select it\n"
    "Shift+Click to add another region\n"
    "Click a selected region to remove it\n"
    "Alt+Click pipettes Figure\n"
    "Ctrl+Click pipettes Background\n"
    "F assigns Figure   ·   B assigns Background"
)

# Small state marks in Mosaic — borders, not text overlays.
STATE_COLOURS = {
    store.STATE_NOT_GENERATED: (70, 70, 78),
    store.STATE_GENERATING: (90, 150, 220),
    store.STATE_GENERATION_FAILED: (216, 92, 84),
    store.STATE_UNREVIEWED: (226, 196, 90),
    store.STATE_ACCEPTED: (110, 190, 120),
    store.STATE_REJECTED: (200, 110, 200),
    store.STATE_MANUAL: (90, 200, 200),
    store.STATE_MANUAL_INCOMPLETE: (226, 148, 60),
}


def _zoom_key(media_type: str) -> str:
    return f"palette_browser_zoom_{media_type}"


def _qcolor(colour: dict | None, fallback=(40, 40, 46)) -> QColor:
    rgb = (colour or {}).get("rgb") or fallback
    return QColor(int(rgb[0]), int(rgb[1]), int(rgb[2]))


def _hex(rgb) -> str:
    values = [int(value) for value in rgb]
    return "#{:02x}{:02x}{:02x}".format(*values)


def _readable_on(colour: dict | None) -> QColor:
    """Pick a legible numeral colour for an arbitrary measured background."""
    rgb = (colour or {}).get("rgb") or [0, 0, 0]
    luminance = (rgb[0] * 299 + rgb[1] * 587 + rgb[2] * 114) / 1000
    return QColor(theme.ACCENT_TEXT) if luminance > 140 else QColor(theme.TEXT)


def preview_quadrants(entry: dict | None) -> list:
    """The four proposal palettes, positionally keyed 1-4.

    A quadrant is filled only when that choice is actually selectable and has
    both measured colours; an unavailable, converged or refusal choice stays
    empty so the preview never offers a palette the reviewer cannot press.
    """
    entry = entry or {}
    proposals = entry.get("proposals") or {}
    quadrants = []
    for key in CHOICE_KEYS:
        proposal = proposals.get(key) or {}
        colours = [item for item in (proposal.get("colours") or []) if item]
        if proposal.get("active") and len(colours) == 2:
            quadrants.append({"key": key, "figure": colours[0],
                              "background": colours[1]})
        else:
            quadrants.append(None)
    return quadrants


def _paint_pair(painter, rect, figure, background) -> None:
    """One measured palette: background fill with the figure colour centred."""
    painter.fillRect(rect, _qcolor(background))
    diameter = max(4, int(min(rect.width(), rect.height()) * 0.55))
    painter.setBrush(_qcolor(figure))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(rect.center().x() - diameter // 2,
                        rect.center().y() - diameter // 2, diameter, diameter)


def preview_cells(rect, count: int = 4, columns: int = 2) -> list:
    """Tile *rect* into *count* cells. Two columns for a frame, four for a strip."""
    rows = max(1, -(-count // columns))
    cells = []
    for index in range(count):
        column, row = index % columns, index // columns
        left = rect.left() + (rect.width() * column) // columns
        right = rect.left() + (rect.width() * (column + 1)) // columns
        top = rect.top() + (rect.height() * row) // rows
        bottom = rect.top() + (rect.height() * (row + 1)) // rows
        cells.append(QRect(left, top, right - left, bottom - top))
    return cells


def paint_preview(painter, rect, quadrants, *, numbers: bool = False,
                  columns: int = 2) -> None:
    """Paint the 1-4 proposal palettes inside *rect*."""
    quadrants = list(quadrants[:4])
    cells = preview_cells(rect, len(quadrants), columns)
    for quadrant, cell in zip(quadrants, cells):
        if quadrant is None:
            painter.fillRect(cell, QColor(theme.CELL_BG))
            continue
        _paint_pair(painter, cell, quadrant["figure"], quadrant["background"])
        if numbers and cell.height() >= 34:
            painter.setPen(_readable_on(quadrant["background"]))
            painter.setFont(theme.font_ui(bold=True))
            painter.drawText(cell.adjusted(6, 4, -6, -4),
                             Qt.AlignTop | Qt.AlignLeft, quadrant["key"])
    painter.setBrush(Qt.NoBrush)
    painter.setPen(QPen(QColor(theme.CANVAS_BG), 1))
    for cell in cells:
        painter.drawRect(cell)


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------

class _GenerationWorker(QThread):
    """Runs proposal generation off the GUI thread, for one media item."""

    progress = pyqtSignal(int, int, str, str)
    done = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, project_path: str, filename: str, media_type: str, *,
                 shot_id: str | None = None, force: bool = False, parent=None) -> None:
        super().__init__(parent)
        self._target = (project_path, filename, media_type)
        self._shot_id = shot_id
        self._force = force
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        from services import palette_review

        project_path, filename, media_type = self._target
        models = None
        try:
            models = palette_review.load_models(project_path)
            if self._shot_id:
                self.progress.emit(0, 1, self._shot_id, "running")
                result = palette_review.create_frame(
                    project_path, filename, media_type, self._shot_id,
                    models=models, force=self._force,
                )
                self.progress.emit(1, 1, self._shot_id, result["status"])
                self.done.emit({
                    "considered": 1,
                    "generated": int(result["status"] == "generated"),
                    "skipped": int(result["status"] == "skipped"),
                    "failed": int(result["status"] == "failed"),
                    "complete": True, "errors": [],
                })
                return
            self.done.emit(palette_review.create_all(
                project_path, filename, media_type, models=models,
                force=self._force,
                on_progress=lambda done, total, shot, status:
                    self.progress.emit(done, total, shot, status),
                should_cancel=lambda: self._cancelled,
            ))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            if models is not None:
                from services import palette_review as service

                service._free_models(models)


class _MaskWorker(QThread):
    """One interactive segmentation request for manual authoring."""

    masks_ready = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, project_path: str, image_path: str, concept: str,
                 parent=None) -> None:
        super().__init__(parent)
        self._project_path = project_path
        self._image_path = image_path
        self._concept = concept

    def run(self) -> None:
        try:
            from PIL import Image
            from services.silhouette import load_sam_model
            from tool import prefs as _prefs

            name = _prefs.get("model_segmentation") or "sam3"
            segmenter, _effective, _device = load_sam_model(self._project_path, name)
            with Image.open(self._image_path) as opened:
                image = opened.convert("RGB")
            masks = segmenter.segment_concept(image, self._concept)
            masks.sort(key=lambda item: -float(item.get("area", 0) or 0))
            self.masks_ready.emit(masks)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Mosaic cell
# ---------------------------------------------------------------------------

class _FrameCell(QWidget):
    """One frame in Mosaic: its palette, its image, and a state border."""

    clicked = pyqtSignal(int)

    def __init__(self, index: int, item: dict, parent=None) -> None:
        super().__init__(parent)
        self._index = index
        self._item = item
        self._pixmap: Optional[QPixmap] = None
        self._show_palette = True
        self._show_image = True
        self._selected = False
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(f"{item.get('shot_id', '')}\n{item.get('state', '')}")

    def set_visibility(self, palette: bool, image: bool) -> None:
        self._show_palette, self._show_image = palette, image
        self.update()

    def set_selected(self, selected: bool) -> None:
        if self._selected != selected:
            self._selected = selected
            self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._index)
        super().mousePressEvent(event)

    def _load_pixmap(self) -> Optional[QPixmap]:
        if self._pixmap is None and self._item.get("available"):
            pixmap = QPixmap(self._item["image"])
            self._pixmap = pixmap if not pixmap.isNull() else QPixmap()
        if self._pixmap is not None and not self._pixmap.isNull():
            return self._pixmap
        return None

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        final = self._item.get("final") or {}
        background = final.get(store.ROLE_BACKGROUND)
        figure = final.get(store.ROLE_FIGURE)
        quadrants = self._item.get("preview") or []
        previewing = (self._show_palette and not final
                      and any(quadrants))

        if self._show_palette and background:
            painter.fillRect(self.rect(), _qcolor(background))
        elif previewing:
            paint_preview(painter, self.rect(), quadrants)
        else:
            painter.fillRect(self.rect(), QColor(theme.CELL_BG))

        if self._show_image:
            pixmap = self._load_pixmap()
            if pixmap is not None:
                scaled = pixmap.scaled(self.size(), Qt.KeepAspectRatio,
                                       Qt.SmoothTransformation)
                if self._show_palette and (figure or previewing):
                    painter.setOpacity(0.55)
                painter.drawPixmap((self.width() - scaled.width()) // 2,
                                   (self.height() - scaled.height()) // 2, scaled)
                painter.setOpacity(1.0)

        if self._show_palette and figure:
            diameter = max(6, int(self.height() * 3 / 5))
            painter.setBrush(_qcolor(figure))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(self.rect().center().x() - diameter // 2,
                                self.rect().center().y() - diameter // 2,
                                diameter, diameter)

        painter.setBrush(Qt.NoBrush)
        pen = QPen(QColor(*STATE_COLOURS.get(self._item.get("state"), (70, 70, 78))))
        pen.setWidth(4 if self._selected else 2)
        painter.setPen(pen)
        painter.drawRect(self.rect().adjusted(1, 1, -2, -2))
        if self._selected:
            painter.setPen(QPen(QColor(theme.ACCENT), 2))
            painter.drawRect(self.rect().adjusted(4, 4, -5, -5))
        painter.end()


# ---------------------------------------------------------------------------
# Whole-frame canvas with mask selection and pipette
# ---------------------------------------------------------------------------

class _FrameCanvas(QLabel):
    """One frame, its selectable mask overlays, and the manual pipette."""

    pipette = pyqtSignal(int, int, int, str)
    sam_requested = pyqtSignal(int, int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(320, 240)
        self.setStyleSheet(f"background: {theme.CANVAS_BG};")
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.NoFocus)
        self._image: Optional[QImage] = None
        self._pixmap: Optional[QPixmap] = None
        self._blobs: list[dict] = []
        self._selected: set[int] = set()
        self._show_image = True
        self._show_palette = True
        self._palette: dict = {}
        self._preview: list = []

    def set_frame(self, path: str | None) -> None:
        image = QImage(path) if path else None
        self._image = image if image is not None and not image.isNull() else None
        self._pixmap = QPixmap.fromImage(self._image) if self._image else None
        self._blobs, self._selected = [], set()
        self.update()

    def set_blobs(self, blobs: list[dict]) -> None:
        self._blobs = blobs
        self._selected = set()
        self.update()

    def blobs(self) -> list[dict]:
        return self._blobs

    def selected_indices(self) -> list[int]:
        return sorted(self._selected)

    def toggle_selection(self, index: int, additive: bool) -> None:
        """Click selects; Shift+Click accumulates; clicking a selected one removes it."""
        if index in self._selected:
            self._selected.discard(index)
        elif additive:
            self._selected.add(index)
        else:
            self._selected = {index}
        self.update()

    def clear_selection(self) -> None:
        self._selected = set()
        self.update()

    def set_visibility(self, palette: bool, image: bool) -> None:
        self._show_palette, self._show_image = palette, image
        self.update()

    def set_palette(self, palette: dict) -> None:
        self._palette = palette or {}
        self.update()

    def set_preview(self, quadrants: list) -> None:
        self._preview = list(quadrants or [])
        self.update()

    def _layout(self) -> tuple[int, int, float]:
        if self._pixmap is None:
            return 0, 0, 1.0
        scale = min(self.width() / max(1, self._pixmap.width()),
                    self.height() / max(1, self._pixmap.height()))
        return (int((self.width() - self._pixmap.width() * scale) // 2),
                int((self.height() - self._pixmap.height() * scale) // 2), scale)

    def _source_pos(self, x: int, y: int):
        if self._image is None:
            return None
        ox, oy, scale = self._layout()
        sx = int((x - ox) / max(scale, 1e-6))
        sy = int((y - oy) / max(scale, 1e-6))
        if 0 <= sx < self._image.width() and 0 <= sy < self._image.height():
            return sx, sy
        return None

    def _blob_at(self, x: int, y: int) -> int:
        ox, oy, scale = self._layout()
        best, best_area = -1, float("inf")
        for index, blob in enumerate(self._blobs):
            polygon = blob.get("polygon") or []
            if len(polygon) < 3:
                continue
            points = QPolygon([QPoint(int(px * scale + ox), int(py * scale + oy))
                               for px, py in polygon])
            if points.containsPoint(QPoint(x, y), Qt.OddEvenFill):
                area = float(blob.get("area", 0) or 0)
                if area < best_area:
                    best, best_area = index, area
        return best

    def mousePressEvent(self, event) -> None:  # noqa: N802
        modifiers = event.modifiers()
        position = self._source_pos(event.x(), event.y())
        if modifiers & (Qt.AltModifier | Qt.ControlModifier):
            if position is not None and self._image is not None:
                colour = self._image.pixelColor(*position)
                role = (store.ROLE_FIGURE if modifiers & Qt.AltModifier
                        else store.ROLE_BACKGROUND)
                self.pipette.emit(colour.red(), colour.green(), colour.blue(), role)
            return
        index = self._blob_at(event.x(), event.y())
        if index >= 0:
            self.toggle_selection(index, bool(modifiers & Qt.ShiftModifier))
        elif position is not None:
            self.sam_requested.emit(*position)
        super().mousePressEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(theme.CANVAS_BG))
        if self._pixmap is None:
            painter.setPen(QColor(theme.TEXT_DIM))
            painter.drawText(self.rect(), Qt.AlignCenter, "No frame")
            painter.end()
            return

        ox, oy, scale = self._layout()
        width = int(self._pixmap.width() * scale)
        height = int(self._pixmap.height() * scale)
        frame = QRect(ox, oy, width, height)
        previewing = (self._show_palette and not self._palette
                      and any(self._preview))
        if self._show_image:
            painter.drawPixmap(ox, oy, self._pixmap.scaled(
                width, height, Qt.IgnoreAspectRatio, Qt.SmoothTransformation))
        elif previewing:
            paint_preview(painter, frame, self._preview, numbers=True)
        else:
            painter.fillRect(frame, QColor(theme.CELL_BG))

        for index, blob in enumerate(self._blobs):
            polygon = blob.get("polygon") or []
            if len(polygon) < 3:
                continue
            points = QPolygon([QPoint(int(px * scale + ox), int(py * scale + oy))
                               for px, py in polygon])
            chosen = index in self._selected
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(theme.ACCENT) if chosen
                                    else QColor(120, 120, 130)))
            painter.setOpacity(0.42 if chosen else 0.16)
            painter.drawPolygon(points)
            painter.setOpacity(1.0)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(theme.ACCENT) if chosen
                                else QColor(150, 150, 160), 3 if chosen else 1))
            painter.drawPolygon(points)

        if self._show_palette and self._palette:
            bar = max(28, int(height * 0.10))
            for offset, role in enumerate(store.ROLES):
                colour = self._palette.get(role)
                if colour:
                    painter.fillRect(ox + offset * (width // 2), oy + height - bar,
                                     width // 2, bar, _qcolor(colour))
        elif previewing and self._show_image:
            # Image is visible, so the proposals ride along the bottom edge.
            bar = max(34, int(height * 0.14))
            paint_preview(painter, QRect(ox, oy + height - bar, width, bar),
                          self._preview, numbers=True, columns=4)
        painter.end()


# ---------------------------------------------------------------------------
# Browser page
# ---------------------------------------------------------------------------

class _PaletteBrowserPage(QWidget):
    """All Frames / Single Frame stack plus the shared Shotlist scrubber."""

    frame_selected = pyqtSignal(int)

    def __init__(self, media_type: str, parent=None) -> None:
        super().__init__(parent)
        self._media_type = media_type
        self._cells: list[_FrameCell] = []
        self._show_palette, self._show_image = True, True

        from tool import prefs as _prefs

        self._zoom_manager = ZoomManager(
            self,
            float(_prefs.get(_zoom_key(media_type), _ZOOM_DEFAULT) or _ZOOM_DEFAULT),
            _ZOOM_MIN, _ZOOM_MAX, _ZOOM_STEP,
            persist_cb=lambda value: _prefs.set(_zoom_key(self._media_type), value),
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
        self._scroll.setFocusPolicy(Qt.NoFocus)
        self._scroll.setStyleSheet(
            f"QScrollArea {{ background: {theme.CANVAS_BG}; border: none; }}")
        self._scroll.setVerticalScrollBar(theme.JumpScrollBar())
        self._scroll.viewport().installEventFilter(self)
        self._grid = AspectGridWidget(aspect=_ASPECT, gap=_GAP, margin=_MARGIN)
        self._grid.set_zoom(self._zoom_manager.zoom())
        self._scroll.setWidget(self._grid)

        self._canvas = _FrameCanvas()

        self._stack = QStackedWidget()
        self._stack.addWidget(self._scroll)
        self._stack.addWidget(self._canvas)
        outer.addWidget(self._stack, 1)

        self._scrub_bar = TimelineScrollBar()
        # A plain QWidget does not paint an ancestor's stylesheet background,
        # so without this the grab target renders in the default window grey
        # and reads as a foreign panel under the dark browser canvas.
        self._scrub = TimelineHitArea(self._scrub_bar, bars=SCRUB_BARS)
        self._scrub.setAttribute(Qt.WA_StyledBackground, True)
        self._scrub.setStyleSheet(f"background: {theme.CANVAS_BG};")
        self._scrub_bar.valueChanged.connect(
            lambda value: self.frame_selected.emit(int(value)))
        outer.addWidget(self._scrub)

    def canvas(self) -> _FrameCanvas:
        return self._canvas

    def zoom_manager(self) -> ZoomManager:
        return self._zoom_manager

    def request_reflow(self) -> None:
        self._grid.set_zoom(self._zoom_manager.zoom())

    def mode(self) -> str:
        return MODE_ALL if self._stack.currentIndex() == 0 else MODE_SINGLE

    def set_mode(self, mode: str) -> None:
        self._stack.setCurrentIndex(0 if mode == MODE_ALL else 1)

    def set_frames(self, frames: list[dict]) -> None:
        cells = []
        for index, item in enumerate(frames):
            cell = _FrameCell(index, item)
            cell.set_visibility(self._show_palette, self._show_image)
            cell.clicked.connect(self.frame_selected.emit)
            cells.append(cell)
        self._cells = cells
        self._grid.set_cells(cells)
        self._scrub_bar.blockSignals(True)
        self._scrub_bar.setRange(0, max(0, len(frames) - 1))
        self._scrub_bar.setPageStep(max(1, len(frames) // 20))
        self._scrub_bar.blockSignals(False)

    def set_current(self, index: int) -> None:
        for position, cell in enumerate(self._cells):
            cell.set_selected(position == index)
        self._scrub_bar.blockSignals(True)
        self._scrub_bar.setValue(index)
        self._scrub_bar.blockSignals(False)

    def set_visibility(self, palette: bool, image: bool) -> None:
        self._show_palette, self._show_image = palette, image
        for cell in self._cells:
            cell.set_visibility(palette, image)
        self._canvas.set_visibility(palette, image)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self._scroll.viewport() and event.type() == QEvent.Wheel:
            if self._zoom_manager.handle_wheel_event(event):
                return True
        return super().eventFilter(obj, event)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class PaletteVisualizerWindow(WindowVisualizer):
    """Generate, review, correct and author two-colour palettes for one movie."""

    def __init__(self, project_path: str, media_type: str = "movie") -> None:
        self._project_path = project_path
        self._media_type = media_type if media_type in ("movie", "gameplay") else "movie"
        self._films: list[dict] = []
        self._filename = ""
        self._frames: list[dict] = []
        self._record: dict = {}
        self._current = 0
        self._updating = False
        self._show_palette, self._show_image = True, True
        self._worker: Optional[_GenerationWorker] = None
        self._mask_worker: Optional[_MaskWorker] = None

        super().__init__(pref_key="window_palette")
        self.setWindowTitle("Palette")
        self.setMinimumSize(720, 480)
        self.resize(1280, 780)
        self.set_mode(MODE_ALL)
        QTimer.singleShot(0, self._load_films)

    # ------------------------------------------------------------------ shell
    def create_browser(self) -> QWidget:
        self._browser = _PaletteBrowserPage(self._media_type)
        self._browser.frame_selected.connect(self._select_frame)
        canvas = self._browser.canvas()
        canvas.pipette.connect(self._on_pipette)
        canvas.sam_requested.connect(self._on_sam_requested)
        return self._browser

    def create_inspector(self) -> QWidget:
        panel = TabPanel()

        media = QWidget()
        media_layout = QVBoxLayout(media)
        media_layout.setContentsMargins(0, 0, 0, 0)
        media_layout.setSpacing(theme.SECTION_GAP)
        self._media_combo = QComboBox()
        self._media_combo.addItems(["movie", "gameplay"])
        self._media_combo.setCurrentText(self._media_type)
        style_canonical_combo(self._media_combo)
        self._media_combo.currentTextChanged.connect(self._on_media_type_changed)
        media_layout.addWidget(self._media_combo)
        self._film_combo = QComboBox()
        style_canonical_combo(self._film_combo)
        self._film_combo.currentIndexChanged.connect(self._on_film_changed)
        media_layout.addWidget(self._film_combo)
        panel.add_section("Media", media, pref_key="palette_section_media")

        view = QWidget()
        view_layout = QVBoxLayout(view)
        view_layout.setContentsMargins(0, 0, 0, 0)
        view_layout.setSpacing(theme.SECTION_GAP)
        self._mode_btns: dict[str, QPushButton] = {}
        for label, mode, tip in (
            ("Single Frame   S", MODE_SINGLE,
             "Show the selected frame on its own for review and manual editing"),
            ("All Frames   A", MODE_ALL,
             "Show every frame of the selected movie/gameplay as a grid"),
        ):
            button = QPushButton(label)
            button.setStyleSheet(theme.action_button_stylesheet())
            button.setFocusPolicy(Qt.NoFocus)
            button.setCheckable(True)
            button.setToolTip(tip)
            button.clicked.connect(lambda _checked, m=mode: self.set_mode(m))
            self._mode_btns[mode] = button
            view_layout.addWidget(button)
        self._toggle_btns: dict[str, QPushButton] = {}
        for label, name, tip in (
            ("Palette   P", "palette", "Show the measured palette, or the 1-4 "
                                       "proposal preview when none is chosen yet"),
            ("Image   I", "image", "Show the source frame"),
        ):
            button = QPushButton(label)
            button.setStyleSheet(theme.action_button_stylesheet())
            button.setFocusPolicy(Qt.NoFocus)
            button.setCheckable(True)
            button.setChecked(True)
            button.setToolTip(tip)
            button.clicked.connect(
                lambda _checked, n=name: self._toggle_display(n))
            self._toggle_btns[name] = button
            view_layout.addWidget(button)
        panel.add_section("View", view, pref_key="palette_section_view")

        generate = QWidget()
        generate_layout = QVBoxLayout(generate)
        generate_layout.setContentsMargins(0, 0, 0, 0)
        generate_layout.setSpacing(theme.SECTION_GAP)
        self._create_btn = QPushButton("Create Palette   C")
        self._create_btn.setStyleSheet(theme.action_button_stylesheet())
        self._create_btn.setFocusPolicy(Qt.NoFocus)
        self._create_btn.setToolTip(
            "Generate palette proposals for the current frame")
        self._create_btn.clicked.connect(self.create_palette)
        generate_layout.addWidget(self._create_btn)
        # Deliberately no keyboard shortcut: batch generation is an explicit
        # button press (or CLI command), never a rapid-review keystroke.
        self._create_all_btn = QPushButton("Create All Palettes")
        self._create_all_btn.setStyleSheet(theme.action_button_stylesheet())
        self._create_all_btn.setFocusPolicy(Qt.NoFocus)
        self._create_all_btn.setToolTip(
            "Generate proposals for every frame of the selected "
            "movie/gameplay (no keyboard shortcut)")
        self._create_all_btn.clicked.connect(self.create_all_palettes)
        generate_layout.addWidget(self._create_all_btn)
        # Progress/summary line. Hidden while empty so Generate never shows a
        # blank row between runs.
        self._progress_lbl = QLabel("")
        self._progress_lbl.setStyleSheet(status_label_stylesheet())
        self._progress_lbl.setWordWrap(True)
        self._progress_lbl.setVisible(False)
        generate_layout.addWidget(self._progress_lbl)
        generate_section = panel.add_section("Generate", generate,
                                             pref_key="palette_section_generate")
        self._generate_section = generate_section
        self._sweep = SweepBar(self)
        self._sweep_timer = QTimer(self)
        self._sweep_timer.setInterval(20)
        self._sweep_timer.timeout.connect(self._sweep.tick)
        generate_section.set_subbar(self._sweep)

        review = QWidget()
        review_layout = QVBoxLayout(review)
        review_layout.setContentsMargins(0, 0, 0, 0)
        review_layout.setSpacing(theme.SECTION_GAP)
        choices_wrap = QWidget()
        choices_row = QHBoxLayout(choices_wrap)
        choices_row.setContentsMargins(0, 0, 0, 0)
        choices_row.setSpacing(theme.SECTION_GAP)
        self._choice_btns: dict[str, QPushButton] = {}
        for key in CHOICE_KEYS:
            button = QPushButton(key)
            button.setStyleSheet(theme.action_button_stylesheet())
            button.setFocusPolicy(Qt.NoFocus)
            button.clicked.connect(lambda _checked, k=key: self.accept_choice(k))
            self._choice_btns[key] = button
            choices_row.addWidget(button)
        review_layout.addWidget(choices_wrap)
        self._choice_lbl = QLabel("")
        self._choice_lbl.setStyleSheet(status_label_stylesheet())
        self._choice_lbl.setWordWrap(True)
        review_layout.addWidget(self._choice_lbl)
        self._reject_btn = QPushButton("None / Reject Proposals")
        self._reject_btn.setStyleSheet(theme.action_button_stylesheet())
        self._reject_btn.setFocusPolicy(Qt.NoFocus)
        self._reject_btn.clicked.connect(self.reject_proposals)
        review_layout.addWidget(self._reject_btn)
        self._reset_btn = QPushButton("Reset Palette   Del")
        self._reset_btn.setStyleSheet(theme.action_button_stylesheet())
        self._reset_btn.setFocusPolicy(Qt.NoFocus)
        self._reset_btn.clicked.connect(self.reset_frame)
        review_layout.addWidget(self._reset_btn)
        panel.add_section("Review", review, pref_key="palette_section_review")

        manual = QWidget()
        manual_layout = QVBoxLayout(manual)
        manual_layout.setContentsMargins(0, 0, 0, 0)
        manual_layout.setSpacing(theme.SECTION_GAP)
        manual.setToolTip(MANUAL_HELP)
        # Not an action button, so it has to carry the canonical tooltip rule
        # itself or the Inspector's bare container styles render it black.
        manual.setStyleSheet(theme.tooltip_stylesheet())
        for label, role in (("Figure   F", store.ROLE_FIGURE),
                            ("Background   B", store.ROLE_BACKGROUND)):
            button = QPushButton(label)
            button.setStyleSheet(theme.action_button_stylesheet())
            button.setFocusPolicy(Qt.NoFocus)
            button.setToolTip(MANUAL_HELP)
            button.clicked.connect(lambda _checked, r=role: self.assign_role(r))
            manual_layout.addWidget(button)
        # Compact transient state only (region count, segmentation errors).
        self._manual_lbl = QLabel("")
        self._manual_lbl.setStyleSheet(
            status_label_stylesheet() + theme.tooltip_stylesheet())
        self._manual_lbl.setWordWrap(True)
        self._manual_lbl.setVisible(False)
        self._manual_lbl.setToolTip(MANUAL_HELP)
        manual_layout.addWidget(self._manual_lbl)
        manual_section = panel.add_section("Manual", manual,
                                           pref_key="palette_section_manual")
        manual_section.setToolTip(MANUAL_HELP)
        manual_section.setStyleSheet(theme.tooltip_stylesheet())

        info = QWidget()
        info_layout = QVBoxLayout(info)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(0)
        self._info = MetadataBlock(
            ["frame", "shot", "state", "figure", "background"])
        info_layout.addWidget(self._info)
        panel.add_section("Info", info, pref_key="palette_section_info")

        tools = QWidget()
        tools_layout = QVBoxLayout(tools)
        tools_layout.setContentsMargins(0, 0, 0, 0)
        tools_layout.setSpacing(theme.SECTION_GAP)
        # No keyboard shortcut for Clear All either — it is destructive.
        self._clear_btn = QPushButton("Clear All")
        self._clear_btn.setStyleSheet(theme.action_button_stylesheet())
        self._clear_btn.setFocusPolicy(Qt.NoFocus)
        self._clear_btn.clicked.connect(self.clear_all)
        tools_layout.addWidget(self._clear_btn)
        panel.add_section("Media Tools", tools, pref_key="palette_section_tools")

        for widget in (self._media_combo, self._film_combo):
            widget.installEventFilter(self)

        self._inspector = Inspector()
        self._inspector.add_tab(panel, "Palette")
        return self._inspector

    # ------------------------------------------------------------------ data
    def _on_media_type_changed(self, media_type: str) -> None:
        if self._updating or media_type == self._media_type:
            return
        self._media_type = media_type
        self._load_films()

    def _load_films(self) -> None:
        from data.metadata import get_metadata

        try:
            self._films = get_metadata(self._project_path, media_type=self._media_type)
        except Exception:
            self._films = []
        self._updating = True
        self._film_combo.clear()
        for entry in self._films:
            title = entry.get("title") or Path(entry.get("filename", "?")).stem
            year = entry.get("year", "")
            self._film_combo.addItem(f"{title} ({year})" if year else title,
                                     userData=entry.get("filename", ""))
        self._updating = False
        if self._films:
            self._film_combo.setCurrentIndex(0)
            self._on_film_changed(0)
            return
        self._filename = ""
        self._frames = []
        self._record = {}
        self._browser.set_frames([])
        self._refresh_inspector()

    def _on_film_changed(self, index: int) -> None:
        if self._updating or index < 0:
            return
        self._filename = self._film_combo.itemData(index) or ""
        self._current = 0
        self.reload()

    def reload(self) -> None:
        from services import palette_review

        if not self._filename:
            return
        self._record = store.load_review(
            self._project_path, self._filename, self._media_type)
        frames = palette_review.list_frames(
            self._project_path, self._filename, self._media_type)
        for item in frames:
            entry = (self._record.get("frames") or {}).get(item["shot_id"]) or {}
            item["state"] = store.frame_state(entry)
            item["choices"] = store.selectable_choices(entry)
            item["final"] = entry.get("final_palette") or {}
            item["preview"] = preview_quadrants(entry)
        self._frames = frames
        self._browser.set_frames(frames)
        self._current = min(self._current, max(0, len(frames) - 1))
        self._select_frame(self._current)

    def current_frame(self) -> dict | None:
        if 0 <= self._current < len(self._frames):
            return self._frames[self._current]
        return None

    def _entry(self) -> dict:
        item = self.current_frame()
        if not item:
            return {}
        return (self._record.get("frames") or {}).get(item["shot_id"]) or {}

    def _select_frame(self, index: int) -> None:
        if not self._frames:
            self._refresh_inspector()
            return
        self._current = max(0, min(int(index), len(self._frames) - 1))
        self._browser.set_current(self._current)
        item = self.current_frame()
        canvas = self._browser.canvas()
        canvas.set_frame(item["image"] if item and item.get("available") else None)
        canvas.set_palette((item or {}).get("final") or {})
        canvas.set_preview((item or {}).get("preview") or [])
        self._refresh_inspector()

    # ------------------------------------------------------------- inspector
    def _refresh_inspector(self) -> None:
        item = self.current_frame()
        entry = self._entry()
        proposals = entry.get("proposals") or {}
        selectable = set(store.selectable_choices(entry))
        for key, button in self._choice_btns.items():
            proposal = proposals.get(key) or {}
            button.setEnabled(key in selectable)
            label = str(proposal.get("label") or "").split(" ")[0][:9]
            button.setText(f"{key}  {label}" if label else key)

        lines = []
        for key in sorted(proposals, key=lambda value: int(value)):
            proposal = proposals[key] or {}
            converges = proposal.get("converges_with")
            if converges:
                suffix = f"  (same as {converges[0]})"
            elif key not in selectable:
                suffix = "  (unavailable)"
            else:
                suffix = ""
            lines.append(f"{key} {proposal.get('label', '')}{suffix}")
        self._choice_lbl.setText(
            "\n".join(lines) or "No proposals yet — press C to create them.")

        final = entry.get("final_palette") or {}
        for key, value in (
            ("frame", f"{self._current + 1} / {len(self._frames)}"
                      if self._frames else "\u2014"),
            ("shot", (item or {}).get("shot_id", "\u2014")),
            ("state", store.frame_state(entry)),
            ("figure", (final.get(store.ROLE_FIGURE) or {}).get("hex", "\u2014")),
            ("background", (final.get(store.ROLE_BACKGROUND) or {}).get("hex", "\u2014")),
        ):
            self._info.set(key, value)

    # ------------------------------------------------------------ generation
    def create_palette(self) -> None:
        """C — generate proposals for the current frame. Never changes display mode."""
        item = self.current_frame()
        if not item or self._worker is not None:
            return
        self._start_worker(_GenerationWorker(
            self._project_path, self._filename, self._media_type,
            shot_id=item["shot_id"], parent=self))

    def create_all_palettes(self) -> None:
        """Inspector-only batch generation, scoped to the selected media item."""
        if not self._filename or self._worker is not None:
            return
        self._start_worker(_GenerationWorker(
            self._project_path, self._filename, self._media_type, parent=self))

    def _start_worker(self, worker: _GenerationWorker) -> None:
        self._worker = worker
        worker.progress.connect(self._on_progress)
        worker.done.connect(self._on_generation_done)
        worker.failed.connect(self._on_generation_failed)
        self._create_btn.setEnabled(False)
        self._create_all_btn.setEnabled(False)
        self._sweep.start()
        self._sweep_timer.start()
        worker.start()

    def _on_progress(self, done: int, total: int, _shot_id: str, _status: str) -> None:
        # The sweep animation and the section title carry the rest of the
        # message, so the count alone is enough.
        self._generate_section.set_subtitle(f"{done} / {total}")

    def _set_progress(self, text: str) -> None:
        self._progress_lbl.setText(text)
        self._progress_lbl.setVisible(bool(text))

    def _set_manual_status(self, text: str) -> None:
        self._manual_lbl.setText(text)
        self._manual_lbl.setVisible(bool(text))

    def _stop_worker(self) -> None:
        self._sweep_timer.stop()
        self._sweep.stop()
        self._generate_section.set_subtitle("")
        self._create_btn.setEnabled(True)
        self._create_all_btn.setEnabled(True)
        self._worker = None

    def _on_generation_done(self, summary: dict) -> None:
        self._stop_worker()
        self._set_progress(
            f"generated {summary.get('generated', 0)}  "
            f"skipped {summary.get('skipped', 0)}  "
            f"failed {summary.get('failed', 0)}")
        self.reload()

    def _on_generation_failed(self, message: str) -> None:
        self._stop_worker()
        self._set_progress(f"Generation failed: {message}")

    # ---------------------------------------------------------------- review
    def accept_choice(self, choice: str) -> bool:
        """Accept a proposal and advance. An invalid or greyed choice does nothing."""
        from services import palette_review

        item = self.current_frame()
        if not item or str(choice) not in store.selectable_choices(self._entry()):
            return False
        palette_review.accept(self._project_path, self._filename,
                              self._media_type, item["shot_id"], str(choice))
        self._reload_entry(item["shot_id"])
        self.next_frame()
        return True

    def reject_proposals(self) -> None:
        """Rejection advances like acceptance — both are completed review answers."""
        from services import palette_review

        item = self.current_frame()
        if not item:
            return
        palette_review.reject(self._project_path, self._filename,
                              self._media_type, item["shot_id"])
        self._reload_entry(item["shot_id"])
        self.next_frame()

    def reset_frame(self) -> None:
        """Delete/Backspace — clear the human result, keep the proposals."""
        from services import palette_review

        item = self.current_frame()
        if not item:
            return
        palette_review.reset(self._project_path, self._filename,
                             self._media_type, item["shot_id"])
        self._reload_entry(item["shot_id"])
        self._select_frame(self._current)

    def clear_all(self) -> None:
        from services import palette_review

        if not self._filename:
            return
        answer = QMessageBox.question(
            self, "Clear All",
            f"Clear all palette review and manual results for\n{self._filename}?\n\n"
            "Generated proposals are preserved.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        palette_review.clear_all(self._project_path, self._filename, self._media_type)
        self.reload()

    def _reload_entry(self, shot_id: str) -> None:
        self._record = store.load_review(
            self._project_path, self._filename, self._media_type)
        entry = (self._record.get("frames") or {}).get(shot_id) or {}
        for item in self._frames:
            if item["shot_id"] == shot_id:
                item["state"] = store.frame_state(entry)
                item["choices"] = store.selectable_choices(entry)
                item["final"] = entry.get("final_palette") or {}
                item["preview"] = preview_quadrants(entry)
        self._browser.set_frames(self._frames)
        self._browser.set_current(self._current)

    # ---------------------------------------------------------------- manual
    def _on_sam_requested(self, _x: int, _y: int) -> None:
        item = self.current_frame()
        if not item or not item.get("available") or self._mask_worker is not None:
            return
        self._manual_lbl.setText("Segmenting\u2026")
        self._manual_lbl.setVisible(True)
        worker = _MaskWorker(self._project_path, item["image"], "object", self)
        worker.masks_ready.connect(self._on_masks_ready)
        worker.failed.connect(self._on_masks_failed)
        self._mask_worker = worker
        worker.start()

    def _on_masks_ready(self, masks: list) -> None:
        self._mask_worker = None
        self._browser.canvas().set_blobs(build_blobs(masks))
        self._set_manual_status(
            f"{len(self._browser.canvas().blobs())} region(s) found")

    def _on_masks_failed(self, message: str) -> None:
        self._mask_worker = None
        self._set_manual_status(f"Segmentation failed: {message}")

    def _on_pipette(self, red: int, green: int, blue: int, role: str) -> None:
        self._assign(role, [red, green, blue], pipette=True)

    def assign_role(self, role: str) -> None:
        """F / B — assign the currently selected region(s) to a role.

        The mean of the selected regions is used here. A pipetted colour is
        stored verbatim instead and is never replaced by a mask mean.
        """
        canvas = self._browser.canvas()
        indices = canvas.selected_indices()
        item = self.current_frame()
        if not item or not indices:
            self._set_manual_status(
                "Select a region, or Alt/Ctrl+Click to pipette")
            return
        from PIL import Image

        with Image.open(item["image"]) as opened:
            array = np.asarray(opened.convert("RGB"))
        blobs = canvas.blobs()
        union = np.zeros(array.shape[:2], dtype=bool)
        for index in indices:
            mask = np.asarray(blobs[index]["mask"], dtype=bool)
            if mask.shape == union.shape:
                union |= mask
        if not union.any():
            return
        rgb = np.rint(array[union].astype(float).mean(axis=0)).astype(int).tolist()
        self._assign(role, rgb, pipette=False, masks=[
            {"index": index, "area": int(blobs[index].get("area", 0))}
            for index in indices
        ])

    def _assign(self, role: str, rgb: list, *, pipette: bool,
                masks: list | None = None) -> None:
        from services import palette_review

        item = self.current_frame()
        if not item:
            return
        values = [int(value) for value in rgb]
        palette_review.set_manual(
            self._project_path, self._filename, self._media_type, item["shot_id"],
            role, {"rgb": values, "hex": _hex(values), "pipette": pipette},
            masks=masks,
        )
        self._reload_entry(item["shot_id"])
        self._browser.canvas().set_palette(
            (self._entry().get("final_palette")) or {})
        self._refresh_inspector()
        # Manual editing never auto-advances.

    # ------------------------------------------------------------ navigation
    def next_frame(self) -> None:
        if self._frames:
            self._select_frame(min(self._current + 1, len(self._frames) - 1))

    def previous_frame(self) -> None:
        if self._frames:
            self._select_frame(max(self._current - 1, 0))

    def set_mode(self, mode: str) -> None:
        """Single entry point for both the S/A keys and the View buttons."""
        self._browser.set_mode(mode)
        current = self._browser.mode()
        for name, button in self._mode_btns.items():
            button.setChecked(name == current)

    def mode(self) -> str:
        return self._browser.mode()

    def _toggle_display(self, name: str) -> None:
        """Single entry point for both the P/I keys and the View buttons."""
        if name == "palette":
            self._show_palette = not self._show_palette
        else:
            self._show_image = not self._show_image
        self._browser.set_visibility(self._show_palette, self._show_image)
        self._toggle_btns["palette"].setChecked(self._show_palette)
        self._toggle_btns["image"].setChecked(self._show_image)

    def toggle_palette(self) -> None:
        self._toggle_display("palette")

    def toggle_image(self) -> None:
        self._toggle_display("image")

    # -------------------------------------------------------------- keyboard
    @staticmethod
    def _editing_text() -> bool:
        from PyQt5.QtWidgets import QAbstractSpinBox, QLineEdit, QTextEdit

        widget = QApplication.focusWidget()
        return isinstance(widget, (QLineEdit, QTextEdit, QAbstractSpinBox))

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if self._editing_text():
            super().keyPressEvent(event)
            return
        if self._browser.zoom_manager().handle_key_event(event, _ZOOM_DEFAULT):
            return
        if event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier | Qt.AltModifier):
            super().keyPressEvent(event)
            return
        key = event.key()
        if key == Qt.Key_Right:
            self.next_frame()
            return
        if key == Qt.Key_Left:
            self.previous_frame()
            return
        if key in (Qt.Key_Delete, Qt.Key_Backspace):
            self.reset_frame()
            return
        letter = event.text().upper()
        handlers = {
            "C": self.create_palette,
            "P": self.toggle_palette,
            "I": self.toggle_image,
            "S": lambda: self.set_mode(MODE_SINGLE),
            "A": lambda: self.set_mode(MODE_ALL),
            "F": lambda: self.assign_role(store.ROLE_FIGURE),
            "B": lambda: self.assign_role(store.ROLE_BACKGROUND),
        }
        if letter in CHOICE_KEYS:
            self.accept_choice(letter)
            return
        if letter in handlers:
            handlers[letter]()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        for worker in (self._worker, self._mask_worker):
            if worker is not None and worker.isRunning():
                if hasattr(worker, "cancel"):
                    worker.cancel()
                worker.wait(3000)
        super().closeEvent(event)


def build_blobs(masks: list) -> list[dict]:
    """Convert raw segmentation masks to selectable overlay blobs.

    Every returned mask becomes its own selectable region — they are never
    unioned automatically. The human decides which ones matter.
    """
    import cv2

    blobs = []
    for mask in masks or []:
        segmentation = np.asarray(mask.get("segmentation"), dtype=bool)
        if segmentation.ndim != 2 or not segmentation.any():
            continue
        contours, _ = cv2.findContours(segmentation.astype(np.uint8) * 255,
                                       cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue
        main = max(contours, key=cv2.contourArea)
        perimeter = cv2.arcLength(main, True)
        if perimeter <= 0:
            continue
        approx = cv2.approxPolyDP(main, 0.003 * perimeter, True)
        if len(approx) < 3:
            continue
        blobs.append({
            "polygon": approx.squeeze(axis=1).tolist(),
            "area": int(segmentation.sum()),
            "mask": segmentation,
        })
    return blobs


def run_visualizer(project_path: str, media_type: str = "movie") -> None:
    """Create QApplication (if needed) and open the palette visualizer."""
    from visualizers.launcher import run_visualizer_window

    run_visualizer_window(
        "palette",
        lambda: PaletteVisualizerWindow(project_path, media_type=media_type),
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--media", default="movie")
    parsed = parser.parse_args()
    run_visualizer(parsed.project, media_type=parsed.media)
