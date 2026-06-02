#!/usr/bin/env python3
"""Book Visualizer — browse imported books as page spreads.

Launched via:
    crossing visualizer book

Layout:
  LEFT  — open-book spread view (one spread at a time) + fuchsia page bar
  RIGHT — panel: book selector, page info, Import / New buttons

Spread layout (book-style):
  - Page 1 is displayed alone on the RIGHT side (front cover)
  - Pages 2-3, 4-5 … shown as left+right pairs filling available width
  - Last page alone on the LEFT side if total page count is even (back cover)

Navigation:
  Left/Right arrows — previous/next spread
  Click page bar    — jump to spread
  Home/End          — previous/next book
  Escape / Ctrl+Q / Ctrl+W — close
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from styles import theme
from styles.theme import GripSplitter, save_window_geometry, restore_window_geometry

from PyQt5.QtCore import Qt, QEvent, pyqtSignal, QRect, QRectF, QSize, QTimer, QPointF
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QIcon,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)
try:
    from PyQt5.QtSvg import QSvgRenderer
    _HAS_SVG = True
except ImportError:
    _HAS_SVG = False

if "QT_QPA_PLATFORM_PLUGIN_PATH" in os.environ:
    del os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GUTTER       = 16    # px gap between left and right page in a spread
_MARGIN       = 24    # px outer margin around pages
_BAR_H        = 8     # px height of the page position bar
_PANEL_WIDTH  = 240   # px — right panel preferred width

# Tool modes
_TOOL_NONE  = "none"
_TOOL_CUT   = "cut"
_TOOL_ERASE = "erase"

# Cut overlay visual constants
_HANDLE_R   = 4      # half-size of point handle square (px)
_CLOSE_DIST = 12     # px distance from first point to snap-close polygon
_HIT_DIST   = 8      # px distance for point / segment hit-testing

_PANEL_STYLESHEET = (
    f"QWidget {{ background: {theme.PANEL_BG}; }}"
    f" QPushButton {{ background-color: {theme.BTN_BG}; border: none;"
    f" padding: 0 10px; border-radius: 3px;"
    f" min-height: {theme.BTN_H}px; max-height: {theme.BTN_H}px; }}"
    f" QPushButton:hover    {{ background-color: {theme.BTN_HOVER}; }}"
    f" QPushButton:pressed  {{ background-color: {theme.BTN_PRESSED}; }}"
    f" QPushButton:checked  {{ background-color: {theme.ACCENT}; color: {theme.TEXT}; }}"
    f" QPushButton:disabled {{ color: {theme.TEXT_DIM};"
    f" background-color: {theme.BTN_BG}; }}"
)


# ---------------------------------------------------------------------------
# SVG icon helper
# ---------------------------------------------------------------------------

def _svg_icon(name: str, size: int = 18, color: str = "#ffffff") -> QIcon:
    """Load an iconoir SVG, recolour strokes/fills to *color*, return QIcon."""
    icon_dir = Path(__file__).parent.parent / "styles" / "icons" / "iconoir"
    path = icon_dir / f"{name}.svg"
    if not path.exists():
        return QIcon()
    raw = path.read_bytes()
    coloured = raw.replace(b"#000000", color.encode())
    if _HAS_SVG:
        renderer = QSvgRenderer(coloured)
        pix = QPixmap(size, size)
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        renderer.render(painter)
        painter.end()
        return QIcon(pix)
    return QIcon()


# ---------------------------------------------------------------------------
# Layer persistence helpers
# ---------------------------------------------------------------------------

def _layers_path(project_path: str, slug: str) -> Path:
    from data.book import book_dir
    return book_dir(project_path, slug) / "layers.json"


def _load_layers(project_path: str, slug: str) -> list:
    p = _layers_path(project_path, slug)
    if not p.exists():
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_layers(project_path: str, slug: str, layers: list) -> None:
    p = _layers_path(project_path, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Strip transient UI state before persisting
    to_save = []
    for layer in layers:
        entry = {k: v for k, v in layer.items() if k != "selected"}
        to_save.append(entry)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(to_save, f, indent=2)


# ---------------------------------------------------------------------------
# Spread index helpers
# ---------------------------------------------------------------------------

def _spread_count(page_count: int) -> int:
    """Number of spreads for a book with *page_count* pages.

    Spread 0 = front cover (page 0 alone on right)
    Spread k = pages (2k-1, 2k) for k >= 1
    Last spread may be a single back-cover page (alone on left) when
    page_count is even.
    """
    if page_count <= 0:
        return 0
    # cover (1) + pairs for remaining pages (ceil)
    return 1 + (page_count - 1 + 1) // 2


def _pages_for_spread(spread_idx: int, page_count: int):
    """Return (left_page, right_page) 0-based indices for a spread.

    A missing page is represented as *None*.
    Spread 0: left=None, right=0  (front cover on right)
    Spread k (k>=1): left=2k-1, right=2k  (or right=None for back cover)
    """
    if spread_idx == 0:
        return None, 0
    left = 2 * spread_idx - 1
    right = 2 * spread_idx
    # back-cover case: even page_count means last spread is single page on left
    if right >= page_count:
        right = None
    return left, right


# ---------------------------------------------------------------------------
# Reveal-through-cuts helpers
# ---------------------------------------------------------------------------

def _page_behind(page_idx: int) -> int:
    """Return the index of the page physically behind *page_idx* in the book stack.

    Even page index = right/recto page → the page behind is +2 (deeper into the book).
    Odd page index  = left/verso page  → the page behind is -2 (further back in the book).
    """
    return page_idx + 2 if page_idx % 2 == 0 else page_idx - 2


def _mirror_points(points: list) -> list:
    """Return horizontally mirrored copy of normalised polygon points.

    Flips x: x' = 1 - x.  y is unchanged.  Used to create the reverse-side
    ghost cut on the back of the same physical leaf.
    """
    return [[1.0 - x, y] for x, y in points]


def _spread_for_page(page_idx: int) -> int:
    """Return the spread index that contains *page_idx*."""
    if page_idx == 0:
        return 0
    return (page_idx + 1) // 2


# ---------------------------------------------------------------------------
# _SpreadView — single-spread display widget
# ---------------------------------------------------------------------------

class _SpreadView(QWidget):
    """Displays one open-book spread.

    Renders only the currently visible spread directly from the open fitz
    document at the current widget size.  A short debounce timer prevents
    thrashing during window resize.  Results are cached by
    (slug, left_i, right_i, w, h).
    """

    _DEBOUNCE_MS = 100

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._doc   = None            # open fitz.Document
        self._slug  = ""
        self._left_i:  Optional[int] = None
        self._right_i: Optional[int] = None
        self._spread_idx: int = 0
        # cache: (slug, left_i, right_i, w, h) → (QImage|None, QImage|None)
        self._cache: dict = {}
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(self._DEBOUNCE_MS)
        self._debounce.timeout.connect(self._do_render)
        self._overlay: Optional[QWidget] = None  # _CutOverlay attached externally

        # Reveal-through-cuts state
        self._all_layers: list = []   # all layers for current book (every page)
        self._layers_ver: int = 0     # bumped on every set_layers() call
        self._reveal_cache: dict = {} # keyed by (slug, page_idx, cw, ch, ver, depth)
        self._REVEAL_MAX_DEPTH: int = 4

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # ------------------------------------------------------------------

    def set_spread(
        self,
        doc,
        slug: str,
        left_i: Optional[int],
        right_i: Optional[int],
        spread_idx: int = 0,
    ) -> None:
        """Set document + page indices then render immediately."""
        self._doc    = doc
        self._slug   = slug
        self._left_i  = left_i
        self._right_i = right_i
        self._spread_idx = spread_idx
        self._debounce.stop()
        self._do_render()

    def clear(self) -> None:
        self._doc    = None
        self._slug   = ""
        self._left_i  = None
        self._right_i = None
        self._debounce.stop()
        self.update()

    def invalidate(self, slug: Optional[str] = None) -> None:
        """Drop cached renders (for one slug, or all)."""
        if slug is None:
            self._cache.clear()
        else:
            for k in list(self._cache):
                if k[0] == slug:
                    del self._cache[k]

    def set_layers(self, layers: list) -> None:
        """Update the full layer list used by the reveal compositor.

        Clears both the spread cache and the per-page reveal cache so the next
        render picks up the new cuts, then triggers an immediate re-render.
        """
        self._all_layers = list(layers)
        self._layers_ver += 1
        self._cache.clear()
        self._reveal_cache.clear()
        if self._doc is not None:
            self._do_render()

    # ------------------------------------------------------------------

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._overlay is not None:
            self._overlay.setGeometry(0, 0, self.width(), self.height())
        if self._doc is not None:
            self._debounce.start()   # restart on each resize step; fires once idle

    # ------------------------------------------------------------------

    def _cell_size(self):
        """Return (page_w, avail_h) for a single page cell."""
        page_w = max(1, (self.width()  - 2 * _MARGIN - _GUTTER) // 2)
        avail_h = max(1,  self.height() - 2 * _MARGIN)
        return page_w, avail_h

    def page_rects(self) -> tuple:
        """Return (left_rect, right_rect) as QRect|None in widget coordinates.

        Mirrors the blit geometry used in paintEvent so callers can do
        hit-testing against the exact rendered page positions.
        """
        key = (self._slug, self._left_i, self._right_i, self.width(), self.height())
        if key not in self._cache:
            return None, None
        left_img, right_img = self._cache[key]
        cell_w, cell_h = self._cell_size()
        m, g = _MARGIN, _GUTTER

        def _img_rect(img, x, align_right):
            if img is None:
                return None
            iw, ih = img.width(), img.height()
            dy = m + (cell_h - ih) // 2
            dx = (x + cell_w - iw) if align_right else x
            return QRect(dx, dy, iw, ih)

        left_rect  = _img_rect(left_img,  m,              align_right=True)
        right_rect = _img_rect(right_img, m + cell_w + g, align_right=False)
        return left_rect, right_rect

    def _render_page(self, page_idx: int, cell_w: int, cell_h: int) -> Optional[QImage]:
        """Render one PDF page to fit cell_w × cell_h; returns QImage."""
        import fitz
        try:
            page = self._doc[page_idx]
        except Exception:
            return None
        rect = page.rect
        iw, ih = rect.width, rect.height
        if iw <= 0 or ih <= 0:
            return None
        scale = min(cell_w / iw, cell_h / ih)
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        return QImage(
            pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888
        ).copy()

    def _render_page_with_reveals(
        self,
        page_idx: int,
        cell_w: int,
        cell_h: int,
        layers_by_page: dict,
        depth: int,
    ) -> Optional[QImage]:
        """Render *page_idx* and recursively composite pages visible through cuts.

        *layers_by_page* maps page_idx → list of cut layer dicts (all pages).
        Results are memoized in ``_reveal_cache`` for the lifetime of the current
        layer version so repeated calls within the same render are free.
        Recursion stops when ``depth`` reaches ``_REVEAL_MAX_DEPTH`` or when no
        further pages exist.
        """
        rev_key = (self._slug, page_idx, cell_w, cell_h, self._layers_ver, depth)
        if rev_key in self._reveal_cache:
            return self._reveal_cache[rev_key]

        # --- base render ---------------------------------------------------
        img = self._render_page(page_idx, cell_w, cell_h)
        if img is None:
            return None

        iw, ih = img.width(), img.height()

        # --- find closed cut polygons on this page -------------------------
        cuts = [
            l for l in layers_by_page.get(page_idx, [])
            if l.get("closed")
            and len(l.get("geometry", {}).get("points", [])) >= 3
        ]

        if not cuts or depth >= self._REVEAL_MAX_DEPTH:
            self._reveal_cache[rev_key] = img
            return img

        # --- get the page behind -------------------------------------------
        behind_idx = _page_behind(page_idx)
        if behind_idx >= self._doc.page_count:
            self._reveal_cache[rev_key] = img
            return img

        behind_img = self._render_page_with_reveals(
            behind_idx, cell_w, cell_h, layers_by_page, depth + 1
        )
        if behind_img is None:
            self._reveal_cache[rev_key] = img
            return img

        # --- composite: paint behind through each cut polygon --------------
        # Also collect sub-cuts on the behind page to draw as faint outlines
        behind_cuts = [
            l for l in layers_by_page.get(behind_idx, [])
            if l.get("closed")
            and len(l.get("geometry", {}).get("points", [])) >= 3
        ]
        outline_alpha = max(18, 90 - depth * 28)   # fainter at greater depth (reserved)
        outline_color = QColor(230, 230, 230, 255)

        result = img.copy()
        painter = QPainter(result)
        painter.setRenderHint(QPainter.Antialiasing)

        for cut in cuts:
            pts = cut["geometry"]["points"]
            poly = QPolygonF([QPointF(nx * iw, ny * ih) for nx, ny in pts])
            path = QPainterPath()
            path.addPolygon(poly)
            path.closeSubpath()
            painter.setClipPath(path)
            # Scale behind_img to fill iw×ih so coordinates always align
            painter.drawImage(QRect(0, 0, iw, ih), behind_img)
            # Draw sub-cut outlines clipped to this reveal window
            if behind_cuts:
                painter.setPen(QPen(outline_color, 1.0))
                painter.setBrush(Qt.NoBrush)
                for bcut in behind_cuts:
                    bpts = bcut["geometry"]["points"]
                    bpoly = QPolygonF([QPointF(nx * iw, ny * ih) for nx, ny in bpts])
                    painter.drawPolygon(bpoly)

        painter.setClipping(False)
        painter.end()

        self._reveal_cache[rev_key] = result
        # Bound reveal cache to avoid unbounded growth during rapid resizing
        if len(self._reveal_cache) > 120:
            # Drop oldest half
            keys = list(self._reveal_cache)
            for k in keys[: len(keys) // 2]:
                del self._reveal_cache[k]
        return result

    def _do_render(self) -> None:
        """Render current spread into cache then repaint."""
        if self._doc is None:
            self.update()
            return
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        key = (self._slug, self._left_i, self._right_i, w, h)
        if key not in self._cache:
            cell_w, cell_h = self._cell_size()
            # Build page → layers lookup for reveal compositor
            layers_by_page: dict = {}
            for layer in self._all_layers:
                pi = layer.get("page")
                if pi is not None:
                    layers_by_page.setdefault(pi, []).append(layer)
            left_img  = self._render_page_with_reveals(self._left_i,  cell_w, cell_h, layers_by_page, 0) if self._left_i  is not None else None
            right_img = self._render_page_with_reveals(self._right_i, cell_w, cell_h, layers_by_page, 0) if self._right_i is not None else None
            self._cache[key] = (left_img, right_img)
            # Keep cache bounded
            while len(self._cache) > 10:
                del self._cache[next(iter(self._cache))]
        self.update()

    # ------------------------------------------------------------------

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(theme.CANVAS_BG))

        if self._doc is None:
            p.end()
            return

        key = (self._slug, self._left_i, self._right_i, self.width(), self.height())
        if key not in self._cache:
            p.end()
            return

        left_img, right_img = self._cache[key]
        if left_img is None and right_img is None:
            p.end()
            return

        cell_w, cell_h = self._cell_size()
        m, g = _MARGIN, _GUTTER

        def _blit(img: QImage, x: int, align_right: bool) -> None:
            iw, ih = img.width(), img.height()
            dy = m + (cell_h - ih) // 2
            dx = (x + cell_w - iw) if align_right else x
            p.drawImage(dx, dy, img)

        if left_img is not None and right_img is not None:
            _blit(left_img,  m,              align_right=True)
            _blit(right_img, m + cell_w + g, align_right=False)
        elif right_img is not None:
            _blit(right_img, m + cell_w + g, align_right=False)
        elif left_img is not None:
            _blit(left_img,  m,              align_right=True)

        p.end()


# ---------------------------------------------------------------------------
# _CutOverlay — transparent drawing canvas for cut tool
# ---------------------------------------------------------------------------

class _CutOverlay(QWidget):
    """Transparent overlay stacked on top of _SpreadView.

    Handles cut/erase tool mouse interactions and renders polygon geometry
    on top of the page images.  Coordinates are stored as normalised (0..1)
    floats relative to the rendered page rect so they scale with the window.
    """

    layer_committed  = pyqtSignal(dict)   # emitted when a polygon is closed
    layer_removed    = pyqtSignal(str)    # emitted with layer id on deletion
    selection_changed = pyqtSignal(str)   # emitted with layer id (or "")

    def __init__(self, parent_view: "_SpreadView") -> None:
        super().__init__(parent_view)
        self._view = parent_view

        self._tool: str = _TOOL_NONE
        self._layers: list = []            # committed layer dicts

        # in-progress (open) polygon
        self._wip_points: list = []        # [[nx, ny], ...]
        self._wip_page: Optional[int] = None

        # selection
        self._sel_id:  Optional[str] = None
        self._sel_pt:  Optional[int] = None   # selected point index

        # hover for segment-insertion preview
        self._hover_seg: Optional[tuple] = None  # (layer_id, seg_idx, QPointF screen)

        # whole-polygon drag
        self._drag_id:     Optional[str]   = None
        self._drag_origin: Optional[list]  = None  # deep copy of points at drag start
        self._drag_start:  Optional[QPointF] = None

        # single-point drag
        self._pt_drag_id:  Optional[str] = None
        self._pt_drag_idx: Optional[int] = None

        self._mouse_pos: Optional[QPointF] = None  # for live WIP preview
        self._show_outlines: bool = True            # toggle via checkbox

        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.raise_()

    # ------------------------------------------------------------------
    # Public API

    def set_show_outlines(self, visible: bool) -> None:
        self._show_outlines = visible
        self.update()

    def set_tool(self, tool: str) -> None:
        self._tool = tool
        if tool == _TOOL_CUT:
            self.setCursor(Qt.CrossCursor)
        elif tool == _TOOL_ERASE:
            self.setCursor(Qt.ForbiddenCursor)
        else:
            self.setCursor(Qt.ArrowCursor)
        self.update()

    def set_layers(self, layers: list) -> None:
        """Replace displayed layers (e.g. on book switch)."""
        self._layers = [dict(layer) for layer in layers]
        self._sel_id = None
        self._sel_pt = None
        self._pt_drag_id = None
        self._pt_drag_idx = None
        self._drag_id = None
        self._drag_origin = None
        self._drag_start = None
        self._wip_points = []
        self._wip_page = None
        self.update()

    def current_layers(self) -> list:
        return list(self._layers)

    def cancel_wip(self) -> None:
        self._wip_points = []
        self._wip_page = None
        self._pt_drag_id = None
        self._pt_drag_idx = None
        self.update()

    def delete_selected_point(self) -> Optional[str]:
        """Delete the selected point; delete whole layer if < 3 pts remain.

        Returns the layer id if the layer was fully deleted, else None.
        """
        if self._sel_id is None or self._sel_pt is None:
            return None
        layer = self._layer_by_id(self._sel_id)
        if layer is None:
            return None
        pts = layer["geometry"]["points"]
        del pts[self._sel_pt]
        self._sel_pt = None
        if len(pts) < 3:
            lid = layer["id"]
            mirror_id = layer.get("mirror_id")
            self._layers.remove(layer)
            self._sel_id = None
            if mirror_id:
                mirror = self._layer_by_id(mirror_id)
                if mirror is not None:
                    self._layers.remove(mirror)
                    self.layer_removed.emit(mirror_id)
            self.update()
            self.layer_removed.emit(lid)
            return lid
        self._sync_mirror_for(layer["id"])
        self.update()
        return None

    def delete_selected_layer(self) -> Optional[str]:
        """Delete the currently selected layer. Returns layer id or None."""
        if self._sel_id is None:
            return None
        layer = self._layer_by_id(self._sel_id)
        if layer is None:
            return None
        lid = layer["id"]
        mirror_id = layer.get("mirror_id")
        self._layers.remove(layer)
        self._sel_id = None
        self._sel_pt = None
        if mirror_id:
            mirror = self._layer_by_id(mirror_id)
            if mirror is not None:
                self._layers.remove(mirror)
                self.layer_removed.emit(mirror_id)
        self.update()
        self.layer_removed.emit(lid)
        return lid

    def delete_layers_on_spread(self, spread_idx: int) -> list:
        """Remove all layers on *spread_idx* and their mirrors; return all deleted ids."""
        to_del = [l for l in self._layers if l.get("spread") == spread_idx]
        ids = [l["id"] for l in to_del]
        # Collect mirror layers that live on other spreads
        mirror_ids = []
        for l in to_del:
            mid = l.get("mirror_id")
            if mid and mid not in ids:
                ml = self._layer_by_id(mid)
                if ml is not None:
                    mirror_ids.append(mid)
        for l in to_del:
            self._layers.remove(l)
        for mid in mirror_ids:
            ml = self._layer_by_id(mid)
            if ml is not None:
                self._layers.remove(ml)
        all_ids = ids + mirror_ids
        if self._sel_id in all_ids:
            self._sel_id = None
            self._sel_pt = None
        self.update()
        return all_ids

    def add_layer(self, layer: dict) -> None:
        """Add a layer externally (e.g. from loaded file)."""
        self._layers.append(dict(layer))
        self.update()

    def select_layer(self, lid: Optional[str]) -> None:
        self._sel_id = lid
        self._sel_pt = None
        self.update()

    def rename_layer(self, lid: str, name: str) -> None:
        layer = self._layer_by_id(lid)
        if layer:
            layer["name"] = name
            self.update()

    def reorder_layers(self, new_ids: list) -> None:
        """Reorder self._layers to match new_ids order."""
        id_map = {l["id"]: l for l in self._layers}
        self._layers = [id_map[i] for i in new_ids if i in id_map]
        for z, l in enumerate(self._layers):
            l["z_index"] = z
        self.update()

    # ------------------------------------------------------------------
    # Mirror helpers

    def _reverse_page(self, page_idx: int) -> int:
        """Return the page index on the reverse side of the same physical leaf.

        Even pages are right-side (recto), odd pages are left-side (verso).
        The back of page N is N+1 if N is even, or N-1 if N is odd.
        """
        return page_idx + 1 if page_idx % 2 == 0 else page_idx - 1

    def _sync_mirror_for(self, layer_id: str) -> None:
        """Update the mirror layer's points to match *layer_id*'s current points."""
        layer = self._layer_by_id(layer_id)
        if layer is None:
            return
        mirror_id = layer.get("mirror_id")
        if not mirror_id:
            return
        mirror = self._layer_by_id(mirror_id)
        if mirror is None:
            return
        mirror["geometry"]["points"] = _mirror_points(layer["geometry"]["points"])

    def _create_mirror_for(self, layer: dict) -> Optional[dict]:
        """Create and append a horizontally-mirrored ghost layer on the reverse page.

        Returns the mirror layer dict, or None if the reverse page is out of range.
        Sets ``mirror_id`` on both the original and the mirror.
        """
        page_idx = layer.get("page")
        if page_idx is None:
            return None
        doc = self._view._doc
        if doc is None:
            return None
        rev_page = self._reverse_page(page_idx)
        if rev_page < 0 or rev_page >= doc.page_count:
            return None
        mirror_id = f"cut_{uuid.uuid4().hex[:8]}"
        layer["mirror_id"] = mirror_id
        mirror_layer = {
            "id":       mirror_id,
            "type":     "Cut",
            "name":     layer.get("name", "Cut"),
            "page":     rev_page,
            "spread":   _spread_for_page(rev_page),
            "z_index":  len(self._layers),
            "closed":   layer.get("closed", True),
            "mirror_id": layer["id"],
            "geometry": {
                "type":   "polygon",
                "points": _mirror_points(layer["geometry"]["points"]),
            },
        }
        self._layers.append(mirror_layer)
        return mirror_layer

    # ------------------------------------------------------------------
    # Coordinate helpers

    def _visible_page_rects(self) -> dict:
        """Return {page_idx: QRect} for currently visible pages."""
        left_i  = self._view._left_i
        right_i = self._view._right_i
        lr, rr = self._view.page_rects()
        result = {}
        if left_i  is not None and lr is not None:
            result[left_i]  = lr
        if right_i is not None and rr is not None:
            result[right_i] = rr
        return result

    def _which_page(self, sx: float, sy: float) -> Optional[tuple]:
        """If (sx, sy) is inside a visible page, return (page_idx, nx, ny)."""
        for page_idx, rect in self._visible_page_rects().items():
            if rect.contains(int(sx), int(sy)):
                nx = (sx - rect.x()) / rect.width()
                ny = (sy - rect.y()) / rect.height()
                return page_idx, nx, ny
        return None

    def _to_screen(self, page_idx: int, nx: float, ny: float) -> Optional[QPointF]:
        rects = self._visible_page_rects()
        r = rects.get(page_idx)
        if r is None:
            return None
        return QPointF(r.x() + nx * r.width(), r.y() + ny * r.height())

    def _layer_by_id(self, lid: str) -> Optional[dict]:
        for l in self._layers:
            if l["id"] == lid:
                return l
        return None

    def _layer_screen_pts(self, layer: dict) -> list:
        page_idx = layer.get("page")
        rects = self._visible_page_rects()
        r = rects.get(page_idx)
        if r is None:
            return []
        return [
            QPointF(r.x() + nx * r.width(), r.y() + ny * r.height())
            for nx, ny in layer["geometry"]["points"]
        ]

    def _hit_point(self, pos: QPointF) -> Optional[tuple]:
        """Return (layer_id, pt_idx) if pos is near a handle."""
        for layer in reversed(self._layers):
            pts = self._layer_screen_pts(layer)
            for i, pt in enumerate(pts):
                if abs(pos.x() - pt.x()) <= _HIT_DIST and abs(pos.y() - pt.y()) <= _HIT_DIST:
                    return layer["id"], i
        return None

    def _hit_segment(self, pos: QPointF) -> Optional[tuple]:
        """Return (layer_id, seg_idx, insertion_point) if pos is near a segment."""
        for layer in reversed(self._layers):
            pts = self._layer_screen_pts(layer)
            if len(pts) < 2:
                continue
            segs = list(range(len(pts) - 1))
            if layer.get("closed") and len(pts) >= 3:
                segs.append(len(pts) - 1)  # closing segment
            for si in segs:
                a = pts[si]
                b = pts[(si + 1) % len(pts)]
                closest = self._closest_on_segment(pos, a, b)
                if closest is not None:
                    dist = (pos - closest).manhattanLength()
                    if dist <= _HIT_DIST:
                        return layer["id"], si, closest
        return None

    def _hit_polygon(self, pos: QPointF) -> Optional[str]:
        """Return layer_id if pos is inside any visible polygon."""
        px, py = pos.x(), pos.y()
        for layer in reversed(self._layers):
            if not layer.get("closed"):
                continue
            pts = self._layer_screen_pts(layer)
            if len(pts) < 3:
                continue
            poly = QPolygonF(pts)
            if poly.containsPoint(QPointF(px, py), Qt.OddEvenFill):
                return layer["id"]
        return None

    @staticmethod
    def _closest_on_segment(p: QPointF, a: QPointF, b: QPointF) -> Optional[QPointF]:
        abx = b.x() - a.x(); aby = b.y() - a.y()
        lenSq = abx * abx + aby * aby
        if lenSq == 0:
            return a
        t = max(0.0, min(1.0, ((p.x() - a.x()) * abx + (p.y() - a.y()) * aby) / lenSq))
        return QPointF(a.x() + t * abx, a.y() + t * aby)

    # ------------------------------------------------------------------
    # Mouse event dispatch

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            return
        pos = QPointF(event.pos())
        if self._tool == _TOOL_CUT:
            self._cut_press(pos)
        elif self._tool == _TOOL_ERASE:
            self._erase_press(pos)
        else:
            self._select_press(pos, event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            return
        if self._tool == _TOOL_CUT:
            if not self._wip_points:
                # Double-click on vertex → select whole layer
                pos = QPointF(event.pos())
                hit_pt = self._hit_point(pos)
                if hit_pt:
                    lid, _ = hit_pt
                    self._sel_id = lid
                    self._sel_pt = None
                    self._pt_drag_id = None
                    self._pt_drag_idx = None
                    self.update()
                    self.selection_changed.emit(lid)
                    return
            self._close_wip()
            return
        if self._tool == _TOOL_NONE:
            pos = QPointF(event.pos())
            hit_pt = self._hit_point(pos)
            if hit_pt:
                lid, _ = hit_pt
                self._sel_id = lid
                self._sel_pt = None      # whole layer selected
                self._pt_drag_id = None
                self._pt_drag_idx = None
                self.update()
                self.selection_changed.emit(lid)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self._mouse_pos = QPointF(event.pos())
        if self._pt_drag_id is not None:
            self._pt_drag_move(self._mouse_pos)
        elif self._drag_id is not None:
            self._drag_move(self._mouse_pos)
        else:
            self._update_hover(self._mouse_pos)
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            if self._pt_drag_id is not None:
                dragged_id = self._pt_drag_id
                self._pt_drag_id = None
                self._pt_drag_idx = None
                self._sync_mirror_for(dragged_id)
                self.layer_committed.emit({})
            elif self._drag_id is not None:
                dragged_id = self._drag_id
                self._drag_id = None
                self._drag_origin = None
                self._drag_start = None
                self._sync_mirror_for(dragged_id)
                self.layer_committed.emit({})   # signal layers changed (empty = update only)

    # ------------------------------------------------------------------
    # CUT tool

    def _cut_press(self, pos: QPointF) -> None:
        # When no WIP polygon is in progress, handle vertex/segment interaction first
        if not self._wip_points:
            # Click on existing vertex → select it + start point drag
            hit_pt = self._hit_point(pos)
            if hit_pt:
                lid, pi = hit_pt
                self._sel_id = lid
                self._sel_pt = pi
                self._pt_drag_id = lid
                self._pt_drag_idx = pi
                self.update()
                self.selection_changed.emit(lid)
                return
            # Click on segment hover dot → insert vertex
            if self._sel_id:
                hit_seg = self._hit_segment(pos)
                if hit_seg and hit_seg[0] == self._sel_id:
                    lid, si, insert_pt = hit_seg
                    layer = self._layer_by_id(lid)
                    if layer:
                        pts = layer["geometry"]["points"]
                        r = self._visible_page_rects().get(layer["page"])
                        if r:
                            nx2 = (insert_pt.x() - r.x()) / r.width()
                            ny2 = (insert_pt.y() - r.y()) / r.height()
                            pts.insert(si + 1, [nx2, ny2])
                            self._sel_pt = si + 1
                            self._hover_seg = None
                            self._sync_mirror_for(lid)
                            self.update()
                            self.layer_committed.emit({})
                            return

        # Normal WIP polygon drawing
        hit = self._which_page(pos.x(), pos.y())
        if hit is None:
            return
        page_idx, nx, ny = hit

        if not self._wip_points:
            self._sel_id = None   # clear selection when starting a new polygon
            self._sel_pt = None
            self._wip_page = page_idx
            self._wip_points = [[nx, ny]]
        elif page_idx == self._wip_page:
            # Check if clicking near first vertex to close (3+ pts)
            if len(self._wip_points) >= 3:
                first = self._to_screen(self._wip_page, *self._wip_points[0])
                if first is not None and (pos - first).manhattanLength() < _CLOSE_DIST:
                    self._close_wip()
                    return
            self._wip_points.append([nx, ny])
        self.update()

    def _close_wip(self) -> None:
        if len(self._wip_points) < 3:
            self.cancel_wip()
            return
        layer = {
            "id":     f"cut_{uuid.uuid4().hex[:8]}",
            "type":   "Cut",
            "name":   "Cut",
            "page":   self._wip_page,
            "spread": self._view._spread_idx,
            "z_index": len(self._layers),
            "closed": True,
            "geometry": {
                "type":   "polygon",
                "points": list(self._wip_points),
            },
        }
        self._wip_points = []
        self._wip_page = None
        self._layers.append(layer)
        mirror = self._create_mirror_for(layer)
        self.update()
        self.layer_committed.emit(layer)
        if mirror is not None:
            self.layer_committed.emit(mirror)

    # ------------------------------------------------------------------
    # ERASE tool

    def _erase_press(self, pos: QPointF) -> None:
        # Check point first
        hit_pt = self._hit_point(pos)
        if hit_pt:
            self._sel_id, self._sel_pt = hit_pt
            self.delete_selected_point()
            return
        # Then polygon
        lid = self._hit_polygon(pos)
        if lid:
            layer = self._layer_by_id(lid)
            if layer:
                mirror_id = layer.get("mirror_id")
                self._layers.remove(layer)
                if self._sel_id == lid:
                    self._sel_id = None
                    self._sel_pt = None
                if mirror_id:
                    mirror = self._layer_by_id(mirror_id)
                    if mirror is not None:
                        self._layers.remove(mirror)
                        self.layer_removed.emit(mirror_id)
                self.update()
                self.layer_removed.emit(lid)

    # ------------------------------------------------------------------
    # SELECT / DRAG tool (tool == _TOOL_NONE)

    def _select_press(self, pos: QPointF, event) -> None:
        # Point handle? → behaviour depends on current selection state
        hit_pt = self._hit_point(pos)
        if hit_pt:
            lid, pi = hit_pt
            # Whole layer already selected (e.g. after double-click) → drag whole polygon
            if lid == self._sel_id and self._sel_pt is None:
                layer = self._layer_by_id(lid)
                if layer:
                    self._drag_id = lid
                    self._drag_start = pos
                    self._drag_origin = [list(p) for p in layer["geometry"]["points"]]
                self.update()
                return
            # Otherwise: select single point + start point drag
            self._sel_id = lid
            self._sel_pt = pi
            self._pt_drag_id = lid
            self._pt_drag_idx = pi
            self._drag_id = None          # never both drags at once
            self.update()
            self.selection_changed.emit(lid)
            return

        # Segment insertion?
        hit_seg = self._hit_segment(pos)
        if hit_seg and self._sel_id == hit_seg[0]:
            lid, si, insert_pt = hit_seg
            layer = self._layer_by_id(lid)
            if layer:
                pts = layer["geometry"]["points"]
                r = self._visible_page_rects().get(layer["page"])
                if r:
                    nx = (insert_pt.x() - r.x()) / r.width()
                    ny = (insert_pt.y() - r.y()) / r.height()
                    pts.insert(si + 1, [nx, ny])
                    self._sel_pt = si + 1
                    self._sync_mirror_for(lid)
                    self.update()
                    self.layer_committed.emit({})
                    return

        # Polygon body?
        lid = self._hit_polygon(pos)
        if lid:
            self._sel_id = lid
            self._sel_pt = None
            self.selection_changed.emit(lid)
            layer = self._layer_by_id(lid)
            if layer:
                self._drag_id = lid
                self._drag_start = pos
                self._drag_origin = [list(p) for p in layer["geometry"]["points"]]
            self.update()
            return

        # Empty space → clear selection
        self._sel_id = None
        self._sel_pt = None
        self.selection_changed.emit("")
        self.update()

    def _pt_drag_move(self, pos: QPointF) -> None:
        """Move the single selected point to cursor position."""
        if self._pt_drag_id is None or self._pt_drag_idx is None:
            return
        layer = self._layer_by_id(self._pt_drag_id)
        if layer is None:
            return
        rects = self._visible_page_rects()
        r = rects.get(layer["page"])
        if r is None or r.width() <= 0 or r.height() <= 0:
            return
        nx = (pos.x() - r.x()) / r.width()
        ny = (pos.y() - r.y()) / r.height()
        layer["geometry"]["points"][self._pt_drag_idx] = [nx, ny]

    def _drag_move(self, pos: QPointF) -> None:
        if self._drag_id is None or self._drag_start is None or self._drag_origin is None:
            return
        layer = self._layer_by_id(self._drag_id)
        if layer is None:
            return
        rects = self._visible_page_rects()
        r = rects.get(layer["page"])
        if r is None or r.width() <= 0 or r.height() <= 0:
            return
        dx = (pos.x() - self._drag_start.x()) / r.width()
        dy = (pos.y() - self._drag_start.y()) / r.height()
        layer["geometry"]["points"] = [
            [ox + dx, oy + dy] for ox, oy in self._drag_origin
        ]

    # ------------------------------------------------------------------
    # Hover for segment-insertion indicator

    def _update_hover(self, pos: QPointF) -> None:
        # Show segment-insertion hint when:
        #   • select tool: whole layer selected (_sel_pt is None)
        #   • cut tool: layer selected and no WIP in progress
        in_select = self._tool == _TOOL_NONE and self._sel_pt is None
        in_cut    = self._tool == _TOOL_CUT  and not self._wip_points
        if not (in_select or in_cut) or self._sel_id is None:
            self._hover_seg = None
            return
        hit_seg = self._hit_segment(pos)
        if hit_seg and hit_seg[0] == self._sel_id:
            self._hover_seg = hit_seg
        else:
            self._hover_seg = None

    # ------------------------------------------------------------------
    # Paint

    def paintEvent(self, _event) -> None:  # noqa: N802
        rects = self._visible_page_rects()

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        CUT_COLOR  = QColor("#e6e6e6")           # 90% grey for unselected
        SEL_COLOR  = QColor(theme.ACCENT)        # fuchsia for selected
        FILL_ALPHA = QColor(230, 230, 230, 12)   # very subtle light fill
        SEL_FILL   = QColor(255, 0, 255, 40)
        hr = _HANDLE_R - 1                       # slightly smaller handles

        # ── committed layers ─────────────────────────────────────────
        for layer in self._layers:
            page_idx = layer.get("page")
            if page_idx not in rects:
                continue
            pts = self._layer_screen_pts(layer)
            if len(pts) < 2:
                continue

            is_sel     = layer["id"] == self._sel_id
            has_sel_pt = is_sel and self._sel_pt is not None
            closed     = layer.get("closed", False)
            line_color = SEL_COLOR if is_sel else CUT_COLOR
            fill       = SEL_FILL  if is_sel else FILL_ALPHA

            pen = QPen(line_color, 1.5)
            p.setPen(pen)

            if closed and len(pts) >= 3:
                poly = QPolygonF(pts)
                p.setBrush(fill)
                p.drawPolygon(poly)
                p.setBrush(Qt.NoBrush)
            else:
                p.setBrush(Qt.NoBrush)
                for i in range(len(pts) - 1):
                    p.drawLine(pts[i], pts[i + 1])

            # point handles — always shown for selected; only when handles ON for others
            if self._show_outlines or is_sel:
                for i, pt in enumerate(pts):
                    # all handles fuchsia when layer selected; grey otherwise
                    hcolor = SEL_COLOR if is_sel else CUT_COLOR
                    if closed:
                        p.setBrush(hcolor)
                        p.setPen(QPen(QColor("#000000"), 0.8))
                    else:
                        p.setBrush(Qt.NoBrush)
                        p.setPen(QPen(hcolor, 1.5))
                    p.drawRect(int(pt.x()) - hr, int(pt.y()) - hr, hr * 2, hr * 2)

        # ── segment-insertion hover dot ───────────────────────────────
        if self._hover_seg:
            _, _, insert_pt = self._hover_seg
            p.setPen(QPen(SEL_COLOR, 1.5))
            p.setBrush(SEL_COLOR)
            p.drawEllipse(insert_pt, hr - 1, hr - 1)

        # ── work-in-progress polygon ──────────────────────────────────
        if self._wip_points and self._wip_page in rects:
            r = rects[self._wip_page]
            pts_s = [
                QPointF(r.x() + nx * r.width(), r.y() + ny * r.height())
                for nx, ny in self._wip_points
            ]
            pen = QPen(SEL_COLOR, 1.5, Qt.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            for i in range(len(pts_s) - 1):
                p.drawLine(pts_s[i], pts_s[i + 1])
            if self._mouse_pos is not None:
                p.drawLine(pts_s[-1], self._mouse_pos)
            # open handles
            p.setPen(QPen(SEL_COLOR, 1.5))
            p.setBrush(Qt.NoBrush)
            for pt in pts_s:
                p.drawRect(int(pt.x()) - hr, int(pt.y()) - hr, hr * 2, hr * 2)

        p.end()


# ---------------------------------------------------------------------------
# _PageBar — horizontal fuchsia position indicator
# ---------------------------------------------------------------------------

class _PageBar(QWidget):
    """Horizontal bar showing current spread position; clickable to jump."""

    jumped = pyqtSignal(int)   # emits spread index

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._spread_count = 0
        self._current = 0
        self.setFixedHeight(_BAR_H)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_state(self, current: int, total: int) -> None:
        self._current = current
        self._spread_count = total
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(theme.INPUT_BG))
        n = self._spread_count
        if n > 0:
            cell_w = self.width() / n
            x = int(self._current * cell_w)
            pw = max(4, int(cell_w))
            p.fillRect(x, 0, pw, self.height(), QColor(theme.ACCENT))
        p.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._jump(event.x())

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if event.buttons() & Qt.LeftButton:
            self._jump(event.x())

    def _jump(self, x: int) -> None:
        if self._spread_count <= 0 or self.width() <= 0:
            return
        idx = int(x / self.width() * self._spread_count)
        idx = max(0, min(self._spread_count - 1, idx))
        self.jumped.emit(idx)


# ---------------------------------------------------------------------------
# _LayerRow — single row in the Layers panel
# ---------------------------------------------------------------------------

class _LayerRow(QWidget):
    """One layer entry: drag handle | name (double-click to rename) | × button."""

    delete_requested = pyqtSignal(str)   # layer id
    rename_requested = pyqtSignal(str, str)  # layer id, new name
    selected         = pyqtSignal(str)   # layer id

    def __init__(self, layer: dict, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._lid  = layer["id"]
        self._name = layer.get("name", "Cut")
        self._layer_type = layer.get("type", "Cut")
        self._editing = False

        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(4)

        # -- type icon --
        _LAYER_ICONS = {"Cut": "cut"}
        icon_name = _LAYER_ICONS.get(self._layer_type)
        if icon_name:
            self._type_icon = QLabel()
            self._type_icon.setPixmap(_svg_icon(icon_name, 12, theme.TEXT_DIM).pixmap(12, 12))
            self._type_icon.setFixedSize(14, 14)
            self._type_icon.setAlignment(Qt.AlignCenter)
            self._type_icon.setStyleSheet("background: transparent;")
            lay.addWidget(self._type_icon)
            lay.addSpacing(2)

        # -- name label (normal state) --
        self._name_label = QLabel(self._name)
        self._name_label.setStyleSheet(
            f"color: {theme.TEXT}; font-family: '{theme.FAMILY_UI}';"
            f" font-size: {theme.BASE_PT}pt;"
        )
        self._name_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        # -- inline editor (edit state) --
        self._name_edit = QLineEdit(self._name)
        self._name_edit.setStyleSheet(
            f"QLineEdit {{ color: {theme.TEXT}; background: {theme.INPUT_BG};"
            f" font-family: '{theme.FAMILY_UI}'; font-size: {theme.BASE_PT}pt;"
            f" border: 1px solid {theme.ACCENT}; border-radius: 2px; padding: 0 2px;"
            f" selection-background-color: {theme.ACCENT}; selection-color: {theme.TEXT}; }}"
        )
        self._name_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._name_edit.hide()
        self._name_edit.returnPressed.connect(self._commit_rename)
        self._name_edit.editingFinished.connect(self._commit_rename)

        lay.addWidget(self._name_label)
        lay.addWidget(self._name_edit)

        self._del_btn = QPushButton()
        self._del_btn.setIcon(_svg_icon("trash", 12, theme.TEXT_DIM))
        self._del_btn.setIconSize(QSize(12, 12))
        self._del_btn.setFixedSize(18, 18)
        self._del_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; }}"
            f"QPushButton:hover {{ background: transparent; }}"
        )
        self._del_btn.setToolTip("Delete layer")
        self._del_btn.clicked.connect(lambda: self.delete_requested.emit(self._lid))
        lay.addWidget(self._del_btn)

        self.setFixedHeight(26)
        self.setFocusPolicy(Qt.NoFocus)
        self._del_btn.setFocusPolicy(Qt.NoFocus)
        self.set_selected(False)

    def _start_rename(self) -> None:
        if self._editing:
            return
        self._editing = True
        self._name_edit.setText(self._name)
        self._name_label.hide()
        self._name_edit.show()
        self._name_edit.setFocus()
        self._name_edit.selectAll()

    def _commit_rename(self) -> None:
        if not self._editing:
            return
        self._editing = False
        new_name = self._name_edit.text().strip() or self._name
        self._name_edit.hide()
        self._name_label.show()
        if new_name != self._name:
            self._name = new_name
            self._name_label.setText(new_name)
            self.rename_requested.emit(self._lid, new_name)

    def set_selected(self, selected: bool) -> None:
        bg = theme.ACCENT if selected else theme.BTN_BG
        text = theme.TEXT if selected else theme.TEXT_DIM
        self._name_label.setStyleSheet(
            f"color: {text}; font-family: '{theme.FAMILY_UI}';"
            f" font-size: {theme.BASE_PT}pt; background: transparent;"
        )
        self.setStyleSheet(
            f"_LayerRow {{ background: {bg}; border-radius: 3px; }}"
        )

    def update_name(self, name: str) -> None:
        self._name = name
        self._name_label.setText(name)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self._editing:
            super().mousePressEvent(event)
            return
        self.selected.emit(self._lid)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self._start_rename()


# ---------------------------------------------------------------------------
# _LayerPanel — scrollable list of layer rows
# ---------------------------------------------------------------------------

class _LayerPanel(QWidget):
    """Scrollable layer list used inside the right control panel."""

    layer_selected    = pyqtSignal(str)       # id
    layer_deleted     = pyqtSignal(str)       # id
    layer_renamed     = pyqtSignal(str, str)  # id, name
    layers_reordered  = pyqtSignal(list)      # new id order

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._rows: dict = {}          # lid → _LayerRow
        self._order: list = []         # lid order (top = front)
        self._sel_id: Optional[str] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._list = QListWidget()
        self._list.setFocusPolicy(Qt.NoFocus)
        self._list.setDragDropMode(QListWidget.InternalMove)
        self._list.setDefaultDropAction(Qt.MoveAction)
        self._list.setStyleSheet(
            f"QListWidget {{ background: {theme.INPUT_BG}; border: none; }}"
            f"QListWidget::item {{ border-bottom: 1px solid {theme.PANEL_BG}; }}"
            f"QListWidget::item:selected {{ background: transparent; }}"
        )
        self._list.setSpacing(1)
        self._list.model().rowsMoved.connect(self._on_rows_moved)
        outer.addWidget(self._list)

    def set_layers(self, layers: list) -> None:
        self._list.clear()
        self._rows.clear()
        self._order.clear()
        for layer in layers:
            self._append_layer(layer)

    def add_layer(self, layer: dict) -> None:
        self._append_layer(layer)

    def _append_layer(self, layer: dict) -> None:
        lid = layer["id"]
        row = _LayerRow(layer)
        row.delete_requested.connect(self.layer_deleted)
        row.rename_requested.connect(self._on_rename)
        row.selected.connect(self._on_select)

        item = QListWidgetItem()
        item.setData(Qt.UserRole, lid)
        item.setSizeHint(row.sizeHint())
        self._list.addItem(item)
        self._list.setItemWidget(item, row)
        self._rows[lid] = row
        self._order.append(lid)

    def remove_layer(self, lid: str) -> None:
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item and item.data(Qt.UserRole) == lid:
                self._list.takeItem(i)
                break
        self._rows.pop(lid, None)
        if lid in self._order:
            self._order.remove(lid)
        if self._sel_id == lid:
            self._sel_id = None

    def remove_all(self) -> None:
        self._list.clear()
        self._rows.clear()
        self._order.clear()
        self._sel_id = None

    def select_layer(self, lid: Optional[str]) -> None:
        if self._sel_id and self._sel_id in self._rows:
            self._rows[self._sel_id].set_selected(False)
        self._sel_id = lid
        if lid and lid in self._rows:
            self._rows[lid].set_selected(True)

    def update_layer_name(self, lid: str, name: str) -> None:
        if lid in self._rows:
            self._rows[lid].update_name(name)

    def _on_select(self, lid: str) -> None:
        self.select_layer(lid)
        self.layer_selected.emit(lid)

    def _on_rename(self, lid: str, name: str) -> None:
        self.update_layer_name(lid, name)
        self.layer_renamed.emit(lid, name)

    def _on_rows_moved(self) -> None:
        new_order = []
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item:
                new_order.append(item.data(Qt.UserRole))
        self._order = new_order
        self.layers_reordered.emit(new_order)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class BookVisualizerWindow(QMainWindow):
    """Main window for the Book Visualizer."""

    def __init__(self, project_path: str) -> None:
        super().__init__()
        self.setWindowTitle("Crossing — Book Visualizer")
        self._project_path = project_path

        self._books: list[dict] = []
        self._current_book_idx: int = 0   # index into self._books
        self._doc = None                  # open fitz.Document (or None)
        self._slug: str = ""              # slug of the currently open book
        self._spread_idx: int = 0         # current spread index
        self._updating_combo: bool = False

        # tool / layer state
        self._tool: str = _TOOL_NONE
        self._next_layer_id: int = 1      # used by _CutOverlay for id generation
        self._clipboard_layer: Optional[dict] = None  # copy/cut clipboard

        self._build_ui()
        self._load_all_books()
        restore_window_geometry(self, "window_book")
        # Grab navigation keys regardless of which child widget has focus
        QApplication.instance().installEventFilter(self)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._save_current_layers()
        self._close_doc()
        save_window_geometry(self, "window_book")
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # UI construction

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)

        outer = QVBoxLayout(central)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(0)

        splitter = GripSplitter(Qt.Horizontal)
        outer.addWidget(splitter, stretch=1)

        # ── LEFT: spread view + page bar ─────────────────────────────
        left_col = QWidget()
        left_col.setStyleSheet(f"background: {theme.CANVAS_BG};")
        left_layout = QVBoxLayout(left_col)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        self._spread_view = _SpreadView()
        left_layout.addWidget(self._spread_view, stretch=1)

        # attach transparent drawing overlay as child of spread view
        self._overlay = _CutOverlay(self._spread_view)
        self._spread_view._overlay = self._overlay
        self._overlay.layer_committed.connect(self._on_layer_committed)
        self._overlay.layer_removed.connect(self._on_layer_removed)
        self._overlay.selection_changed.connect(self._on_overlay_selection)

        self._page_bar = _PageBar()
        self._page_bar.jumped.connect(self._go_spread)
        left_layout.addWidget(self._page_bar)

        splitter.addWidget(left_col)

        # ── RIGHT: control panel (mosaic-style) ───────────────────────
        panel = self._build_control_panel()
        splitter.addWidget(panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)

        self.setMinimumSize(700, 480)
        self.resize(1200, 800)

    def _build_control_panel(self) -> QWidget:
        # Outer container with fixed width
        outer = QWidget()
        outer.setFixedWidth(_PANEL_WIDTH)
        outer.setStyleSheet(_PANEL_STYLESHEET)

        outer_layout = QVBoxLayout(outer)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        # Scrollable interior
        scroll = QScrollArea()
        scroll.setFocusPolicy(Qt.NoFocus)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"QScrollArea {{ background: {theme.PANEL_BG}; border: none; }}")

        panel = QWidget()
        panel.setStyleSheet(_PANEL_STYLESHEET)
        scroll.setWidget(panel)
        outer_layout.addWidget(scroll)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(14)

        # ── Book group ────────────────────────────────────────────────
        book_group = QGroupBox("Book")
        book_layout = QVBoxLayout(book_group)
        book_layout.setContentsMargins(8, 12, 8, 8)
        book_layout.setSpacing(6)

        self._combo = QComboBox()
        self._combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._combo.setStyleSheet(
            f"QComboBox {{ background: {theme.INPUT_BG}; }}"
        )
        self._combo.currentIndexChanged.connect(self._on_combo_changed)
        self._combo.installEventFilter(self)
        book_layout.addWidget(self._combo)

        # Page info row
        dim_style = f"color: {theme.TEXT_DIM}; font-size: {theme.BASE_PT}pt;"

        self._pages_label = QLabel("—")
        self._pages_label.setWordWrap(True)
        self._pages_label.setStyleSheet(dim_style)
        book_layout.addWidget(self._pages_label)

        spread_row = QWidget()
        spread_row_layout = QHBoxLayout(spread_row)
        spread_row_layout.setContentsMargins(0, 0, 0, 0)
        spread_row_layout.setSpacing(2)

        self._spread_prefix = QLabel("")
        self._spread_prefix.setStyleSheet(dim_style)
        spread_row_layout.addWidget(self._spread_prefix)

        self._spread_page_edit = QLineEdit("")
        self._spread_page_edit.setPlaceholderText("\u2014")
        self._spread_page_edit.setStyleSheet(
            f"{dim_style} background: {theme.INPUT_BG}; border: none; padding: 1px 3px;"
            f" selection-background-color: {theme.ACCENT}; selection-color: {theme.TEXT};"
        )
        self._spread_page_edit.setToolTip("Type a page number and press Enter to jump")
        self._spread_page_edit.setFixedWidth(60)
        self._spread_page_edit.returnPressed.connect(self._on_page_entered)

        def _spread_page_mouse_press(e):
            QLineEdit.mousePressEvent(self._spread_page_edit, e)
            QTimer.singleShot(0, self._spread_page_edit.selectAll)

        self._spread_page_edit.mousePressEvent = _spread_page_mouse_press
        spread_row_layout.addWidget(self._spread_page_edit)

        self._spread_suffix = QLabel("")
        self._spread_suffix.setStyleSheet(dim_style)
        spread_row_layout.addWidget(self._spread_suffix)
        spread_row_layout.addStretch()

        book_layout.addWidget(spread_row)

        self._loading_label = QLabel("")
        self._loading_label.setWordWrap(True)
        self._loading_label.setStyleSheet(
            f"color: {theme.TEXT}; background: {theme.ACCENT};"
            f" font-size: {theme.BASE_PT}pt; padding: 3px 6px; border-radius: 3px;"
        )
        self._loading_label.hide()
        book_layout.addWidget(self._loading_label)

        # Import / New buttons
        book_btn_row = QHBoxLayout()
        book_btn_row.setSpacing(4)

        self._import_btn = QPushButton("Import")
        self._import_btn.setToolTip("Import a PDF into this book")
        self._import_btn.clicked.connect(self._on_import)
        book_btn_row.addWidget(self._import_btn)

        self._new_btn = QPushButton("New")
        self._new_btn.setToolTip("Create a new book")
        self._new_btn.clicked.connect(self._on_new_book)
        book_btn_row.addWidget(self._new_btn)

        self._delete_book_btn = QPushButton()
        self._delete_book_btn.setIcon(_svg_icon("trash", 14, theme.TEXT_DIM))
        self._delete_book_btn.setIconSize(QSize(14, 14))
        self._delete_book_btn.setFixedSize(26, 26)
        self._delete_book_btn.setToolTip("Delete this book")
        self._delete_book_btn.setFocusPolicy(Qt.NoFocus)
        self._delete_book_btn.clicked.connect(self._on_delete_book)
        book_btn_row.addWidget(self._delete_book_btn)

        book_layout.addLayout(book_btn_row)

        layout.addWidget(book_group)

        # ── Tools group ───────────────────────────────────────────────
        tools_group = QGroupBox("Tools")
        tools_layout = QVBoxLayout(tools_group)
        tools_layout.setContentsMargins(8, 12, 8, 8)
        tools_layout.setSpacing(6)

        tool_row = QHBoxLayout()
        tool_row.setSpacing(4)

        ICON_SIZE = 18
        BTN_SIZE  = 32

        self._cut_btn = QPushButton()
        self._cut_btn.setIcon(_svg_icon("cut", ICON_SIZE))
        self._cut_btn.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self._cut_btn.setCheckable(True)
        self._cut_btn.setFixedSize(BTN_SIZE, BTN_SIZE)
        self._cut_btn.setToolTip("Cut tool — draw polygon cuts")
        self._cut_btn.clicked.connect(lambda checked: self._set_tool(_TOOL_CUT if checked else _TOOL_NONE))
        tool_row.addWidget(self._cut_btn)

        self._erase_btn = QPushButton()
        self._erase_btn.setIcon(_svg_icon("erase", ICON_SIZE))
        self._erase_btn.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self._erase_btn.setCheckable(True)
        self._erase_btn.setFixedSize(BTN_SIZE, BTN_SIZE)
        self._erase_btn.setToolTip("Erase tool — click a cut to remove it")
        self._erase_btn.clicked.connect(lambda checked: self._set_tool(_TOOL_ERASE if checked else _TOOL_NONE))
        tool_row.addWidget(self._erase_btn)

        self._trash_btn = QPushButton()
        self._trash_btn.setIcon(_svg_icon("trash-solid", ICON_SIZE))
        self._trash_btn.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self._trash_btn.setFixedSize(BTN_SIZE, BTN_SIZE)
        self._trash_btn.setToolTip("Delete all cuts on this spread")
        self._trash_btn.clicked.connect(self._on_trash)
        tool_row.addWidget(self._trash_btn)

        tool_row.addStretch()
        tools_layout.addLayout(tool_row)

        self._show_outlines_chk = QCheckBox("Show Cut Handles")
        self._show_outlines_chk.setChecked(True)
        self._show_outlines_chk.setFocusPolicy(Qt.NoFocus)
        self._show_outlines_chk.toggled.connect(
            lambda checked: self._overlay.set_show_outlines(checked)
        )
        tools_layout.addWidget(self._show_outlines_chk)
        layout.addWidget(tools_group)

        # ── Layers group ──────────────────────────────────────────────
        layers_group = QGroupBox("Layers")
        layers_layout = QVBoxLayout(layers_group)
        layers_layout.setContentsMargins(8, 12, 8, 8)
        layers_layout.setSpacing(4)

        self._layer_panel = _LayerPanel()
        self._layer_panel.setMinimumHeight(100)
        self._layer_panel.layer_selected.connect(self._on_panel_layer_selected)
        self._layer_panel.layer_deleted.connect(self._on_panel_layer_deleted)
        self._layer_panel.layer_renamed.connect(self._on_panel_layer_renamed)
        self._layer_panel.layers_reordered.connect(self._on_panel_layers_reordered)
        layers_layout.addWidget(self._layer_panel)

        layout.addWidget(layers_group)
        layout.addStretch()

        return outer

    # ------------------------------------------------------------------
    # Data loading

    def _load_all_books(self) -> None:
        """Populate the combo box and show the current book (or first)."""
        from data.book import list_books

        self._books = list_books(self._project_path)

        self._updating_combo = True
        self._combo.clear()
        for book in self._books:
            self._combo.addItem(book.get("slug", ""))
        self._updating_combo = False

        if not self._books:
            self._pages_label.setText("No books yet.")
            self._spread_view.clear()
            return

        try:
            from tool import prefs
            current = prefs.get("current_book")
        except Exception:
            current = None

        start_idx = 0
        if current:
            for i, book in enumerate(self._books):
                if book.get("slug") == current:
                    start_idx = i
                    break

        self._show_book(start_idx)

    def _show_book(self, idx: int) -> None:
        if idx < 0 or idx >= len(self._books):
            return

        self._current_book_idx = idx
        self._close_doc()
        self._spread_idx = 0

        book = self._books[idx]
        slug = book.get("slug", "")
        pdf_rel = book.get("pdf")
        page_count = book.get("page_count", 0)

        if not self._updating_combo and self._combo.currentIndex() != idx:
            self._updating_combo = True
            self._combo.setCurrentIndex(idx)
            self._updating_combo = False

        self._page_bar.set_state(0, 0)
        self._spread_prefix.setText("")
        self._spread_page_edit.setText("")
        self._spread_suffix.setText("")
        self._loading_label.hide()

        if not pdf_rel:
            self._pages_label.setText("No PDF imported.")
            self._import_btn.setText("Import")
            self._import_btn.setToolTip("Import a PDF into this book")
            self._persist_current(slug)
            return

        from data.book import book_dir
        pdf_path = book_dir(self._project_path, slug) / pdf_rel

        if not pdf_path.exists():
            self._pages_label.setText("PDF file missing.")
            self._import_btn.setText("Import")
            self._import_btn.setToolTip("Import a PDF into this book")
            self._persist_current(slug)
            return

        import fitz
        try:
            self._doc  = fitz.open(str(pdf_path))
            self._slug = slug
        except Exception as exc:
            self._pages_label.setText(f"Error: {exc}")
            return

        self._pages_label.setText(f"{page_count} pages")
        self._import_btn.setText("Replace")
        self._import_btn.setToolTip("Replace the current PDF with a new file")
        self._persist_current(slug)
        # Restore last-visited spread for this book
        try:
            from tool import prefs
            saved = prefs.get(f"spread_{slug}")
            if saved is not None:
                n = _spread_count(page_count)
                self._spread_idx = max(0, min(n - 1, int(saved)))
        except Exception:
            pass
        self._show_spread()
        # Load layers for this book
        self._load_book_layers(slug)

    def _go_spread(self, idx: int) -> None:
        """Jump to spread *idx*."""
        if self._doc is None:
            return
        n = _spread_count(self._doc.page_count)
        if n == 0:
            return
        self._spread_idx = max(0, min(n - 1, idx))
        self._show_spread()

    def _show_spread(self) -> None:
        """Display self._spread_idx from the open document."""
        if self._doc is None:
            self._spread_view.clear()
            self._page_bar.set_state(0, 0)
            self._spread_prefix.setText("")
            self._spread_page_edit.setText("")
            self._spread_suffix.setText("")
            return
        page_count = self._doc.page_count
        n = _spread_count(page_count)
        if n == 0:
            self._spread_view.clear()
            self._page_bar.set_state(0, 0)
            self._spread_prefix.setText("")
            self._spread_page_edit.setText("")
            self._spread_suffix.setText("")
            return
        left_i, right_i = _pages_for_spread(self._spread_idx, page_count)
        self._spread_view.set_spread(self._doc, self._slug, left_i, right_i, self._spread_idx)
        self._page_bar.set_state(self._spread_idx, n)
        if left_i is None:
            self._spread_prefix.setText("p.")
            self._spread_page_edit.setText("1")
        elif right_i is None:
            self._spread_prefix.setText("p.")
            self._spread_page_edit.setText(str(left_i + 1))
        else:
            self._spread_prefix.setText("pp.")
            self._spread_page_edit.setText(f"{left_i + 1}–{right_i + 1}")
        self._spread_suffix.setText(f" of {page_count}")
        self._persist_spread()
        self._refresh_layer_panel()

    def _close_doc(self) -> None:
        """Close the open fitz document and reset the spread view."""
        self._save_current_layers()
        self._overlay.set_layers([])
        self._layer_panel.remove_all()
        self._spread_view.clear()
        if self._doc is not None:
            try:
                self._doc.close()
            except Exception:
                pass
            self._doc = None
        self._slug = ""

    def _persist_current(self, slug: str) -> None:
        try:
            from tool import prefs
            prefs.set("current_book", slug)
        except Exception:
            pass

    def _persist_spread(self) -> None:
        if not self._slug:
            return
        try:
            from tool import prefs
            prefs.set(f"spread_{self._slug}", self._spread_idx)
        except Exception:
            pass

    def _on_page_entered(self) -> None:
        """Parse the number in _spread_page_edit and jump to that page's spread."""
        if self._doc is None:
            return
        import re
        text = self._spread_page_edit.text().strip()
        m = re.search(r'\d+', text)
        if not m:
            self._spread_page_edit.clearFocus()
            return
        page_num = int(m.group())
        page_count = self._doc.page_count
        page_idx = max(0, min(page_count - 1, page_num - 1))
        spread = 0 if page_idx == 0 else (page_idx + 1) // 2
        self._spread_page_edit.clearFocus()
        self._go_spread(spread)

    # ------------------------------------------------------------------
    # Tool management

    def _set_tool(self, tool: str) -> None:
        self._tool = tool
        self._overlay.set_tool(tool)
        # Sync button check states
        self._cut_btn.setChecked(tool == _TOOL_CUT)
        self._erase_btn.setChecked(tool == _TOOL_ERASE)
        # Cancel in-progress WIP when switching away from CUT
        if tool != _TOOL_CUT:
            self._overlay.cancel_wip()

    # ------------------------------------------------------------------
    # Layer persistence

    def _refresh_layer_panel(self) -> None:
        """Populate the Layers group box with only layers on the current spread."""
        if self._doc is None:
            self._layer_panel.remove_all()
            return
        left_i, right_i = _pages_for_spread(self._spread_idx, self._doc.page_count)
        visible_pages = {p for p in (left_i, right_i) if p is not None}
        visible = [l for l in self._overlay.current_layers()
                   if l.get("page") in visible_pages]
        self._layer_panel.set_layers(visible)

    def _load_book_layers(self, slug: str) -> None:
        layers = _load_layers(self._project_path, slug)
        self._overlay.set_layers(layers)
        self._spread_view.set_layers(layers)
        self._refresh_layer_panel()

    def _save_current_layers(self) -> None:
        if not self._slug:
            return
        layers = self._overlay.current_layers()
        try:
            _save_layers(self._project_path, self._slug, layers)
        except Exception:
            pass
        self._spread_view.set_layers(layers)

    # ------------------------------------------------------------------
    # Overlay callbacks

    def _on_layer_committed(self, layer: dict) -> None:
        """Called when overlay commits a new polygon (or signals layers changed)."""
        if layer:   # non-empty → actual new layer
            # Only add to panel if the layer is visible on the current spread
            if self._doc is not None:
                left_i, right_i = _pages_for_spread(self._spread_idx, self._doc.page_count)
                visible_pages = {p for p in (left_i, right_i) if p is not None}
                if layer.get("page") in visible_pages:
                    self._layer_panel.add_layer(layer)
            else:
                self._layer_panel.add_layer(layer)
        self._save_current_layers()

    def _on_layer_removed(self, lid: str) -> None:
        self._layer_panel.remove_layer(lid)
        self._save_current_layers()

    def _on_overlay_selection(self, lid: str) -> None:
        self._layer_panel.select_layer(lid if lid else None)

    # ------------------------------------------------------------------
    # Layer panel callbacks

    def _on_panel_layer_selected(self, lid: str) -> None:
        self._overlay.select_layer(lid)

    def _on_panel_layer_deleted(self, lid: str) -> None:
        layer = self._overlay._layer_by_id(lid)
        if layer is not None:
            mirror_id = layer.get("mirror_id")
            self._overlay._layers.remove(layer)
            if self._overlay._sel_id == lid:
                self._overlay._sel_id = None
                self._overlay._sel_pt = None
            if mirror_id:
                mirror = self._overlay._layer_by_id(mirror_id)
                if mirror is not None:
                    self._overlay._layers.remove(mirror)
                    self._layer_panel.remove_layer(mirror_id)
            self._overlay.update()
        self._layer_panel.remove_layer(lid)
        self._save_current_layers()

    def _on_panel_layer_renamed(self, lid: str, name: str) -> None:
        self._overlay.rename_layer(lid, name)
        self._save_current_layers()

    def _on_panel_layers_reordered(self, new_order: list) -> None:
        self._overlay.reorder_layers(new_order)
        self._save_current_layers()

    # ------------------------------------------------------------------
    # Trash action

    def _on_trash(self) -> None:
        if self._doc is None:
            return
        # Determine which pages are visible on the current spread
        left_i, right_i = _pages_for_spread(self._spread_idx, self._doc.page_count)
        visible_pages = {p for p in (left_i, right_i) if p is not None}
        cut_layers = [
            l for l in self._overlay.current_layers()
            if l.get("page") in visible_pages
        ]
        if not cut_layers:
            return
        ans = QMessageBox.question(
            self,
            "Delete All Cuts",
            f"Delete {len(cut_layers)} cut layer(s) on this spread?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        ids = self._overlay.delete_layers_on_spread(self._spread_idx)
        for lid in ids:
            self._layer_panel.remove_layer(lid)
        self._save_current_layers()

    # ------------------------------------------------------------------
    # Button actions

    def _on_import(self) -> None:
        """Open a file dialog to import a PDF into the current book."""
        if not self._books:
            QMessageBox.warning(self, "No Book", "Create a book first.")
            return

        pdf_path, _ = QFileDialog.getOpenFileName(
            self, "Import PDF", "", "PDF Files (*.pdf)"
        )
        if not pdf_path:
            return

        book = self._books[self._current_book_idx]
        slug = book.get("slug", "")
        force = book.get("pdf") is not None  # overwrite if already imported

        from data.book import import_pdf
        try:
            data = import_pdf(self._project_path, slug, pdf_path, force=force)
        except Exception as exc:
            QMessageBox.critical(self, "Import Failed", str(exc))
            return

        # Refresh book list entry and redisplay
        self._books[self._current_book_idx] = data
        self._updating_combo = True
        self._combo.setItemText(self._current_book_idx, data.get("slug", slug))
        self._updating_combo = False
        self._import_btn.setText("Import")   # reset label so _show_book can update it
        self._show_book(self._current_book_idx)

    def _on_new_book(self) -> None:
        """Prompt for a slug and create a new book."""
        slug, ok = QInputDialog.getText(
            self, "New Book", "Book slug (e.g. west-of-everything):"
        )
        if not ok or not slug.strip():
            return
        slug = slug.strip()

        from data.book import create_book
        try:
            data = create_book(self._project_path, slug)
        except FileExistsError as exc:
            QMessageBox.warning(self, "Already Exists", str(exc))
            return
        except Exception as exc:
            QMessageBox.critical(self, "Error", str(exc))
            return

        self._books.append(data)
        self._updating_combo = True
        self._combo.addItem(slug)
        self._updating_combo = False

        new_idx = len(self._books) - 1
        self._show_book(new_idx)

    def _on_delete_book(self) -> None:
        """Ask for confirmation then permanently delete the current book."""
        if not self._books:
            return
        book = self._books[self._current_book_idx]
        slug = book.get("slug", "")
        reply = QMessageBox.warning(
            self,
            "Delete Book",
            f"Permanently delete \u201c{slug}\u201d and all its data?\n\nThis cannot be undone.",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if reply != QMessageBox.Yes:
            return
        from data.book import delete_book
        try:
            delete_book(self._project_path, slug)
        except Exception as exc:
            QMessageBox.critical(self, "Error", str(exc))
            return
        idx = self._current_book_idx
        self._books.pop(idx)
        self._updating_combo = True
        self._combo.removeItem(idx)
        self._updating_combo = False
        new_idx = min(idx, len(self._books) - 1)
        if new_idx >= 0:
            self._show_book(new_idx)
        else:
            self._combo.setCurrentIndex(-1)
            self._slug = ""
            self._doc = None
            self._overlay.set_layers([])
            self._layer_panel.remove_all()

    # ------------------------------------------------------------------
    # Event handling

    def _on_combo_changed(self, idx: int) -> None:
        if self._updating_combo:
            return
        self._show_book(idx)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() == QEvent.KeyPress:
            key  = event.key()
            mods = event.modifiers()
            # Never intercept when a line-edit or open combo popup is active
            focused = QApplication.focusWidget()
            in_text = isinstance(focused, QLineEdit)
            ctrl = mods == Qt.ControlModifier
            if not in_text and not mods:
                if key == Qt.Key_Left:
                    self._go_spread(self._spread_idx - 1)
                    return True
                if key == Qt.Key_Right:
                    self._go_spread(self._spread_idx + 1)
                    return True
                if key in (Qt.Key_Up, Qt.Key_Home):
                    self._show_book(max(0, self._current_book_idx - 1))
                    return True
                if key in (Qt.Key_Down, Qt.Key_End):
                    self._show_book(min(len(self._books) - 1, self._current_book_idx + 1))
                    return True
            if not in_text and ctrl:
                if key == Qt.Key_C:
                    sel_id = self._overlay._sel_id
                    if sel_id:
                        layer = self._overlay._layer_by_id(sel_id)
                        if layer:
                            import copy as _copy
                            self._clipboard_layer = _copy.deepcopy(layer)
                    return True
                if key == Qt.Key_X:
                    sel_id = self._overlay._sel_id
                    if sel_id:
                        layer = self._overlay._layer_by_id(sel_id)
                        if layer:
                            import copy as _copy
                            self._clipboard_layer = _copy.deepcopy(layer)
                            self._overlay.delete_selected_layer()
                            self._save_current_layers()
                    return True
                if key == Qt.Key_V:
                    if self._clipboard_layer and self._doc:
                        import copy as _copy
                        new_layer = _copy.deepcopy(self._clipboard_layer)
                        new_layer["id"] = f"cut_{uuid.uuid4().hex[:8]}"
                        new_layer["spread"] = self._spread_idx
                        new_layer["z_index"] = len(self._overlay.current_layers())
                        new_layer.pop("mirror_id", None)  # will be assigned fresh below
                        orig_page = self._clipboard_layer.get("page")
                        if orig_page is not None:
                            pc = self._doc.page_count
                            orig_spread = self._clipboard_layer.get("spread", 0)
                            orig_left, orig_right = _pages_for_spread(orig_spread, pc)
                            cur_left, cur_right = _pages_for_spread(self._spread_idx, pc)
                            if orig_page == orig_left:
                                new_page = cur_left
                            else:
                                new_page = cur_right
                            if new_page is None:
                                new_page = cur_left if cur_left is not None else cur_right
                            new_layer["page"] = new_page
                        self._overlay._layers.append(new_layer)
                        mirror = self._overlay._create_mirror_for(new_layer)
                        self._overlay._sel_id = new_layer["id"]
                        self._overlay._sel_pt = None
                        self._overlay.update()
                        self._on_layer_committed(new_layer)
                        if mirror is not None:
                            self._on_layer_committed(mirror)
                    return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key  = event.key()
        mods = event.modifiers()

        if key == Qt.Key_Escape:
            if self._overlay._wip_points:
                self._overlay.cancel_wip()
            elif self._tool != _TOOL_NONE:
                self._set_tool(_TOOL_NONE)
            elif self._overlay._sel_id is not None:
                self._overlay._sel_id = None
                self._overlay._sel_pt = None
                self._overlay.update()
            return
        if key in (Qt.Key_Q, Qt.Key_W) and mods == Qt.ControlModifier:
            self.close()
            return
        if key in (Qt.Key_Delete, Qt.Key_Backspace):
            if self._overlay._sel_pt is not None:
                self._overlay.delete_selected_point()
            elif self._overlay._sel_id is not None:
                self._overlay.delete_selected_layer()
            self._save_current_layers()
            return

        super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_visualizer(project_path: str) -> None:
    """Launch the Book Visualizer window."""
    app = QApplication.instance() or QApplication(sys.argv)
    theme.apply_theme(app)
    window = BookVisualizerWindow(project_path)
    window.show()
    app.exec_()

