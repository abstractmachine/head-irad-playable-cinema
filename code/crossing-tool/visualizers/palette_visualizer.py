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
from visualizers.components.shortcut_button import ShortcutButton
from visualizers.components.tab_panel import TabPanel
from visualizers.components.timeline_scrubber import TimelineHitArea, TimelineScrollBar
from visualizers.components.zoom_manager import ZoomManager

_DEFAULT_ASPECT = 16 / 9
# Canonical browser spacing, as used by Metadata's thumbnail grid.
_GAP = _MARGIN = theme.SECTION_GAP
_ZOOM_MIN, _ZOOM_MAX, _ZOOM_STEP, _ZOOM_DEFAULT = 0.60, 3.00, 0.20, 1.00
# Scrub grab target, in SCROLLBAR_W units. It now floats over the frames, so
# keep the band tight: every pixel of it is a pixel of grid you cannot click.
SCRUB_BARS = 2
# Canonical selection border, matching ThumbnailCell's 2px accent ring.
SELECTION_BORDER = 2
# All Frames: the background colour is a ring over the image, or the whole cell
# when the image is hidden. The figure disc keeps one size either way.
CELL_BORDER_RATIO = 0.10
CELL_FIGURE_RATIO = 3 / 5
# Single Frame palette overlay: a border of the background colour around the
# frame edge, and a centred disc of the figure colour, both as a fraction of
# the frame's shorter side.
SINGLE_BORDER_RATIO = 0.10
SINGLE_FIGURE_RATIO = 0.20

MODE_ALL = "all"
MODE_SINGLE = "single"
CHOICE_KEYS = ("1", "2", "3", "4")

# Interaction help lives in tooltips, never as Inspector prose — see the
# Inspector prose rule in agents.md.
MANUAL_HELP = (
    "Figure or Background arms that role\n"
    "While armed, click the frame to take the colour under the cursor,\n"
    "or press 1-4 to take that role from a proposal\n"
    "Press the button again to cancel\n"
    "Alt+Click picks Figure   ·   Ctrl+Click picks Background\n"
    "Clicking the frame unarmed segments it, for orientation only"
)

CHOICE_HELP = (
    "1  DIRECT — the materials carrier-v1 named for each side\n"
    "2  FIELD — articulation-v1's distinguished material and counterfield\n"
    "3  ALTERNATIVE — a second relation narratology-v1 recorded,\n"
    "      or a refusal when it recorded only one\n"
    "4  CONTROL — the deterministic bottom-up production palette\n"
    "\n"
    "Pressing a number takes the whole proposal. Arm Figure or Background\n"
    "first to take only that half — F then 1, B then 4 — recorded as a split\n"
    "validation. A choice without two measured colours is greyed out and does\n"
    "nothing. Nothing here advances; use the arrow keys."
)

def frame_aspect(frames: list, default: float = _DEFAULT_ASPECT) -> float:
    """The width:height ratio of this media's frames.

    Read from the first available frame's header rather than assuming 16:9, so
    the grid cells match the film and the image fills them exactly.
    """
    from PyQt5.QtGui import QImageReader

    for item in frames or []:
        if not item.get("available"):
            continue
        size = QImageReader(item["image"]).size()
        if size.isValid() and size.height() > 0:
            return size.width() / size.height()
    return default


def _zoom_key(media_type: str) -> str:
    return f"palette_browser_zoom_{media_type}"


def _qcolor(colour: dict | None, fallback=(40, 40, 46)) -> QColor:
    rgb = (colour or {}).get("rgb") or fallback
    return QColor(int(rgb[0]), int(rgb[1]), int(rgb[2]))


def _hex(rgb) -> str:
    values = [int(value) for value in rgb]
    return "#{:02x}{:02x}{:02x}".format(*values)


def _role_text(colour: dict | None, choice=None) -> str:
    """One role's hex and where it came from — the point of a split validation."""
    colour = colour or {}
    hex_value = colour.get("hex")
    if not hex_value:
        return "\u2014"
    choice = colour.get("choice") or choice
    if choice:
        return f"{hex_value}  ·  {choice}"
    if colour.get("pipette"):
        return f"{hex_value}  ·  pipette"
    return hex_value


def _readable_on(colour: dict | None) -> QColor:
    """Pick a legible numeral colour for an arbitrary measured background."""
    rgb = (colour or {}).get("rgb") or [0, 0, 0]
    luminance = (rgb[0] * 299 + rgb[1] * 587 + rgb[2] * 114) / 1000
    return QColor(theme.ACCENT_TEXT) if luminance > 140 else QColor(theme.TEXT)


def display_palette(entry: dict | None) -> dict:
    """The palette to draw for a frame.

    The resolved palette when there is one, otherwise whichever roles have
    been composed so far — a half-finished palette should still show its half,
    whether that half was pipetted or taken from a proposal.
    """
    entry = entry or {}
    return (entry.get("final_palette") or {}) or store.resolved_roles(entry)


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


def _paint_border(painter, rect, colour, ratio: float) -> None:
    """An inset ring of *colour*, *ratio* of the shorter side thick."""
    thickness = max(1, int(min(rect.width(), rect.height()) * ratio))
    pen = QPen(_qcolor(colour), thickness)
    pen.setJoinStyle(Qt.MiterJoin)
    painter.setBrush(Qt.NoBrush)
    painter.setPen(pen)
    half = thickness // 2
    painter.drawRect(rect.adjusted(half, half, -half - 1, -half - 1))


def _paint_frame_palette(painter, rect, palette: dict) -> None:
    """Single Frame treatment: background border around the edge, figure disc."""
    background = palette.get(store.ROLE_BACKGROUND)
    figure = palette.get(store.ROLE_FIGURE)
    if background:
        _paint_border(painter, rect, background, SINGLE_BORDER_RATIO)
    if figure:
        diameter = max(6, int(min(rect.width(), rect.height())
                              * SINGLE_FIGURE_RATIO))
        painter.setBrush(_qcolor(figure))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(rect.center().x() - diameter // 2,
                            rect.center().y() - diameter // 2, diameter, diameter)


def needs_generation(item: dict | None) -> bool:
    """True when a frame still has nothing — no proposals and no manual palette."""
    return (item or {}).get("state") in (store.STATE_NOT_GENERATED,
                                        store.STATE_GENERATION_FAILED)


def reset_action(entry: dict | None) -> tuple[str, bool]:
    """Label and enabled state for the staged reset control.

    Delete walks a frame backwards: a chosen palette returns to the proposals,
    the proposals return to ungenerated, and an ungenerated frame has nothing
    left to undo.
    """
    state = store.frame_state(entry)
    if state in (store.STATE_ACCEPTED, store.STATE_REJECTED,
                 store.STATE_MANUAL, store.STATE_MANUAL_INCOMPLETE,
                 store.STATE_SPLIT, store.STATE_SPLIT_INCOMPLETE):
        return "Reset Palette", True
    if state in (store.STATE_UNREVIEWED, store.STATE_GENERATION_FAILED):
        return "Reset Proposals", True
    return "Reset", False


def _action(label: str, shortcut: str | None, tooltip: str = "", *,
            checkable: bool = False) -> ShortcutButton:
    """An Inspector action button with its key hint flush right."""
    button = ShortcutButton(label, shortcut)
    button.setStyleSheet(theme.action_button_stylesheet())
    button.setFocusPolicy(Qt.NoFocus)
    button.setCheckable(checkable)
    if tooltip:
        button.setToolTip(tooltip)
    return button


def _row(*widgets) -> QWidget:
    """Lay widgets out side by side on one Inspector row."""
    wrap = QWidget()
    layout = QHBoxLayout(wrap)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(theme.SECTION_GAP)
    for widget in widgets:
        layout.addWidget(widget)
    return wrap


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
    for index, (quadrant, cell) in enumerate(zip(quadrants, cells)):
        key = quadrant["key"] if quadrant else str(index + 1)
        if quadrant is None:
            painter.fillRect(cell, QColor(theme.CELL_BG))
            pen = QColor(theme.TEXT_DIM)
        else:
            _paint_pair(painter, cell, quadrant["figure"], quadrant["background"])
            pen = _readable_on(quadrant["background"])
        # The slot number is what the reviewer presses, so label empty slots too.
        if numbers and cell.height() >= 34:
            painter.setPen(pen)
            painter.setFont(theme.font_ui(bold=True))
            painter.drawText(cell.adjusted(6, 4, -6, -4),
                             Qt.AlignTop | Qt.AlignLeft, key)
    painter.setBrush(Qt.NoBrush)


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
    double_clicked = pyqtSignal(int)

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

    def set_item(self, item: dict) -> None:
        """Refresh this cell's record in place, without rebuilding the grid."""
        self._item = item
        self.setToolTip(f"{item.get('shot_id', '')}\n{item.get('state', '')}")
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._index)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.double_clicked.emit(self._index)
        super().mouseDoubleClickEvent(event)

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
        rect = self.rect()
        final = self._item.get("final") or {}
        background = final.get(store.ROLE_BACKGROUND)
        figure = final.get(store.ROLE_FIGURE)
        quadrants = self._item.get("preview") or []
        previewing = self._show_palette and not final and any(quadrants)
        pixmap = self._load_pixmap() if self._show_image else None

        if pixmap is not None:
            # The grid truncates cell width and height independently, so a cell
            # is never exactly the media's ratio and KeepAspectRatio would leave
            # a pixel of backing showing. Expanding fills the cell outright; the
            # sub-pixel overflow is clipped to the widget.
            scaled = pixmap.scaled(self.size(), Qt.KeepAspectRatioByExpanding,
                                   Qt.SmoothTransformation)
            painter.fillRect(rect, QColor(theme.CANVAS_BG))
            painter.drawPixmap((self.width() - scaled.width()) // 2,
                               (self.height() - scaled.height()) // 2, scaled)
        elif self._show_palette and background:
            painter.fillRect(rect, _qcolor(background))
        elif previewing:
            paint_preview(painter, rect, quadrants)
        else:
            painter.fillRect(rect, QColor(theme.CELL_BG))

        if pixmap is not None and self._show_palette:
            if background:
                _paint_border(painter, rect, background, CELL_BORDER_RATIO)
            elif previewing:
                # An opaque strip rather than a wash over the frame.
                bar = max(6, int(self.height() * 0.22))
                paint_preview(painter, QRect(0, self.height() - bar,
                                             self.width(), bar),
                              quadrants, columns=4)

        if self._show_palette and figure:
            diameter = max(6, int(self.height() * CELL_FIGURE_RATIO))
            painter.setBrush(_qcolor(figure))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(rect.center().x() - diameter // 2,
                                rect.center().y() - diameter // 2,
                                diameter, diameter)

        painter.setBrush(Qt.NoBrush)
        # Only the current selection carries a border; an unselected cell shows
        # its state through its content, matching ThumbnailCell's convention.
        if self._selected:
            painter.setPen(QPen(QColor(theme.ACCENT), SELECTION_BORDER))
            inset = SELECTION_BORDER // 2
            painter.drawRect(self.rect().adjusted(
                inset, inset, -inset - 1, -inset - 1))
        painter.end()


class _PreviewCell(_FrameCell):
    """The current frame drawn by the browser's own painter, for the Inspector.

    Subclassed rather than reimplemented so the Palette and Image sections can
    never drift from what the thumbnails show. Inert: these report state, they
    are not a second way to select a frame.
    """

    def __init__(self, palette: bool, image: bool, parent=None) -> None:
        super().__init__(-1, {}, parent)
        self.set_visibility(palette, image)
        self._aspect = _DEFAULT_ASPECT
        self.setCursor(Qt.ArrowCursor)

    def set_aspect(self, aspect: float) -> None:
        aspect = float(aspect)
        if aspect > 0 and abs(aspect - self._aspect) > 1e-6:
            self._aspect = aspect
            self._fit()

    def set_item(self, item: dict) -> None:
        # A preview follows the selection, so the cached frame cannot survive it.
        self._pixmap = None
        super().set_item(item)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        event.ignore()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        event.ignore()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # A collapsed section is hidden, and Qt sends no resizeEvent to a hidden
        # widget, so without this the first expand lays out at a stale height.
        self._fit()

    def _fit(self) -> None:
        height = max(1, int(self.width() / self._aspect))
        if self.height() != height:
            self.setFixedHeight(height)


# ---------------------------------------------------------------------------
# Whole-frame canvas with mask selection and pipette
# ---------------------------------------------------------------------------

class _FrameCanvas(QLabel):
    """One frame, its selectable mask overlays, and the manual pipette."""

    pipette = pyqtSignal(int, int, int, str)
    sam_requested = pyqtSignal(int, int)
    double_clicked = pyqtSignal()

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
        self._pick_role: str | None = None
        self._pick_preview: dict | None = None

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

    def set_pick_role(self, role: str | None) -> None:
        """Arm the next click to take the pixel colour for *role*."""
        self._pick_role = role
        self._pick_preview = None
        self.setCursor(Qt.CrossCursor if role else Qt.ArrowCursor)
        self.update()

    def pick_role(self) -> str | None:
        return self._pick_role

    def pick_preview(self) -> dict | None:
        return self._pick_preview

    def effective_palette(self) -> dict:
        """The palette as drawn: the stored one, with any live pick on top."""
        palette = dict(self._palette)
        if self._pick_role and self._pick_preview:
            palette[self._pick_role] = self._pick_preview
        return palette

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
        role = None
        if modifiers & Qt.AltModifier:
            role = store.ROLE_FIGURE
        elif modifiers & Qt.ControlModifier:
            role = store.ROLE_BACKGROUND
        elif self._pick_role:
            role = self._pick_role
        if role is not None:
            # The pixel under the cursor is the colour, so no segmentation.
            if position is not None and self._image is not None:
                colour = self._image.pixelColor(*position)
                self.pipette.emit(colour.red(), colour.green(), colour.blue(), role)
            return
        index = self._blob_at(event.x(), event.y())
        if index >= 0:
            self.toggle_selection(index, bool(modifiers & Qt.ShiftModifier))
        elif position is not None:
            self.sam_requested.emit(*position)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self._pick_role is None:
            self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        # While armed, the role shows the colour under the cursor as you move.
        if self._pick_role:
            position = self._source_pos(event.x(), event.y())
            colour = None
            if position is not None and self._image is not None:
                pixel = self._image.pixelColor(*position)
                rgb = [pixel.red(), pixel.green(), pixel.blue()]
                colour = {"rgb": rgb, "hex": _hex(rgb)}
            if colour != self._pick_preview:
                self._pick_preview = colour
                self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._pick_preview is not None:
            self._pick_preview = None
            self.update()
        super().leaveEvent(event)

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
        palette = self.effective_palette()
        previewing = (self._show_palette and not palette
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

        if self._show_palette and palette:
            _paint_frame_palette(painter, frame, palette)
        elif previewing and self._show_image:
            # With the image visible the proposals go in a strip: just under
            # the frame when it fits there whole, otherwise inside its bottom.
            bar = max(34, int(height * 0.14))
            strip = QRect(ox, oy + height, width, bar)
            if strip.bottom() > self.rect().bottom():
                strip = QRect(ox, oy + height - bar, width, bar)
            paint_preview(painter, strip, self._preview, numbers=True, columns=4)
        painter.end()


# ---------------------------------------------------------------------------
# Browser page
# ---------------------------------------------------------------------------

class _PaletteBrowserPage(QWidget):
    """All Frames / Single Frame stack plus the shared Shotlist scrubber."""

    frame_selected = pyqtSignal(int)
    frame_activated = pyqtSignal(int)

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
        self._grid = AspectGridWidget(aspect=_DEFAULT_ASPECT, gap=_GAP,
                                      margin=_MARGIN)
        self._grid.set_zoom(self._zoom_manager.zoom())
        self._scroll.setWidget(self._grid)

        self._canvas = _FrameCanvas()

        self._stack = QStackedWidget()
        self._stack.addWidget(self._scroll)
        self._stack.addWidget(self._canvas)
        outer.addWidget(self._stack, 1)

        self._scrub_bar = TimelineScrollBar()
        # Parented to the page rather than added to the layout: the scrubber
        # floats over the bottom edge so the frames run to the window edge
        # instead of a band of empty canvas sitting under them.
        self._scrub = TimelineHitArea(self._scrub_bar, self, bars=SCRUB_BARS)
        self._scrub.setAttribute(Qt.WA_StyledBackground, True)
        self._scrub.setStyleSheet("background: transparent;")
        self._scrub_bar.valueChanged.connect(
            lambda value: self.frame_selected.emit(int(value)))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        height = self._scrub.height()
        self._scrub.setGeometry(0, self.height() - height, self.width(), height)
        self._scrub.raise_()

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
        self._grid.set_aspect(frame_aspect(frames))
        cells = []
        for index, item in enumerate(frames):
            cell = _FrameCell(index, item)
            cell.set_visibility(self._show_palette, self._show_image)
            cell.clicked.connect(self.frame_selected.emit)
            cell.double_clicked.connect(self.frame_activated.emit)
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
        if 0 <= index < len(self._cells):
            self._scroll.ensureWidgetVisible(self._cells[index])
        self._scrub_bar.blockSignals(True)
        self._scrub_bar.setValue(index)
        self._scrub_bar.blockSignals(False)

    def update_frame(self, index: int, item: dict) -> None:
        """Repaint one cell without rebuilding the whole grid."""
        if 0 <= index < len(self._cells):
            self._cells[index].set_item(item)

    def columns(self) -> int:
        """Grid columns, so the window can step a whole row with Up/Down."""
        return self._grid.columns()

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
        self._active_btn: Optional[QPushButton] = None
        self._mask_worker: Optional[_MaskWorker] = None
        self._pick_role: str | None = None
        self._previews: dict[str, _PreviewCell] = {}

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
        self._browser.frame_activated.connect(self._activate_frame)
        canvas = self._browser.canvas()
        canvas.pipette.connect(self._on_pipette)
        canvas.sam_requested.connect(self._on_sam_requested)
        canvas.double_clicked.connect(lambda: self.set_mode(MODE_ALL))
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
        for label, key, mode, tip in (
            ("Single Frame", "S", MODE_SINGLE,
             "Show the selected frame on its own for review and manual editing"),
            ("All Frames", "A", MODE_ALL,
             "Show every frame of the selected movie/gameplay as a grid"),
        ):
            button = _action(label, key, tip, checkable=True)
            button.clicked.connect(lambda _checked, m=mode: self.set_mode(m))
            self._mode_btns[mode] = button
        self._toggle_btns: dict[str, QPushButton] = {}
        for label, key, name, tip in (
            ("Palette", "P", "palette", "Show the measured palette, or the 1-4 "
                                        "proposal preview when none is chosen yet"),
            ("Image", "I", "image", "Show the source frame"),
        ):
            button = _action(label, key, tip, checkable=True)
            button.setChecked(True)
            button.clicked.connect(
                lambda _checked, n=name: self._toggle_display(n))
            self._toggle_btns[name] = button
        view_layout.addWidget(_row(self._mode_btns[MODE_SINGLE],
                                   self._mode_btns[MODE_ALL]))
        view_layout.addWidget(_row(self._toggle_btns["palette"],
                                   self._toggle_btns["image"]))
        panel.add_section("View", view, pref_key="palette_section_view")

        # Both previews read the current frame the way a thumbnail does, so a
        # reviewer can see either rendering without toggling the whole browser.
        for title, key, palette_on, image_on in (
            ("Palette", "palette", True, False),
            ("Image", "image", False, True),
        ):
            preview = _PreviewCell(palette_on, image_on)
            self._previews[key] = preview
            panel.add_section(title, preview, expanded=False,
                              pref_key=f"palette_section_preview_{key}")

        generate = QWidget()
        generate_layout = QVBoxLayout(generate)
        generate_layout.setContentsMargins(0, 0, 0, 0)
        generate_layout.setSpacing(theme.SECTION_GAP)
        self._create_btn = _action(
            "Generate Proposals", "G",
            "Generate palette proposals for the current frame")
        self._create_btn.clicked.connect(self.create_palette)
        # Deliberately no keyboard shortcut: batch generation is an explicit
        # button press (or CLI command), never a rapid-review keystroke.
        self._create_all_btn = _action(
            "Generate All Remaining", None,
            "Generate proposals for every frame of the selected movie/gameplay "
            "that does not have them yet (no keyboard shortcut)")
        self._create_all_btn.clicked.connect(self.create_all_palettes)
        self._generate_labels = {
            self._create_btn: self._create_btn.text(),
            self._create_all_btn: self._create_all_btn.text(),
        }
        generate_layout.addWidget(_row(self._create_btn, self._create_all_btn))

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
            button.setToolTip(CHOICE_HELP)
            button.clicked.connect(lambda _checked, k=key: self.accept_choice(k))
            self._choice_btns[key] = button
            choices_row.addWidget(button)
        choices_wrap.setToolTip(CHOICE_HELP)
        choices_wrap.setStyleSheet(theme.tooltip_stylesheet())
        review_layout.addWidget(choices_wrap)
        self._reset_btn = _action(
            "Reset Palette", "Del",
            "Clear this frame's accepted or manual palette, keeping its proposals")
        self._reset_btn.clicked.connect(self.reset_frame)
        review_layout.addWidget(self._reset_btn)
        review_section = panel.add_section("Review", review,
                                           pref_key="palette_section_review")
        review_section.setToolTip(CHOICE_HELP)
        review_section.setStyleSheet(theme.tooltip_stylesheet())

        manual = QWidget()
        manual_layout = QVBoxLayout(manual)
        manual_layout.setContentsMargins(0, 0, 0, 0)
        manual_layout.setSpacing(theme.SECTION_GAP)
        manual.setToolTip(MANUAL_HELP)
        # Not an action button, so it has to carry the canonical tooltip rule
        # itself or the Inspector's bare container styles render it black.
        manual.setStyleSheet(theme.tooltip_stylesheet())
        self._role_btns = {}
        for label, key, role in (("Figure", "F", store.ROLE_FIGURE),
                                 ("Background", "B", store.ROLE_BACKGROUND)):
            # Checkable so the button stays lit while it waits for the click.
            button = _action(label, key, MANUAL_HELP, checkable=True)
            button.clicked.connect(lambda _checked, r=role: self.assign_role(r))
            self._role_btns[role] = button
        manual_layout.addWidget(_row(self._role_btns[store.ROLE_FIGURE],
                                     self._role_btns[store.ROLE_BACKGROUND]))
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
        self._manual_sweep = SweepBar(self)
        self._manual_sweep_timer = QTimer(self)
        self._manual_sweep_timer.setInterval(20)
        self._manual_sweep_timer.timeout.connect(self._manual_sweep.tick)
        manual_section.set_subbar(self._manual_sweep)

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
        self._clear_btn = _action(
            "Clear All", None,
            "Clear every accepted, rejected and manual palette for the selected "
            "movie/gameplay, after a confirmation.\nGenerated proposals are kept, "
            "so every frame returns to its unreviewed state.")
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
            item["display"] = display_palette(entry)
            item["preview"] = preview_quadrants(entry)
        self._frames = frames
        self._browser.set_frames(frames)
        for preview in self._previews.values():
            preview.set_aspect(frame_aspect(frames))
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
        canvas.set_palette((item or {}).get("display") or {})
        canvas.set_preview((item or {}).get("preview") or [])
        self._refresh_inspector()

    def _activate_frame(self, index: int) -> None:
        """Double-click in All Frames: select that frame and open it."""
        self._select_frame(index)
        self.set_mode(MODE_SINGLE)

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
            converges = proposal.get("converges_with")
            if converges:
                detail = f"{key} converges with choice {converges[0]}"
            elif proposal and key not in selectable:
                detail = f"{key} {proposal.get('label', '')} — no measured palette"
            elif proposal:
                detail = f"{key} {proposal.get('label', '')}"
            else:
                detail = "No proposals yet — press G to create them"
            button.setToolTip(f"{detail}\n\n{CHOICE_HELP}")

        final = display_palette(entry)
        for preview in self._previews.values():
            preview.set_item(item or {})
        label, enabled = reset_action(entry)
        self._reset_btn.setText(label)
        self._reset_btn.setEnabled(enabled)
        if self._worker is None:
            # Nothing to generate once every frame has proposals or a manual
            # palette; the service would skip them anyway.
            self._create_btn.setEnabled(needs_generation(item))
            self._create_all_btn.setEnabled(
                any(needs_generation(frame) for frame in self._frames))
        for key, value in (
            ("frame", f"{self._current + 1} / {len(self._frames)}"
                      if self._frames else "\u2014"),
            ("shot", (item or {}).get("shot_id", "\u2014")),
            ("state", store.frame_state(entry)),
            ("figure", _role_text(final.get(store.ROLE_FIGURE), final.get("choice"))),
            ("background", _role_text(final.get(store.ROLE_BACKGROUND), final.get("choice"))),
        ):
            self._info.set(key, value)

    # ------------------------------------------------------------ generation
    def create_palette(self) -> None:
        """G — generate proposals for the current frame, or cancel this run."""
        if self._worker is not None:
            if self._active_btn is self._create_btn:
                self.cancel_generation()
            return
        item = self.current_frame()
        if not item or not self._create_btn.isEnabled():
            return
        self._start_worker(_GenerationWorker(
            self._project_path, self._filename, self._media_type,
            shot_id=item["shot_id"], parent=self), self._create_btn)

    def create_all_palettes(self) -> None:
        """Inspector-only batch generation, or cancel this run."""
        if self._worker is not None:
            if self._active_btn is self._create_all_btn:
                self.cancel_generation()
            return
        if not self._filename or not self._create_all_btn.isEnabled():
            return
        self._start_worker(_GenerationWorker(
            self._project_path, self._filename, self._media_type, parent=self),
            self._create_all_btn)

    def cancel_generation(self) -> None:
        """Stop after the frame in flight; everything finished is kept."""
        if self._worker is not None:
            self._worker.cancel()
            self._active_btn.setText("Cancelling")
            self._active_btn.setEnabled(False)

    def _start_worker(self, worker: _GenerationWorker,
                      source: QPushButton) -> None:
        self._worker = worker
        self._active_btn = source
        other = (self._create_all_btn if source is self._create_btn
                 else self._create_btn)
        source.setText("Cancel Proposals")
        source.setEnabled(True)
        other.setEnabled(False)
        worker.progress.connect(self._on_progress)
        worker.done.connect(self._on_generation_done)
        worker.failed.connect(self._on_generation_failed)
        self._sweep.start()
        self._sweep_timer.start()
        worker.start()

    def _on_progress(self, done: int, total: int, shot_id: str, status: str) -> None:
        # The sweep animation and the section title carry the rest of the
        # message, so the count alone is enough.
        self._generate_section.set_subtitle(f"{done} / {total}")
        # Refresh the frame that just landed so it is reviewable immediately,
        # without rebuilding the grid under the reviewer.
        if status == "generated" and shot_id:
            self._refresh_frame(shot_id)

    def _set_manual_status(self, text: str) -> None:
        self._manual_lbl.setText(text)
        self._manual_lbl.setVisible(bool(text))

    def _stop_worker(self) -> None:
        self._sweep_timer.stop()
        self._sweep.stop()
        self._generate_section.set_subtitle("")
        for button, label in self._generate_labels.items():
            button.setText(label)
        self._worker = None
        self._active_btn = None
        self._refresh_inspector()

    def _on_generation_done(self, summary: dict) -> None:
        self._stop_worker()
        # Failures go to the console rather than an Inspector row.
        for shot_id, error in summary.get("errors") or []:
            print(f"[palette] generation failed for {shot_id}: {error}",
                  file=sys.stderr, flush=True)
        self.reload()

    def _on_generation_failed(self, message: str) -> None:
        self._stop_worker()
        print(f"[palette] generation failed: {message}", file=sys.stderr, flush=True)

    # ---------------------------------------------------------------- review
    def accept_choice(self, choice: str) -> bool:
        """Accept a proposal, or just one of its halves when a role is armed.

        Nothing here advances: try 1, then 2, pipette a role, then come back to
        a proposal. Right Arrow moves on when you are satisfied.
        """
        from services import palette_review

        item = self.current_frame()
        if not item or str(choice) not in store.selectable_choices(self._entry()):
            return False
        role = self._pick_role
        if role:
            palette_review.set_role_choice(self._project_path, self._filename,
                                           self._media_type, item["shot_id"],
                                           role, str(choice))
            self.set_pick_role(None)
        else:
            palette_review.accept(self._project_path, self._filename,
                                  self._media_type, item["shot_id"], str(choice))
        self._reload_entry(item["shot_id"])
        return True

    def reject_proposals(self) -> None:
        """Record that no proposal is satisfactory, without leaving the frame."""
        from services import palette_review

        item = self.current_frame()
        if not item:
            return
        palette_review.reject(self._project_path, self._filename,
                              self._media_type, item["shot_id"])
        self._reload_entry(item["shot_id"])

    def reset_frame(self) -> None:
        """Delete/Backspace — step this frame back one stage.

        A chosen palette returns to the proposals; proposals are discarded;
        an ungenerated frame does nothing.
        """
        from services import palette_review

        item = self.current_frame()
        if not item:
            return
        entry = self._entry()
        _label, enabled = reset_action(entry)
        if not enabled:
            return
        if store.frame_state(entry) in (store.STATE_UNREVIEWED,
                                        store.STATE_GENERATION_FAILED):
            palette_review.clear_proposals(self._project_path, self._filename,
                                           self._media_type, item["shot_id"])
        else:
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
        self._refresh_frame(shot_id, reload_record=False)

    def _refresh_frame(self, shot_id: str, *, reload_record: bool = True) -> None:
        """Re-read one frame and repaint just its cell."""
        if reload_record:
            self._record = store.load_review(
                self._project_path, self._filename, self._media_type)
        entry = (self._record.get("frames") or {}).get(shot_id) or {}
        for index, item in enumerate(self._frames):
            if item["shot_id"] != shot_id:
                continue
            item["state"] = store.frame_state(entry)
            item["choices"] = store.selectable_choices(entry)
            item["final"] = entry.get("final_palette") or {}
            item["display"] = display_palette(entry)
            item["preview"] = preview_quadrants(entry)
            self._browser.update_frame(index, item)
            if index == self._current:
                canvas = self._browser.canvas()
                canvas.set_palette(item["display"])
                canvas.set_preview(item["preview"])
            break
        self._refresh_inspector()

    # ---------------------------------------------------------------- manual
    def _on_sam_requested(self, _x: int, _y: int) -> None:
        item = self.current_frame()
        if not item or not item.get("available") or self._mask_worker is not None:
            return
        # The sweep on the section title says "working"; no status text needed.
        self._manual_sweep.start()
        self._manual_sweep_timer.start()
        worker = _MaskWorker(self._project_path, item["image"], "object", self)
        worker.masks_ready.connect(self._on_masks_ready)
        worker.failed.connect(self._on_masks_failed)
        self._mask_worker = worker
        worker.start()

    def _stop_manual_sweep(self) -> None:
        self._manual_sweep_timer.stop()
        self._manual_sweep.stop()

    def _on_masks_ready(self, masks: list) -> None:
        self._mask_worker = None
        self._stop_manual_sweep()
        self._browser.canvas().set_blobs(build_blobs(masks))

    def _on_masks_failed(self, message: str) -> None:
        self._mask_worker = None
        self._stop_manual_sweep()
        # Failures stay visible; successes say nothing.
        self._set_manual_status(f"Segmentation failed: {message}")

    def _on_pipette(self, red: int, green: int, blue: int, role: str) -> None:
        self._assign(role, [red, green, blue], pipette=True)
        self.set_pick_role(None)

    def set_pick_role(self, role: str | None) -> None:
        """Arm (or disarm) the next canvas click to pick a colour for *role*."""
        self._pick_role = role
        self._browser.canvas().set_pick_role(role)
        for name, button in self._role_btns.items():
            button.setChecked(name == role)

    def assign_role(self, role: str) -> None:
        """F / B — arm a role for the next click or number; the same key cancels."""
        self.set_pick_role(None if self._pick_role == role else role)

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
        self._browser.canvas().set_palette(display_palette(self._entry()))
        self._refresh_inspector()
        # Manual editing never auto-advances.

    # ------------------------------------------------------------ navigation
    def next_frame(self) -> None:
        if self._frames:
            self._select_frame(min(self._current + 1, len(self._frames) - 1))

    def previous_frame(self) -> None:
        if self._frames:
            self._select_frame(max(self._current - 1, 0))

    def step_row(self, direction: int) -> None:
        """Move one grid row. Clamped, and a no-op outside All Frames."""
        if not self._frames or self.mode() != MODE_ALL:
            return
        target = self._current + direction * self._browser.columns()
        if 0 <= target < len(self._frames):
            self._select_frame(target)

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
        if key in (Qt.Key_Up, Qt.Key_Down):
            # Row stepping only makes sense over the grid; Single Frame is a
            # left/right (or scrubber) sequence.
            if self.mode() == MODE_ALL:
                self.step_row(1 if key == Qt.Key_Down else -1)
            return
        if key in (Qt.Key_Delete, Qt.Key_Backspace):
            self.reset_frame()
            return
        letter = event.text().upper()
        handlers = {
            "G": self.create_palette,
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
