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
  Home/End          — previous/next Scope in the active browser tab
  PgUp/PgDn         — previous/next Field in the active browser tab
  Up/Down arrows    — previous/next Label in the active browser tab
  Escape / Ctrl+Q / Ctrl+W — close
"""

from __future__ import annotations

import datetime
import json
import math
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

_CLI_PATH = Path(__file__).parent.parent / "cli.py"

from styles import theme
from styles.theme import GripSplitter, save_window_geometry, restore_window_geometry

from PyQt5.QtCore import Qt, QByteArray, QEvent, pyqtSignal, QMimeData, QObject, QPoint, QRect, QRectF, QSize, QThread, QTimer, QPointF
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
    QSlider,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QDrag,
    QIcon,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QTransform,
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
_PANEL_WIDTH  = 270   # px — right panel preferred width

# Tool modes
_TOOL_NONE  = "none"
_TOOL_CUT   = "cut"
_TOOL_ERASE = "erase"
_TOOL_TEXT  = "text"

# Cut overlay visual constants
_HANDLE_R   = 4      # half-size of point handle square (px)
_CLOSE_DIST = 12     # px distance from first point to snap-close polygon
_HIT_DIST   = 8      # px distance for point / segment hit-testing

# Image layer visual constants
_IMG_CORNER_R   = 5     # half-size of corner resize handle (px)
_IMG_ROT_R      = 5     # radius of rotation handle circle (px)
_IMG_ROT_OFFSET = 22    # px above top-right corner for rotation handle
_IMG_DEFAULT_W  = 0.25  # default normalised width when dropped

# 15-color selection palette (cycled for successive text selections)
_SELECTION_COLORS = [
    "#ff00ff",  # fuchsia
    "#ffff00",  # yellow
    "#00ccff",  # sky blue
    "#00ff88",  # spring green
    "#ff6600",  # orange
    "#cc00ff",  # violet
    "#ff0066",  # rose
    "#00ffff",  # cyan
    "#ff3333",  # red
    "#33ff33",  # lime
    "#ff99cc",  # pink
    "#99ccff",  # periwinkle
    "#ffcc00",  # amber
    "#00ff44",  # mint
    "#aa88ff",  # lavender
]

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
        layers = data if isinstance(data, list) else []
    except Exception:
        return []

    # Back-fill line_weight on engraving layers that pre-date the field.
    # The generation JSON sidecar is the authoritative source.
    from data.book import book_dir
    eng_dir = book_dir(project_path, slug) / "engravings"
    for layer in layers:
        if layer.get("layer_subtype") == "Engraving" and "line_weight" not in layer:
            eng_id = layer.get("id", "")
            gen_json = eng_dir / f"{eng_id}_generation.json"
            try:
                meta = json.loads(gen_json.read_text(encoding="utf-8"))
                layer["line_weight"] = float(meta.get("line_weight") or 1.0)
            except Exception:
                layer["line_weight"] = 1.0

    return layers


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
# Text selection persistence helpers
# ---------------------------------------------------------------------------

def _text_sel_path(project_path: str, slug: str) -> Path:
    from data.book import book_dir
    return book_dir(project_path, slug) / "selections.json"


def _load_text_sels(project_path: str, slug: str) -> list:
    p = _text_sel_path(project_path, slug)
    if not p.exists():
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_text_sels(project_path: str, slug: str, sels: list) -> None:
    p = _text_sel_path(project_path, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(sels, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Mask persistence helpers
# ---------------------------------------------------------------------------

def _mask_path(project_path: str, slug: str) -> Path:
    from data.book import book_dir
    return book_dir(project_path, slug) / "mask.json"


def _load_mask(project_path: str, slug: str) -> set:
    """Return the set of page indices where the text mask is enabled."""
    p = _mask_path(project_path, slug)
    if not p.exists():
        return set()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return set(int(x) for x in data)
    except Exception:
        pass
    return set()


def _save_mask(project_path: str, slug: str, masked_pages: set) -> None:
    p = _mask_path(project_path, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(sorted(masked_pages), f, indent=2)


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
        self._book_dir: Optional[Path] = None   # for image layer compositing
        self._layers_visible: bool = True       # toggled by "Visible" checkbox
        self._drag_layer_id: Optional[str] = None  # excluded from baked composite during drag

        # Text-mask state  (set of page indices where mask is ON)
        self._masked_pages: set = set()
        self._text_sels_for_mask: list = []     # mirrors overlay text selections

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

    def set_layers_visible(self, visible: bool) -> None:
        """Show or hide all layer rendering (cuts + images) without losing data."""
        self._layers_visible = visible
        self._cache.clear()
        self._reveal_cache.clear()
        if self._doc is not None:
            self._do_render()

    def set_book_dir(self, book_dir: Optional[Path]) -> None:
        """Set the base directory used to resolve image layer sources."""
        self._book_dir = book_dir
        self._cache.clear()
        self._reveal_cache.clear()
        if self._doc is not None:
            self._do_render()

    def set_drag_layer_id(self, layer_id: Optional[str]) -> None:
        """Exclude *layer_id* from the baked composite while it is being dragged.

        The overlay draws the layer live during the drag; calling with ``None``
        re-includes all layers and re-bakes the spread.
        """
        if self._drag_layer_id == layer_id:
            return
        self._drag_layer_id = layer_id
        self._cache.clear()
        self._reveal_cache.clear()
        if self._doc is not None:
            self._do_render()

    def set_page_masked(self, page_idx: int, enabled: bool) -> None:
        """Enable or disable the text mask for a single page."""
        if enabled:
            self._masked_pages.add(page_idx)
        else:
            self._masked_pages.discard(page_idx)
        self._cache.clear()
        self._reveal_cache.clear()
        if self._doc is not None:
            self._do_render()

    def set_masked_pages(self, pages: set) -> None:
        """Replace the full masked-pages set (used when loading a book)."""
        self._masked_pages = set(pages)
        self._cache.clear()
        self._reveal_cache.clear()
        if self._doc is not None:
            self._do_render()

    def set_text_sels_for_mask(self, sels: list) -> None:
        """Update the text-selection list used by the mask compositor."""
        self._text_sels_for_mask = list(sels)
        if self._masked_pages:
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

    def _render_page_content(
        self,
        page_idx: int,
        cell_w: int,
        cell_h: int,
        layers_by_page: dict,
    ) -> Optional[QImage]:
        """Render PDF page and composite image layers on top.

        This is the single source of truth for what a page *looks like* before
        cut-reveal compositing.  The recursive reveal system calls this instead
        of _render_page so that illustrations on hidden pages show through cuts.
        """
        img = self._render_page(page_idx, cell_w, cell_h)
        if img is None:
            return None

        img_layers = [
            l for l in layers_by_page.get(page_idx, [])
            if l.get("type") == "Image" and l.get("source")
            and l.get("visible", True)
        ]
        # Apply text mask to the bare PDF layer before compositing illustrations,
        # so that illustrations always remain visible even on a masked page.
        if page_idx in self._masked_pages:
            img = self._apply_text_mask(img, page_idx)

        if not img_layers or self._book_dir is None:
            return img

        iw, ih = img.width(), img.height()
        result = img.copy()
        painter = QPainter(result)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.setRenderHint(QPainter.Antialiasing)

        for layer in img_layers:
            src_path = self._book_dir / layer["source"]
            if not src_path.exists():
                continue
            pix = QPixmap(str(src_path))
            if pix.isNull():
                continue
            cx  = layer["x"]      * iw
            cy  = layer["y"]      * ih
            sw  = layer["width"]  * iw
            sh  = layer["height"] * ih
            rot = layer.get("rotation", 0.0)
            painter.save()
            painter.translate(cx, cy)
            painter.rotate(rot)
            sx = -1.0 if layer.get("flip_h", False) else 1.0
            sy = -1.0 if layer.get("flip_v", False) else 1.0
            if sx != 1.0 or sy != 1.0:
                painter.scale(sx, sy)
            painter.drawImage(QRectF(-sw / 2, -sh / 2, sw, sh), pix.toImage())
            painter.restore()

        painter.end()
        return result

    # ------------------------------------------------------------------
    # Text-mask helpers

    def _sample_bg_color(self, img: QImage) -> QColor:
        """Sample background color from the four corners of a page image."""
        w, h = img.width(), img.height()
        if w < 4 or h < 4:
            return QColor(255, 255, 255)
        rs, gs, bs = [], [], []
        for x, y in ((2, 2), (w - 3, 2), (2, h - 3), (w - 3, h - 3)):
            c = img.pixelColor(x, y)
            rs.append(c.red()); gs.append(c.green()); bs.append(c.blue())
        return QColor(sum(rs) // 4, sum(gs) // 4, sum(bs) // 4)

    def _apply_text_mask(self, img: QImage, page_idx: int) -> QImage:
        """Return a copy of *img* with only text-selection regions visible.

        Everything outside the union of selection rects is filled with the
        sampled page background color.  A 2-px padding prevents edge clipping.
        """
        iw, ih = img.width(), img.height()
        bg = self._sample_bg_color(img)
        result = QImage(iw, ih, img.format())
        result.fill(bg)

        sels = [
            s for s in self._text_sels_for_mask
            if s.get("page") == page_idx and s.get("visible", True)
        ]
        if not sels:
            return result   # whole page blanked when no selections exist

        _PAD = 2
        painter = QPainter(result)
        for sel in sels:
            for nr in sel.get("rects", []):
                x0 = max(0.0, nr[0] * iw - _PAD)
                y0 = max(0.0, nr[1] * ih - _PAD)
                x1 = min(float(iw), nr[2] * iw + _PAD)
                y1 = min(float(ih), nr[3] * ih + _PAD)
                src = QRectF(x0, y0, x1 - x0, y1 - y0)
                painter.drawImage(src, img, src)
        painter.end()
        return result

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
        img = self._render_page_content(page_idx, cell_w, cell_h, layers_by_page)
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
        if behind_idx < 0 or behind_idx >= self._doc.page_count:
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
            self._debounce.start()  # retry once layout is complete
            return
        key = (self._slug, self._left_i, self._right_i, w, h)
        if key not in self._cache:
            cell_w, cell_h = self._cell_size()
            # Build page → layers lookup for reveal compositor
            layers_by_page: dict = {}
            if self._layers_visible:
                for layer in self._all_layers:
                    pi = layer.get("page")
                    if pi is not None and layer.get("id") != self._drag_layer_id:
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
        p.fillRect(self.rect(), QColor(theme.PANEL_BG))

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
    text_sel_committed = pyqtSignal(dict) # emitted when a text selection is created
    text_sel_removed   = pyqtSignal(str)  # emitted with text sel id on deletion
    engraving_requested       = pyqtSignal(dict)                          # emitted with Image layer dict
    silhouette_drop_requested = pyqtSignal(str, int, float, float, dict)  # abs_path, page_idx, nx, ny, meta

    @staticmethod
    def _make_cross_cursor(color: str, size: int = 21) -> QCursor:
        """Return a QCursor with a thin cross drawn in *color*."""
        pix = QPixmap(size, size)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(color), 1.5)
        p.setPen(pen)
        mid = size // 2
        gap = 3  # blank pixels around the centre point
        p.drawLine(0,   mid, mid - gap, mid)         # left arm
        p.drawLine(mid + gap, mid, size - 1, mid)    # right arm
        p.drawLine(mid, 0,   mid, mid - gap)         # top arm
        p.drawLine(mid, mid + gap, mid, size - 1)    # bottom arm
        p.end()
        return QCursor(pix, mid, mid)

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
        self._layers_visible: bool = True           # toggled by "Visible" checkbox

        # image layer interaction state
        self._img_drag_id:       Optional[str]    = None
        self._img_drag_start:    Optional[QPointF] = None
        self._img_drag_origin:   Optional[tuple]  = None   # (x, y, w, h, rot)
        self._img_resize_id:     Optional[str]    = None
        self._img_resize_corner: Optional[int]    = None   # 0=TL 1=TR 2=BR 3=BL
        self._img_resize_origin: Optional[tuple]  = None   # (x, y, w, h, rot, page_idx)
        self._img_rotate_id:     Optional[str]    = None
        self._img_rotate_center: Optional[tuple]  = None   # (cx_s, cy_s) screen center
        self._img_rotate_origin: Optional[float]  = None   # rotation angle at drag start
        self._img_rotate_start_pos: Optional[QPointF] = None
        self._book_dir:          Optional[Path]   = None
        self._pixmap_cache:      dict             = {}      # source_rel → QPixmap

        # Spinner animation state for generating layers
        self._spinner_angle: float = 0.0
        self._spinner_timer = QTimer(self)
        self._spinner_timer.setInterval(40)  # ~25 fps
        self._spinner_timer.timeout.connect(self._tick_spinner)

        # text selection state
        self._text_sels: list          = []               # committed text selection dicts
        self._text_sels_visible: bool  = True
        self._text_color_idx: int      = 0               # cycles through _SELECTION_COLORS
        self._text_drag_page: Optional[int]   = None     # page being selected
        self._text_drag_start_n: Optional[tuple] = None  # (nx, ny) drag origin (normalised)
        self._text_drag_end_n:   Optional[tuple] = None  # (nx, ny) drag current end

        # Page action buttons (hover/select on Image layers)
        self._hover_img_id: Optional[str] = None          # Image layer currently under cursor
        self._action_btn_rects: dict = {}                  # lid → {"engrave": QRect, "delete": QRect}
        self._action_icons_cache: Optional[dict] = None   # lazy-loaded icon pixmaps

        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.raise_()

    # ------------------------------------------------------------------
    # Public API

    def set_show_outlines(self, visible: bool) -> None:
        self._show_outlines = visible
        self.update()

    def set_layers_visible(self, visible: bool) -> None:
        self._layers_visible = visible
        self.update()

    def set_tool(self, tool: str) -> None:
        self._tool = tool
        if tool == _TOOL_CUT:
            self.setCursor(Qt.CrossCursor)
        elif tool == _TOOL_ERASE:
            self.setCursor(self._make_cross_cursor("#ff00ff"))   # fuchsia
        elif tool == _TOOL_TEXT:
            self.setCursor(self._make_cross_cursor("#00ffff"))   # cyan
        else:
            self.setCursor(Qt.ArrowCursor)
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hover_img_id is not None:
            self._hover_img_id = None
            self.update()
        super().leaveEvent(event)

    # ------------------------------------------------------------------
    # Page action buttons

    def _get_action_icons(self) -> dict:
        """Lazy-load and cache action button icon pixmaps."""
        if self._action_icons_cache is None:
            self._action_icons_cache = {
                "engrave": _svg_icon("media-image-plus",  14, "#ffffff"),
                "delete":  _svg_icon("trash-solid",       14, "#ffffff"),
                "flip_h":  _svg_icon("flip",              14, "#ffffff"),
                "flip_v":  _svg_icon("flip-reverse",      14, "#ffffff"),
            }
        return self._action_icons_cache

    def _draw_img_action_buttons(self, p: QPainter, layer: dict, rects: dict) -> None:
        """Draw [Delete] [Engrave] action buttons above the selected/hovered image.

        If there is not enough room above the image (buttons would be clipped),
        they are placed below the bottom edge instead.
        """
        page_idx = layer.get("page")
        r = rects.get(page_idx)
        if r is None:
            return
        corners = self._img_corners_screen(layer, r)
        min_x = int(min(c.x() for c in corners))
        min_y = int(min(c.y() for c in corners))
        max_y = int(max(c.y() for c in corners))
        sz, gap = 22, 4
        # Place above by default; fall back to below when too close to the top.
        if min_y - sz - 3 >= 0:
            btn_y = min_y - sz - 3
        else:
            btn_y = max_y + 3
        # Four buttons left-aligned: [delete] [flip_h] [flip_v] [engrave]
        del_rect = QRect(min_x,                  btn_y, sz, sz)
        fh_rect  = QRect(min_x +     (sz + gap), btn_y, sz, sz)
        fv_rect  = QRect(min_x + 2 * (sz + gap), btn_y, sz, sz)
        eng_rect = QRect(min_x + 3 * (sz + gap), btn_y, sz, sz)
        self._action_btn_rects[layer["id"]] = {
            "engrave": eng_rect, "delete": del_rect,
            "flip_h":  fh_rect,  "flip_v": fv_rect,
        }
        icons = self._get_action_icons()
        for btn_rect, icon_key, btn_color in [
            (del_rect, "delete",  QColor(160,  60,  60, 220)),
            (fh_rect,  "flip_h",  QColor( 60,  90,  90, 220)),
            (fv_rect,  "flip_v",  QColor( 60,  90,  90, 220)),
            (eng_rect, "engrave", QColor( 60,  60, 160, 220)),
        ]:
            p.setPen(Qt.NoPen)
            p.setBrush(btn_color)
            p.drawRoundedRect(btn_rect, 3, 3)
            icon_pix = icons[icon_key].pixmap(14, 14)
            if not icon_pix.isNull():
                if icon_key == "flip_v":
                    icon_pix = icon_pix.transformed(QTransform().rotate(90))
                ix = btn_rect.x() + (sz - icon_pix.width())  // 2
                iy = btn_rect.y() + (sz - icon_pix.height()) // 2
                p.drawPixmap(ix, iy, icon_pix)

    def _check_action_buttons(self, pos: QPointF) -> bool:
        """Return True and handle the action if *pos* hits an action button."""
        pt = pos.toPoint()
        for lid, btns in self._action_btn_rects.items():
            eng_rect = btns.get("engrave")
            del_rect = btns.get("delete")
            fh_rect  = btns.get("flip_h")
            fv_rect  = btns.get("flip_v")
            if eng_rect and eng_rect.contains(pt):
                layer = self._layer_by_id(lid)
                if layer:
                    self.engraving_requested.emit(dict(layer))
                return True
            if del_rect and del_rect.contains(pt):
                layer = self._layer_by_id(lid)
                if layer:
                    self._sel_id = lid
                    self._sel_pt = None
                    self.delete_selected_layer()
                return True
            if fh_rect and fh_rect.contains(pt):
                layer = self._layer_by_id(lid)
                if layer:
                    layer["flip_h"] = not layer.get("flip_h", False)
                    self.update()
                    self.layer_committed.emit(dict(layer))
                return True
            if fv_rect and fv_rect.contains(pt):
                layer = self._layer_by_id(lid)
                if layer:
                    layer["flip_v"] = not layer.get("flip_v", False)
                    self.update()
                    self.layer_committed.emit(dict(layer))
                return True
        return False

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
        self._img_drag_id = None
        self._img_drag_start = None
        self._img_drag_origin = None
        self._img_resize_id = None
        self._img_resize_corner = None
        self._img_resize_origin = None
        self._img_rotate_id = None
        self._img_rotate_center = None
        self._img_rotate_origin = None
        self._img_rotate_start_pos = None
        self.update()

    def current_layers(self) -> list:
        return list(self._layers)

    # ------------------------------------------------------------------
    # Spinner helpers for generating layers

    def _tick_spinner(self) -> None:
        """Advance spinner angle and repaint if any layer is still generating."""
        generating = any(l.get("generating") for l in self._layers)
        if not generating:
            self._spinner_timer.stop()
            return
        self._spinner_angle = (self._spinner_angle + 12) % 360
        self.update()

    def start_spinner_for(self, layer_id: str) -> None:
        """Mark *layer_id* as generating and start the spinner timer."""
        layer = self._layer_by_id(layer_id)
        if layer is not None:
            layer["generating"] = True
        if not self._spinner_timer.isActive():
            self._spinner_timer.start()

    def stop_spinner_for(self, layer_id: str) -> None:
        """Clear the generating flag for *layer_id*."""
        layer = self._layer_by_id(layer_id)
        if layer is not None:
            layer.pop("generating", None)
            layer.pop("generate_error", None)

    def mark_generate_error(self, layer_id: str) -> None:
        """Flag *layer_id* as having a generation error."""
        layer = self._layer_by_id(layer_id)
        if layer is not None:
            layer.pop("generating", None)
            layer["generate_error"] = True

    def set_text_selections(self, sels: list) -> None:
        """Replace text selections (e.g. on book switch)."""
        self._text_sels = list(sels)
        self._text_color_idx = len(sels)
        self._text_drag_page = None
        self._text_drag_start_n = None
        self._text_drag_end_n = None
        self.update()

    def set_text_sels_visible(self, visible: bool) -> None:
        self._text_sels_visible = visible
        self.update()

    def current_text_sels(self) -> list:
        return list(self._text_sels)

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
            mirror_id = layer.get("mirror_id")
            if mirror_id:
                mirror = self._layer_by_id(mirror_id)
                if mirror:
                    mirror["name"] = name
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
    # Book directory (for image asset resolution)

    def set_book_dir(self, book_dir: Optional[Path]) -> None:
        """Set the base directory for resolving image sources."""
        self._book_dir = book_dir
        self._pixmap_cache.clear()
        self.update()

    def _resolve_img_path(self, source_rel: str) -> Optional[Path]:
        if not source_rel:
            return None
        # Absolute paths (preprocessing / generation outputs) resolve directly.
        p_abs = Path(source_rel)
        if p_abs.is_absolute() and p_abs.exists():
            return p_abs
        if self._book_dir is None:
            return None
        p = self._book_dir / source_rel
        return p if p.exists() else None

    def _get_pixmap(self, source_rel: str) -> Optional[QPixmap]:
        """Load and cache a QPixmap by source_rel path."""
        if not source_rel:
            return None
        if source_rel in self._pixmap_cache:
            return self._pixmap_cache[source_rel]
        path = self._resolve_img_path(source_rel)
        pix = QPixmap(str(path)) if path else QPixmap()
        result = None if pix.isNull() else pix
        if len(self._pixmap_cache) > 60:
            evict = list(self._pixmap_cache)[:30]
            for k in evict:
                del self._pixmap_cache[k]
        self._pixmap_cache[source_rel] = result
        return result

    # ------------------------------------------------------------------
    # Image layer geometry helpers

    def _img_transform(self, layer: dict, r: QRect) -> tuple:
        """Return (cx_s, cy_s, sw_s, sh_s, rot_deg) in screen coordinates."""
        cx  = r.x() + layer["x"]      * r.width()
        cy  = r.y() + layer["y"]      * r.height()
        sw  = layer["width"]           * r.width()
        sh  = layer["height"]          * r.height()
        rot = layer.get("rotation", 0.0)
        return cx, cy, sw, sh, rot

    @staticmethod
    def _rot_pt(cx: float, cy: float, dx: float, dy: float, deg: float) -> QPointF:
        """Rotate offset (dx, dy) by *deg* degrees and translate by (cx, cy)."""
        rad   = math.radians(deg)
        cos_a = math.cos(rad)
        sin_a = math.sin(rad)
        return QPointF(cx + dx * cos_a - dy * sin_a,
                       cy + dx * sin_a + dy * cos_a)

    def _img_corners_screen(self, layer: dict, r: QRect) -> list:
        """Return [TL, TR, BR, BL] corner positions as QPointF in screen coords."""
        cx, cy, sw, sh, rot = self._img_transform(layer, r)
        hw, hh = sw / 2, sh / 2
        return [self._rot_pt(cx, cy, dx, dy, rot)
                for dx, dy in [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]]

    def _img_rot_handle_screen(self, layer: dict, r: QRect) -> QPointF:
        """Return screen position of the rotation handle (above TR corner)."""
        cx, cy, sw, sh, rot = self._img_transform(layer, r)
        return self._rot_pt(cx, cy, sw / 2, -sh / 2 - _IMG_ROT_OFFSET, rot)

    def _hit_test_image(self, pos: QPointF, layer: dict) -> bool:
        """True if *pos* is inside the (possibly rotated) image bounding box."""
        rects = self._visible_page_rects()
        r = rects.get(layer.get("page"))
        if r is None:
            return False
        cx, cy, sw, sh, rot = self._img_transform(layer, r)
        dx = pos.x() - cx
        dy = pos.y() - cy
        rad     = math.radians(-rot)
        local_x = dx * math.cos(rad) - dy * math.sin(rad)
        local_y = dx * math.sin(rad) + dy * math.cos(rad)
        return abs(local_x) <= sw / 2 + 2 and abs(local_y) <= sh / 2 + 2

    def _hit_image_body(self, pos: QPointF) -> Optional[str]:
        """Return id of topmost image layer whose body contains *pos*."""
        for layer in reversed(self._layers):
            if layer.get("type") != "Image":
                continue
            if self._hit_test_image(pos, layer):
                return layer["id"]
        return None

    def _hit_image_corner(self, pos: QPointF, lid: str) -> Optional[int]:
        """Return corner index (0=TL,1=TR,2=BR,3=BL) if pos is near a corner handle."""
        layer = self._layer_by_id(lid)
        if layer is None or layer.get("type") != "Image":
            return None
        rects = self._visible_page_rects()
        r = rects.get(layer.get("page"))
        if r is None:
            return None
        for i, c in enumerate(self._img_corners_screen(layer, r)):
            if (pos - c).manhattanLength() <= (_IMG_CORNER_R + 4) * 2:
                return i
        return None

    def _hit_image_rot_handle(self, pos: QPointF, lid: str) -> bool:
        """True if *pos* is near the rotation handle of image layer *lid*."""
        layer = self._layer_by_id(lid)
        if layer is None or layer.get("type") != "Image":
            return False
        rects = self._visible_page_rects()
        r = rects.get(layer.get("page"))
        if r is None:
            return False
        rh = self._img_rot_handle_screen(layer, r)
        return (pos - rh).manhattanLength() <= (_IMG_ROT_R + 4) * 2

    # ------------------------------------------------------------------
    # Image layer drag / resize / rotate

    def _img_drag_move(self, pos: QPointF) -> None:
        """Translate the selected image to follow cursor."""
        if self._img_drag_id is None or self._img_drag_start is None:
            return
        layer = self._layer_by_id(self._img_drag_id)
        if layer is None:
            return
        rects = self._visible_page_rects()
        r = rects.get(layer.get("page"))
        if r is None or r.width() <= 0 or r.height() <= 0:
            return
        ox, oy, _ow, _oh, _orot = self._img_drag_origin
        dx = (pos.x() - self._img_drag_start.x()) / r.width()
        dy = (pos.y() - self._img_drag_start.y()) / r.height()
        layer["x"] = ox + dx
        layer["y"] = oy + dy

        # If the image centre has crossed onto the other visible page, remap
        # layer["page"] and re-express the coordinates in that page's space.
        cx_s = r.x() + layer["x"] * r.width()
        cy_s = r.y() + layer["y"] * r.height()
        for other_idx, other_r in rects.items():
            if other_idx == layer["page"]:
                continue
            if other_r.width() > 0 and other_r.height() > 0 and \
                    other_r.contains(int(cx_s), int(cy_s)):
                layer["x"] = (cx_s - other_r.x()) / other_r.width()
                layer["y"] = (cy_s - other_r.y()) / other_r.height()
                layer["page"] = other_idx
                layer["spread"] = _spread_for_page(other_idx)
                # Reset reference so subsequent delta uses the new page's space
                self._img_drag_start = pos
                self._img_drag_origin = (layer["x"], layer["y"], _ow, _oh, _orot)
                break

    def _img_resize_move(self, pos: QPointF) -> None:
        """Proportionally resize the image as the user drags a corner handle."""
        if self._img_resize_id is None or self._img_resize_origin is None:
            return
        layer = self._layer_by_id(self._img_resize_id)
        if layer is None:
            return
        ox, oy, ow, oh, orot, page_idx = self._img_resize_origin
        rects = self._visible_page_rects()
        r = rects.get(page_idx)
        if r is None:
            return
        cx = r.x() + ox * r.width()
        cy = r.y() + oy * r.height()
        dx = pos.x() - cx
        dy = pos.y() - cy
        rad     = math.radians(-orot)
        local_x = dx * math.cos(rad) - dy * math.sin(rad)
        local_y = dx * math.sin(rad) + dy * math.cos(rad)
        orig_hw  = ow * r.width()  / 2
        orig_hh  = oh * r.height() / 2
        orig_dist = math.hypot(orig_hw, orig_hh)
        if orig_dist == 0:
            return
        new_dist = math.hypot(abs(local_x), abs(local_y))
        scale = max(0.02, new_dist / orig_dist)
        layer["width"]  = ow * scale
        layer["height"] = oh * scale

    def _img_rotate_move(self, pos: QPointF) -> None:
        """Rotate the image as the user drags the rotation handle."""
        if self._img_rotate_id is None or self._img_rotate_center is None:
            return
        layer = self._layer_by_id(self._img_rotate_id)
        if layer is None:
            return
        cx, cy = self._img_rotate_center
        start_angle = math.degrees(math.atan2(
            self._img_rotate_start_pos.y() - cy,
            self._img_rotate_start_pos.x() - cx,
        ))
        cur_angle = math.degrees(math.atan2(pos.y() - cy, pos.x() - cx))
        delta = cur_angle - start_angle
        rotation = (self._img_rotate_origin + delta) % 360
        if QApplication.keyboardModifiers() & Qt.ShiftModifier:
            rotation = round(rotation / 30) * 30 % 360
        layer["rotation"] = rotation

    # ------------------------------------------------------------------
    # Drop-from-illustrations handler

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat("application/x-crossing-illus-source"):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat("application/x-crossing-illus-source"):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        mime = event.mimeData()
        if not mime.hasFormat("application/x-crossing-illus-source"):
            event.ignore()
            return
        raw = bytes(mime.data("application/x-crossing-illus-source")).decode("utf-8")
        try:
            payload  = json.loads(raw)
            abs_path = payload.get("abs_path", "")
            meta     = payload.get("meta", {})
        except (json.JSONDecodeError, ValueError):
            abs_path = ""
            meta     = {}
        if not abs_path or not Path(abs_path).exists():
            event.ignore()
            return
        pos = QPointF(event.pos())
        hit = self._which_page(pos.x(), pos.y())
        if hit is None:
            event.ignore()
            return
        page_idx, nx, ny = hit
        self.silhouette_drop_requested.emit(abs_path, page_idx, nx, ny, meta)
        event.acceptProposedAction()

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
        if "geometry" not in layer:
            return []
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
            if layer.get("type") == "Image":
                continue
            pts = self._layer_screen_pts(layer)
            for i, pt in enumerate(pts):
                if abs(pos.x() - pt.x()) <= _HIT_DIST and abs(pos.y() - pt.y()) <= _HIT_DIST:
                    return layer["id"], i
        return None

    def _hit_segment(self, pos: QPointF) -> Optional[tuple]:
        """Return (layer_id, seg_idx, insertion_point) if pos is near a segment."""
        for layer in reversed(self._layers):
            if layer.get("type") == "Image":
                continue
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
            if layer.get("type") == "Image":
                continue
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
        # Action buttons take priority over everything (select tool only)
        if self._tool == _TOOL_NONE and self._check_action_buttons(pos):
            return
        if self._tool == _TOOL_CUT:
            self._cut_press(pos)
        elif self._tool == _TOOL_ERASE:
            self._erase_press(pos)
        elif self._tool == _TOOL_TEXT:
            self._text_press(pos)
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
        if self._tool == _TOOL_TEXT:
            # Double-click in text tool: expand to word under cursor
            pos = QPointF(event.pos())
            hit = self._which_page(pos.x(), pos.y())
            if hit is None:
                return
            page_idx, nx, ny = hit
            doc = getattr(self._view, "_doc", None)
            if doc is None:
                return
            try:
                page = doc[page_idx]
            except Exception:
                return
            try:
                import fitz
            except Exception:
                return
            pw = page.rect.width
            ph = page.rect.height
            px = nx * pw
            py = ny * ph

            raw = page.get_text("rawdict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
            # Group chars by logical line to avoid expanding across lines
            lines_chars = []  # list of lists of (bbox, char)
            for block in raw.get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    line_chars: list = []
                    for span in line.get("spans", []):
                        for ch in span.get("chars", []):
                            bbox = ch.get("bbox")
                            if not bbox or len(bbox) < 4:
                                continue
                            c = ch.get("c", "")
                            line_chars.append((bbox, c))
                    if line_chars:
                        # ensure left-to-right order
                        line_chars.sort(key=lambda bc: bc[0][0])
                        lines_chars.append(line_chars)

            if not lines_chars:
                return

            # Find the line and char index nearest to the click, preferring
            # a char whose bbox contains the point.  Fallback chooses the
            # line whose vertical centre is closest to the click, then the
            # nearest char within that line (by horizontal distance).
            found_line = None
            found_idx = None
            for li, lchars in enumerate(lines_chars):
                for ci, (bbox, ch) in enumerate(lchars):
                    x0, y0, x1, y1 = bbox
                    if x0 <= px <= x1 and y0 <= py <= y1:
                        found_line = li
                        found_idx = ci
                        break
                if found_line is not None:
                    break

            if found_line is None:
                # pick line by vertical proximity
                def line_vcentre(li):
                    lchars = lines_chars[li]
                    y0s = [b[1] for b, _ in lchars]
                    y1s = [b[3] for b, _ in lchars]
                    return (min(y0s) + max(y1s)) / 2.0

                chosen_line = None
                # lines that vertically contain the point
                containing = [i for i in range(len(lines_chars)) if min(b[1] for b, _ in lines_chars[i]) <= py <= max(b[3] for b, _ in lines_chars[i])]
                if containing:
                    chosen_line = containing[0]
                else:
                    chosen_line = min(range(len(lines_chars)), key=lambda i: abs(line_vcentre(i) - py))

                # nearest char in chosen line by horizontal centre distance
                best_ci = None
                best_dx = None
                for ci, (bbox, ch) in enumerate(lines_chars[chosen_line]):
                    cx = (bbox[0] + bbox[2]) / 2.0
                    dx = abs(cx - px)
                    if best_ci is None or dx < best_dx:
                        best_ci = ci
                        best_dx = dx
                found_line = chosen_line
                found_idx = best_ci

            if found_line is None or found_idx is None:
                return

            # Expand to word within the same line only (exclude spaces, punctuation,
            # parentheses, various quotation marks, colons/semicolons, and dashes)
            delims = {
                " ", "\t", "\n", ",", ".",
                ":", ";",
                "(", ")",
                '"', "'",
                "\u201C", "\u201D",  # “ ”
                "\u2018", "\u2019",  # ‘ ’
                "\u00AB", "\u00BB",  # « »
                "\u2039", "\u203A",  # ‹ ›
                "\u2013", "\u2014",  # – — (en dash, em dash)
            }
            lchars = lines_chars[found_line]
            # If clicked char is a delimiter, search within the same line.
            # Also treat a double-hyphen ("--") sequence as a delimiter when
            # the clicked char is '-' adjacent to another '-'.
            cur_ch = lchars[found_idx][1]
            is_delim = cur_ch in delims
            if not is_delim and cur_ch == '-':
                # check adjacent chars for double-hyphen
                if (found_idx + 1 < len(lchars) and lchars[found_idx + 1][1] == '-') or (
                    found_idx - 1 >= 0 and lchars[found_idx - 1][1] == '-'
                ):
                    is_delim = True

            if is_delim:
                left = found_idx - 1
                right = found_idx + 1
                found = None
                while left >= 0 or right < len(lchars):
                    if left >= 0 and lchars[left][1] not in delims and lchars[left][1] != '-':
                        found = left
                        break
                    if right < len(lchars) and lchars[right][1] not in delims and lchars[right][1] != '-':
                        found = right
                        break
                    left -= 1
                    right += 1
                if found is None:
                    return
                found_idx = found

            # expand left/right but keep within line bounds
            li = found_idx
            ll = li
            while ll - 1 >= 0 and lchars[ll - 1][1] not in delims:
                ll -= 1
            rr = li
            while rr + 1 < len(lchars) and lchars[rr + 1][1] not in delims:
                rr += 1

            sel_chars = lchars[ll: rr + 1]
            if not sel_chars:
                return

            x0 = min(b[0] for b, _ in sel_chars)
            y0 = min(b[1] for b, _ in sel_chars)
            x1 = max(b[2] for b, _ in sel_chars)
            y1 = max(b[3] for b, _ in sel_chars)

            x0n = x0 / pw
            y0n = y0 / ph
            x1n = x1 / pw
            y1n = y1 / ph

            text = "".join(c for _, c in sel_chars)
            spread_idx = getattr(self._view, "_spread_idx", 0)
            color = _SELECTION_COLORS[self._text_color_idx % len(_SELECTION_COLORS)]
            self._text_color_idx += 1
            sel = {
                "id": f"text_{uuid.uuid4().hex[:8]}",
                "type": "TextSelection",
                "name": "Text",
                "page": page_idx,
                "spread": spread_idx,
                "text": text,
                "rects": [[x0n, y0n, x1n, y1n]],
                "color": color,
                "visible": True,
            }
            self._text_sels.append(sel)
            self.update()
            self.text_sel_committed.emit(sel)
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
        if self._img_rotate_id is not None:
            self._img_rotate_move(self._mouse_pos)
        elif self._img_resize_id is not None:
            self._img_resize_move(self._mouse_pos)
        elif self._img_drag_id is not None:
            self._img_drag_move(self._mouse_pos)
        elif self._pt_drag_id is not None:
            self._pt_drag_move(self._mouse_pos)
        elif self._drag_id is not None:
            self._drag_move(self._mouse_pos)
        elif self._text_drag_page is not None:
            self._text_drag_move(self._mouse_pos)
        else:
            self._update_hover(self._mouse_pos)
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            if self._text_drag_page is not None:
                self._text_release()
            elif self._img_rotate_id is not None:
                self._img_rotate_id = None
                self._img_rotate_center = None
                self._img_rotate_origin = None
                self._img_rotate_start_pos = None
                self.layer_committed.emit({})
            elif self._img_resize_id is not None:
                self._img_resize_id = None
                self._img_resize_corner = None
                self._img_resize_origin = None
                self.layer_committed.emit({})
            elif self._img_drag_id is not None:
                self._img_drag_id = None
                self._img_drag_start = None
                self._img_drag_origin = None
                self._view.set_drag_layer_id(None)
                self.layer_committed.emit({})
            elif self._pt_drag_id is not None:
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
        # Then cut polygon
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
            return
        # Then image layer
        img_lid = self._hit_image_body(pos)
        if img_lid:
            layer = self._layer_by_id(img_lid)
            if layer:
                self._layers.remove(layer)
                if self._sel_id == img_lid:
                    self._sel_id = None
                    self._sel_pt = None
                self.update()
                self.layer_removed.emit(img_lid)
            return
        # Then text selection
        tid = self._hit_text_sel(pos)
        if tid:
            sel = self._text_sel_by_id(tid)
            if sel:
                self._text_sels.remove(sel)
                self.update()
                self.text_sel_removed.emit(tid)

    # ------------------------------------------------------------------
    # TEXT selection tool

    def _text_press(self, pos: QPointF) -> None:
        hit = self._which_page(pos.x(), pos.y())
        if hit is None:
            return
        page_idx, nx, ny = hit
        self._text_drag_page = page_idx
        self._text_drag_start_n = (nx, ny)
        self._text_drag_end_n = (nx, ny)

    def _text_drag_move(self, pos: QPointF) -> None:
        if self._text_drag_page is None:
            return
        r = self._visible_page_rects().get(self._text_drag_page)
        if r is None or r.width() <= 0 or r.height() <= 0:
            return
        nx = max(0.0, min(1.0, (pos.x() - r.x()) / r.width()))
        ny = max(0.0, min(1.0, (pos.y() - r.y()) / r.height()))
        self._text_drag_end_n = (nx, ny)

    def _text_release(self) -> None:
        if (self._text_drag_page is None
                or self._text_drag_start_n is None
                or self._text_drag_end_n is None):
            self._text_drag_page = None
            self._text_drag_start_n = None
            self._text_drag_end_n = None
            return
        page_idx = self._text_drag_page
        x0n = min(self._text_drag_start_n[0], self._text_drag_end_n[0])
        y0n = min(self._text_drag_start_n[1], self._text_drag_end_n[1])
        x1n = max(self._text_drag_start_n[0], self._text_drag_end_n[0])
        y1n = max(self._text_drag_start_n[1], self._text_drag_end_n[1])
        self._text_drag_page = None
        self._text_drag_start_n = None
        self._text_drag_end_n = None
        self.update()
        # Skip tiny / accidental drags
        if (x1n - x0n) < 0.002 and (y1n - y0n) < 0.002:
            return
        doc = self._view._doc
        if doc is None:
            return
        try:
            page = doc[page_idx]
        except Exception:
            return
        pw = page.rect.width
        ph = page.rect.height
        import fitz
        sel_rect = fitz.Rect(x0n * pw, y0n * ph, x1n * pw, y1n * ph)
        # Character-level hit testing: collect individual char bboxes that
        # physically intersect the drag rect, then group them by span.
        raw = page.get_text("rawdict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        hit_rects = []
        hit_text_parts = []
        for block in raw.get("blocks", []):
            if block.get("type") != 0:  # 0 = text block
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    all_chars = span.get("chars", [])
                    hit_indices = [
                        i for i, ch in enumerate(all_chars)
                        if sel_rect.intersects(fitz.Rect(ch["bbox"]))
                    ]
                    if not hit_indices:
                        continue
                    span_chars = all_chars[hit_indices[0]:hit_indices[-1] + 1]
                    bx0 = min(c["bbox"][0] for c in span_chars)
                    by0 = min(c["bbox"][1] for c in span_chars)
                    bx1 = max(c["bbox"][2] for c in span_chars)
                    by1 = max(c["bbox"][3] for c in span_chars)
                    hit_rects.append([bx0 / pw, by0 / ph, bx1 / pw, by1 / ph])
                    hit_text_parts.append("".join(c["c"] for c in span_chars))
        if not hit_rects:
            return
        # Merge per-word rects into per-line spanning rects so inter-word gaps
        # are filled in.  Group by (y0, y1) bucket then span x0→x1.
        line_map: dict = {}
        for (rx0, ry0, rx1, ry1) in hit_rects:
            key = (round(ry0, 4), round(ry1, 4))
            if key in line_map:
                line_map[key][0] = min(line_map[key][0], rx0)
                line_map[key][1] = max(line_map[key][1], rx1)
            else:
                line_map[key] = [rx0, rx1]
        hit_rects = [
            [v[0], k[0], v[1], k[1]]
            for k, v in sorted(line_map.items())
        ]
        spread_idx = getattr(self._view, "_spread_idx", 0)
        color = _SELECTION_COLORS[self._text_color_idx % len(_SELECTION_COLORS)]
        self._text_color_idx += 1
        sel = {
            "id":     f"text_{uuid.uuid4().hex[:8]}",
            "type":   "TextSelection",
            "name":   "Text",
            "page":   page_idx,
            "spread": spread_idx,
            "text":   " ".join(hit_text_parts),
            "rects":  hit_rects,
            "color":  color,
            "visible": True,
        }
        self._text_sels.append(sel)
        self.update()
        self.text_sel_committed.emit(sel)

    def _hit_text_sel(self, pos: QPointF) -> Optional[str]:
        """Return id of topmost text selection that contains pos, or None."""
        rects = self._visible_page_rects()
        for sel in reversed(self._text_sels):
            if not sel.get("visible", True):
                continue
            page_idx = sel.get("page")
            r = rects.get(page_idx)
            if r is None:
                continue
            for nr in sel.get("rects", []):
                x0 = r.x() + nr[0] * r.width()
                y0 = r.y() + nr[1] * r.height()
                x1 = r.x() + nr[2] * r.width()
                y1 = r.y() + nr[3] * r.height()
                if x0 <= pos.x() <= x1 and y0 <= pos.y() <= y1:
                    return sel["id"]
        return None

    def _text_sel_by_id(self, sid: str) -> Optional[dict]:
        for s in self._text_sels:
            if s["id"] == sid:
                return s
        return None

    # ------------------------------------------------------------------
    # SELECT / DRAG tool (tool == _TOOL_NONE)

    def _select_press(self, pos: QPointF, event) -> None:
        # ── Selected image: rotation/resize/body handles take priority ─
        if self._sel_id:
            sel_layer = self._layer_by_id(self._sel_id)
            if sel_layer and sel_layer.get("type") == "Image":
                rects = self._visible_page_rects()
                r = rects.get(sel_layer.get("page"))
                if r:
                    if self._hit_image_rot_handle(pos, self._sel_id):
                        cx, cy, sw, sh, rot = self._img_transform(sel_layer, r)
                        self._img_rotate_id = self._sel_id
                        self._img_rotate_center = (cx, cy)
                        self._img_rotate_origin = rot
                        self._img_rotate_start_pos = pos
                        self.update()
                        return
                    corner = self._hit_image_corner(pos, self._sel_id)
                    if corner is not None:
                        self._img_resize_id = self._sel_id
                        self._img_resize_corner = corner
                        self._img_resize_origin = (
                            sel_layer["x"], sel_layer["y"],
                            sel_layer["width"], sel_layer["height"],
                            sel_layer.get("rotation", 0.0),
                            sel_layer.get("page"),
                        )
                        self.update()
                        return
                    if self._hit_test_image(pos, sel_layer):
                        self._img_drag_id = self._sel_id
                        self._img_drag_start = pos
                        self._img_drag_origin = (
                            sel_layer["x"], sel_layer["y"],
                            sel_layer["width"], sel_layer["height"],
                            sel_layer.get("rotation", 0.0),
                        )
                        self._view.set_drag_layer_id(self._sel_id)
                        self.update()
                        return

        # ── Any image body ─────────────────────────────────────────────
        img_lid = self._hit_image_body(pos)
        if img_lid:
            self._sel_id = img_lid
            self._sel_pt = None
            img_layer = self._layer_by_id(img_lid)
            self.selection_changed.emit(img_lid)
            if img_layer:
                self._img_drag_id = img_lid
                self._img_drag_start = pos
                self._img_drag_origin = (
                    img_layer["x"], img_layer["y"],
                    img_layer["width"], img_layer["height"],
                    img_layer.get("rotation", 0.0),
                )
                self._view.set_drag_layer_id(img_lid)
            self.update()
            return

        # ── Cut point handle? ──────────────────────────────────────────
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
        # Track which Image layer is under the cursor (select-tool only)
        if self._tool == _TOOL_NONE:
            new_hov = self._hit_image_body(pos)
            if new_hov != self._hover_img_id:
                self._hover_img_id = new_hov
        else:
            self._hover_img_id = None

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
        has_text = self._text_sels_visible and (
            bool(self._text_sels) or self._text_drag_page is not None
        )
        if not self._layers_visible and not has_text:
            return

        rects = self._visible_page_rects()

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        CUT_COLOR  = QColor("#e6e6e6")           # 90% grey for unselected
        SEL_COLOR  = QColor(theme.ACCENT)        # fuchsia for selected
        FILL_ALPHA = QColor(230, 230, 230, 12)   # very subtle light fill
        SEL_FILL   = QColor(255, 0, 255, 40)
        hr = _HANDLE_R - 1                       # slightly smaller handles

        # ── text selections ───────────────────────────────────────────────
        if self._text_sels_visible:
            p.setPen(Qt.NoPen)
            for sel in self._text_sels:
                if not sel.get("visible", True):
                    continue
                page_idx = sel.get("page")
                r = rects.get(page_idx)
                if r is None:
                    continue
                base = QColor(sel.get("color", "#ff00ff"))
                base.setAlpha(55)
                p.setBrush(base)
                for nr in sel.get("rects", []):
                    x0 = r.x() + nr[0] * r.width()
                    y0 = r.y() + nr[1] * r.height()
                    x1 = r.x() + nr[2] * r.width()
                    y1 = r.y() + nr[3] * r.height()
                    p.drawRect(QRectF(x0, y0, x1 - x0, y1 - y0))

        # ── text selection drag preview ────────────────────────────────
        if (self._text_drag_page is not None
                and self._text_drag_start_n is not None
                and self._text_drag_end_n is not None):
            r = rects.get(self._text_drag_page)
            if r is not None:
                x0 = r.x() + self._text_drag_start_n[0] * r.width()
                y0 = r.y() + self._text_drag_start_n[1] * r.height()
                x1 = r.x() + self._text_drag_end_n[0] * r.width()
                y1 = r.y() + self._text_drag_end_n[1] * r.height()
                drag_rect = QRectF(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))
                p.setPen(QPen(QColor(255, 0, 255, 160), 1.0, Qt.DashLine))
                p.setBrush(QColor(255, 0, 255, 20))
                p.drawRect(drag_rect)

        if not self._layers_visible:
            p.end()
            return

        # ── committed cut layers ───────────────────────────────────────
        for layer in self._layers:
            if layer.get("type") == "Image":
                continue                          # drawn below after cut outlines
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

        # ── image layer bitmaps ───────────────────────────────────────
        for layer in self._layers:
            if layer.get("type") != "Image":
                continue
            if not layer.get("visible", True):
                continue
            page_idx = layer.get("page")
            r = rects.get(page_idx)
            if r is None:
                continue
            cx, cy, sw, sh, rot = self._img_transform(layer, r)
            if sw < 1 or sh < 1:
                continue

            is_generating = layer.get("generating", False)
            is_error      = layer.get("generate_error", False)

            # Resolve display source:
            # 1. output_png (final binary engraving) if present
            # 2. source (relative, falls back to preprocessing placeholder)
            # 3. preprocessing_path (absolute)
            display_src = (
                layer.get("output_png", "") or
                layer.get("source", "") or
                layer.get("preprocessing_path", "")
            )
            pix = self._get_pixmap(display_src) if display_src else None

            p.save()
            p.translate(cx, cy)
            p.rotate(rot)
            sx = -1.0 if layer.get("flip_h", False) else 1.0
            sy = -1.0 if layer.get("flip_v", False) else 1.0
            if sx != 1.0 or sy != 1.0:
                p.scale(sx, sy)

            if is_error:
                # Error state: warm red tinted placeholder
                p.setPen(QPen(QColor("#cc4444"), 1, Qt.DashLine))
                p.setBrush(QColor(100, 40, 40, 80))
                p.drawRect(QRectF(-sw / 2, -sh / 2, sw, sh))
            elif is_generating:
                # Generating state: fuchsia placeholder + spinner arc
                p.setPen(QPen(QColor("#ff00ff"), 1, Qt.DashLine))
                p.setBrush(QColor(120, 0, 120, 60))
                p.drawRect(QRectF(-sw / 2, -sh / 2, sw, sh))
                # Spinner arc (drawn in unscaled/unflipped space)
                r_spin = min(sw, sh) * 0.18
                r_spin = max(8.0, min(32.0, r_spin))
                arc_rect = QRectF(-r_spin, -r_spin, r_spin * 2, r_spin * 2)
                spin_pen = QPen(QColor("#ff00ff"), max(2.0, r_spin * 0.18))
                spin_pen.setCapStyle(Qt.RoundCap)
                p.setPen(spin_pen)
                p.setBrush(Qt.NoBrush)
                start_angle = int((90 - self._spinner_angle) * 16)
                p.drawArc(arc_rect, start_angle, 120 * 16)
            elif pix and not pix.isNull():
                p.drawImage(QRectF(-sw / 2, -sh / 2, sw, sh), pix.toImage())
            else:
                # placeholder when image file is missing
                p.setPen(QPen(QColor("#888888"), 1, Qt.DashLine))
                p.setBrush(QColor(80, 80, 80, 60))
                p.drawRect(QRectF(-sw / 2, -sh / 2, sw, sh))
            p.restore()

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

        # ── image selection handles ───────────────────────────────────
        if self._sel_id:
            sel_layer = self._layer_by_id(self._sel_id)
            if sel_layer and sel_layer.get("type") == "Image":
                r = rects.get(sel_layer.get("page"))
                if r:
                    cx, cy, sw, sh, rot = self._img_transform(sel_layer, r)
                    corners = self._img_corners_screen(sel_layer, r)
                    rot_h   = self._img_rot_handle_screen(sel_layer, r)
                    # Bounding box (drawn in image-local space so it aligns with rotation)
                    p.save()
                    p.translate(cx, cy)
                    p.rotate(rot)
                    p.setPen(QPen(SEL_COLOR, 1.5))
                    p.setBrush(Qt.NoBrush)
                    p.drawRect(QRectF(-sw / 2, -sh / 2, sw, sh))
                    p.restore()
                    # Corner resize handles
                    p.setPen(QPen(SEL_COLOR, 1.5))
                    p.setBrush(SEL_COLOR)
                    for c in corners:
                        p.drawRect(
                            int(c.x()) - _IMG_CORNER_R, int(c.y()) - _IMG_CORNER_R,
                            _IMG_CORNER_R * 2, _IMG_CORNER_R * 2,
                        )
                    # Line from TR corner to rotation handle
                    tr = corners[1]
                    p.setPen(QPen(SEL_COLOR, 1.0))
                    p.drawLine(tr, rot_h)
                    # Rotation handle circle
                    p.setPen(QPen(SEL_COLOR, 1.5))
                    p.setBrush(Qt.NoBrush)
                    p.drawEllipse(rot_h, float(_IMG_ROT_R), float(_IMG_ROT_R))

        # ── image action buttons (hover / selected) ──────────────────────
        self._action_btn_rects.clear()
        no_drag = (
            self._img_drag_id   is None
            and self._img_resize_id is None
            and self._img_rotate_id is None
        )
        if no_drag and self._layers_visible:
            for layer in self._layers:
                if layer.get("type") != "Image":
                    continue
                if not layer.get("visible", True):
                    continue
                lid = layer["id"]
                if lid == self._sel_id or lid == self._hover_img_id:
                    # Don't offer to engrave an engraving
                    if layer.get("layer_subtype") != "Engraving":
                        self._draw_img_action_buttons(p, layer, rects)

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
        p.fillRect(self.rect(), QColor(theme.PANEL_BG))
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

    delete_requested     = pyqtSignal(str)        # layer id
    rename_requested     = pyqtSignal(str, str)   # layer id, new name
    selected             = pyqtSignal(str)         # layer id
    visibility_toggled   = pyqtSignal(str, bool)  # layer id, new visible state

    def __init__(self, layer: dict, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._lid  = layer["id"]
        self._name = layer.get("name", "Cut")
        self._layer_type = layer.get("type", "Cut")
        self._visible = layer.get("visible", True)
        self._editing = False

        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(4)

        # -- type icon --
        _LAYER_ICONS = {"Cut": "cut", "Image": "journal-page", "Engraving": "plus-circle-solid"}
        _display_key = layer.get("layer_subtype") or self._layer_type
        icon_name = _LAYER_ICONS.get(_display_key) or _LAYER_ICONS.get(self._layer_type)
        if icon_name:
            self._type_icon = QLabel()
            self._type_icon.setPixmap(_svg_icon(icon_name, 12, theme.TEXT_DIM).pixmap(12, 12))
            self._type_icon.setFixedSize(14, 14)
            self._type_icon.setAlignment(Qt.AlignCenter)
            lay.addWidget(self._type_icon)
            lay.addSpacing(2)

        # -- name label (normal state) --
        self._name_label = QLabel(self._name)
        self._name_label.setStyleSheet(
            f"color: {theme.TEXT}; font-family: '{theme.FAMILY_UI}';"
            f" font-size: {theme.BASE_PT}pt;"
        )
        self._name_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._name_label.setMaximumWidth(120)

        # -- inline editor (edit state) --
        self._name_edit = QLineEdit(self._name)
        self._name_edit.setStyleSheet(
            f"QLineEdit {{ color: {theme.TEXT}; background: {theme.INPUT_BG};"
            f" font-family: '{theme.FAMILY_UI}'; font-size: {theme.BASE_PT}pt;"
            f" border: 1px solid {theme.ACCENT}; border-radius: 2px; padding: 0 2px;"
            f" selection-background-color: {theme.ACCENT}; selection-color: {theme.TEXT}; }}"
        )
        self._name_edit.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._name_edit.setMaximumWidth(120)
        self._name_edit.hide()
        self._name_edit.returnPressed.connect(self._commit_rename)
        self._name_edit.editingFinished.connect(self._commit_rename)

        lay.addWidget(self._name_label)
        lay.addWidget(self._name_edit)

        # -- eye (visibility) button --
        self._eye_btn = QPushButton()
        self._eye_btn.setIcon(_svg_icon(
            "eye-solid" if self._visible else "eye-closed", 12,
            theme.TEXT_DIM if self._visible else theme.ACCENT
        ))
        self._eye_btn.setIconSize(QSize(12, 12))
        self._eye_btn.setFixedSize(18, 18)
        self._eye_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; }"
        )
        self._eye_btn.setToolTip("Toggle layer visibility")
        self._eye_btn.clicked.connect(self._on_eye_clicked)
        lay.addWidget(self._eye_btn)

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

    def _on_eye_clicked(self) -> None:
        self._visible = not self._visible
        self._eye_btn.setIcon(_svg_icon(
            "eye-solid" if self._visible else "eye-closed", 12,
            theme.TEXT_DIM if self._visible else theme.ACCENT
        ))
        # Refresh italic/dim style without changing selection state
        self.set_selected(self._lid == getattr(self.parent(), "_sel_id", None))
        self.visibility_toggled.emit(self._lid, self._visible)

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
        font_style = "normal" if self._visible else "italic"
        self._name_label.setStyleSheet(
            f"color: {text}; font-family: '{theme.FAMILY_UI}';"
            f" font-size: {theme.BASE_PT}pt; font-style: {font_style};"
            f" background: transparent;"
        )
        self.setStyleSheet(
            f"_LayerRow {{ background: {bg}; border-radius: 3px; }}"
        )
        if hasattr(self, "_type_icon"):
            self._type_icon.setStyleSheet(f"background: {bg};")

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
    layer_visibility_toggled = pyqtSignal(str, bool)  # id, visible

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
            f"QListWidget {{ background: transparent; border: none; }}"
            f"QListWidget::item {{ border-bottom: 1px solid {theme.PANEL_BG}; }}"
            f"QListWidget::item:selected {{ background: transparent; }}"
        )
        self._list.setSpacing(1)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
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
        row.visibility_toggled.connect(self.layer_visibility_toggled)

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
# _IllustrationsDrawer — collapsible asset browser for per-book illustrations
# ---------------------------------------------------------------------------

import shutil as _shutil


def _illustrations_dir(project_path: Path, slug: str) -> Path:
    """Return (and create if needed) the illustrations folder for *slug*."""
    from data.book import book_dir
    d = book_dir(project_path, slug) / "illustrations"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _scan_illustrations(illus_dir: Path) -> dict:
    """Return {category: [Path, …]} by scanning one level of subfolders.

    Files at the root level go under the empty-string category "".
    """
    if not illus_dir.exists():
        return {}
    result: dict = {}
    for entry in sorted(illus_dir.iterdir()):
        if entry.is_dir():
            pngs = sorted(p for p in entry.iterdir() if p.suffix.lower() == ".png")
            result[entry.name] = pngs
        elif entry.is_file() and entry.suffix.lower() == ".png":
            result.setdefault("", []).append(entry)
    return result


class _ThumbnailTile(QWidget):
    """Single illustration thumbnail: image + filename label."""

    _TILE_W = 96
    _TILE_H = 96
    _LABEL_H = 16

    def __init__(self, png_path: Path, source_rel: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._path = png_path
        self._source_rel = source_rel   # e.g. "illustrations/animals/deer.png"
        self.setFixedSize(self._TILE_W, self._TILE_H + self._LABEL_H + 4)
        self.setToolTip(str(png_path))
        self.setAttribute(Qt.WA_StyledBackground, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(1)

        self._img_lbl = QLabel()
        self._img_lbl.setAlignment(Qt.AlignCenter)
        self._img_lbl.setFixedSize(self._TILE_W - 4, self._TILE_H - 4)
        self._img_lbl.setStyleSheet("background: transparent;")
        layout.addWidget(self._img_lbl)

        self._name_lbl = QLabel(png_path.stem)
        self._name_lbl.setAlignment(Qt.AlignCenter)
        self._name_lbl.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-size: {max(7, theme.BASE_PT - 2)}pt;"
            f" background: transparent;"
        )
        self._name_lbl.setFixedHeight(self._LABEL_H)
        layout.addWidget(self._name_lbl)

        self._load_pixmap()

    def _load_pixmap(self) -> None:
        pix = QPixmap(str(self._path))
        if not pix.isNull():
            w = self._TILE_W - 8
            h = self._TILE_H - 8
            pix = pix.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._img_lbl.setPixmap(pix)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if event.buttons() & Qt.LeftButton and self._source_rel:
            drag = QDrag(self)
            mime = QMimeData()
            mime.setData(
                "application/x-crossing-illus-source",
                QByteArray(self._source_rel.encode("utf-8")),
            )
            drag.setMimeData(mime)
            pix = self._img_lbl.pixmap()
            if pix and not pix.isNull():
                scaled = pix.scaled(48, 48, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                drag.setPixmap(scaled)
                drag.setHotSpot(QPoint(scaled.width() // 2, scaled.height() // 2))
            drag.exec_(Qt.CopyAction)


class _CategorySection(QWidget):
    """Category header + 2-column grid of thumbnail tiles.  Accepts PNG drops."""

    file_dropped = pyqtSignal(str, str)   # (category, src_path)

    def __init__(self, category: str, paths: list, base_source_rel: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._category = category
        self._base_source_rel = base_source_rel  # e.g. "illustrations/animals/"
        self.setAcceptDrops(True)
        self.setAttribute(Qt.WA_StyledBackground, True)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 4)
        self._layout.setSpacing(2)

        if category:
            hdr = QLabel(category)
            hdr.setStyleSheet(
                f"color: {theme.TEXT}; font-size: {theme.BASE_PT}pt;"
                f" font-weight: bold; background: transparent; padding: 2px 0;"
            )
            self._layout.addWidget(hdr)

        self._grid_widget = QWidget()
        self._grid_widget.setStyleSheet("background: transparent;")
        self._grid = QGridLayout(self._grid_widget)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(4)
        self._layout.addWidget(self._grid_widget)

        self._populate(paths)

    def _populate(self, paths: list) -> None:
        # Clear existing tiles
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for i, p in enumerate(paths):
            src_rel = self._base_source_rel + p.name if self._base_source_rel else f"illustrations/{p.name}"
            tile = _ThumbnailTile(p, source_rel=src_rel)
            self._grid.addWidget(tile, i // 2, i % 2)

    def refresh(self, paths: list) -> None:
        self._populate(paths)

    # -- drag and drop acceptance ------------------------------------------
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if any(u.toLocalFile().lower().endswith(".png") for u in urls):
                event.acceptProposedAction()
                return
        event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        for url in event.mimeData().urls():
            src = url.toLocalFile()
            if src.lower().endswith(".png"):
                self.file_dropped.emit(self._category, src)
        event.acceptProposedAction()


class _IllustrationsDrawer(QWidget):
    """Collapsible illustrations browser for the current book.

    Scans ``output/books/<slug>/illustrations/`` and shows PNG thumbnails
    grouped by subfolder.  Auto-refreshes via polling every 2 s.
    """

    _POLL_MS = 2000

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._project_path: Optional[Path] = None
        self._slug: Optional[str] = None
        self._last_state: dict = {}    # category → [str(path), …] — for change detection
        self._expanded: bool = True
        self._sections: dict = {}      # category → _CategorySection widget

        self.setAcceptDrops(True)
        self._build_ui()

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(self._POLL_MS)
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start()

    # ------------------------------------------------------------------
    # UI construction

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── header row: toggle + title + add-folder button ──────────
        hdr = QWidget()
        hdr.setStyleSheet(f"background: {theme.BTN_BG}; border-radius: 3px;")
        hdr.setFixedHeight(26)
        hdr_lay = QHBoxLayout(hdr)
        hdr_lay.setContentsMargins(6, 0, 4, 0)
        hdr_lay.setSpacing(4)

        self._toggle_btn = QPushButton("▼  Illustrations")
        self._toggle_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; color: {theme.TEXT};"
            f" font-size: {theme.BASE_PT}pt; font-weight: bold; text-align: left; }}"
        )
        self._toggle_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._toggle_btn.setFocusPolicy(Qt.NoFocus)
        self._toggle_btn.clicked.connect(self._toggle)
        hdr_lay.addWidget(self._toggle_btn)

        self._add_folder_btn = QPushButton("+")
        self._add_folder_btn.setToolTip("Add a new category folder")
        self._add_folder_btn.setFixedSize(22, 22)
        self._add_folder_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.BTN_BG}; border: none; color: {theme.TEXT};"
            f" font-size: {theme.BASE_PT + 2}pt; border-radius: 3px; }}"
            f"QPushButton:hover {{ background: {theme.BTN_HOVER}; }}"
        )
        self._add_folder_btn.setFocusPolicy(Qt.NoFocus)
        self._add_folder_btn.clicked.connect(self._on_add_folder)
        hdr_lay.addWidget(self._add_folder_btn)

        outer.addWidget(hdr)

        # ── body: scrollable category grid ────────────────────────────
        self._body = QWidget()
        self._body.setStyleSheet(f"background: {theme.PANEL_BG};")
        body_outer = QVBoxLayout(self._body)
        body_outer.setContentsMargins(0, 4, 0, 0)
        body_outer.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setFocusPolicy(Qt.NoFocus)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setMinimumHeight(120)
        self._scroll.setMaximumHeight(400)
        self._scroll.setStyleSheet(
            f"QScrollArea {{ background: {theme.PANEL_BG}; border: none; }}"
        )

        self._content = QWidget()
        self._content.setStyleSheet(f"background: {theme.PANEL_BG};")
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(4, 4, 4, 4)
        self._content_layout.setSpacing(8)

        self._empty_label = QLabel("No illustrations yet.\nDrop PNGs here.")
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-size: {theme.BASE_PT}pt; background: transparent;"
        )
        self._empty_label.setWordWrap(True)
        self._content_layout.addWidget(self._empty_label)
        self._content_layout.addStretch()

        self._scroll.setWidget(self._content)
        body_outer.addWidget(self._scroll)
        outer.addWidget(self._body)

        self.setAcceptDrops(True)

    # ------------------------------------------------------------------
    # Public API

    def set_book(self, project_path: Optional[Path], slug: Optional[str]) -> None:
        self._project_path = project_path
        self._slug = slug
        self._last_state = {}
        self._rebuild()

    # ------------------------------------------------------------------
    # Toggle

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._body.setVisible(self._expanded)
        arrow = "▼" if self._expanded else "▶"
        self._toggle_btn.setText(f"{arrow}  Illustrations")

    # ------------------------------------------------------------------
    # Folder creation

    def _on_add_folder(self) -> None:
        if not self._project_path or not self._slug:
            return
        name, ok = QInputDialog.getText(self, "New Category", "Category name:")
        if not ok or not name.strip():
            return
        name = name.strip()
        illus_dir = _illustrations_dir(self._project_path, self._slug)
        new_dir = illus_dir / name
        try:
            new_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            QMessageBox.warning(self, "Error", f"Could not create folder:\n{e}")
            return
        self._rebuild()

    # ------------------------------------------------------------------
    # Polling

    def _poll(self) -> None:
        if not self._project_path or not self._slug:
            return
        illus_dir = _illustrations_dir(self._project_path, self._slug)
        current = _scan_illustrations(illus_dir)
        serialised = {cat: [str(p) for p in paths] for cat, paths in current.items()}
        if serialised != self._last_state:
            self._last_state = serialised
            self._rebuild_from(current)

    def _rebuild(self) -> None:
        if not self._project_path or not self._slug:
            self._rebuild_from({})
            return
        illus_dir = _illustrations_dir(self._project_path, self._slug)
        data = _scan_illustrations(illus_dir)
        self._last_state = {cat: [str(p) for p in paths] for cat, paths in data.items()}
        self._rebuild_from(data)

    def _rebuild_from(self, data: dict) -> None:
        # Remove old section widgets
        for sec in self._sections.values():
            self._content_layout.removeWidget(sec)
            sec.deleteLater()
        self._sections = {}

        # Remove trailing stretch
        while self._content_layout.count():
            item = self._content_layout.takeAt(self._content_layout.count() - 1)
            if item.widget():
                item.widget().deleteLater()

        if not data:
            self._empty_label = QLabel("No illustrations yet.\nDrop PNGs here.")
            self._empty_label.setAlignment(Qt.AlignCenter)
            self._empty_label.setStyleSheet(
                f"color: {theme.TEXT_DIM}; font-size: {theme.BASE_PT}pt;"
                f" background: transparent;"
            )
            self._empty_label.setWordWrap(True)
            self._content_layout.addWidget(self._empty_label)
        else:
            for cat, paths in sorted(data.items()):
                base = f"illustrations/{cat}/" if cat else "illustrations/"
                sec = _CategorySection(cat, paths, base_source_rel=base)
                sec.file_dropped.connect(self._on_file_dropped)
                self._sections[cat] = sec
                self._content_layout.addWidget(sec)

        self._content_layout.addStretch()

    # ------------------------------------------------------------------
    # Drop handling (on the drawer itself — root level)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            if any(u.toLocalFile().lower().endswith(".png")
                   for u in event.mimeData().urls()):
                event.acceptProposedAction()
                return
        event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        pngs = [u.toLocalFile() for u in event.mimeData().urls()
                if u.toLocalFile().lower().endswith(".png")]
        if not pngs:
            event.ignore()
            return
        self._receive_drops(pngs)
        event.acceptProposedAction()

    def _receive_drops(self, png_paths: list, category: str = "") -> None:
        """Copy *png_paths* into the given category (or prompt if no categories)."""
        if not self._project_path or not self._slug:
            return
        illus_dir = _illustrations_dir(self._project_path, self._slug)

        # Resolve target category folder
        if not category:
            # Use first existing category or root
            existing = sorted(
                d.name for d in illus_dir.iterdir() if d.is_dir()
            ) if illus_dir.exists() else []
            if existing:
                cat_name, ok = QInputDialog.getItem(
                    self, "Choose Category",
                    "Copy into category:", existing, 0, False
                )
                if not ok:
                    return
                dest_dir = illus_dir / cat_name
            else:
                dest_dir = illus_dir
        else:
            dest_dir = illus_dir / category

        dest_dir.mkdir(parents=True, exist_ok=True)
        for src in png_paths:
            src_path = Path(src)
            dest = dest_dir / src_path.name
            if dest != src_path:
                _shutil.copy2(str(src_path), str(dest))
        self._rebuild()

    def _on_file_dropped(self, category: str, src: str) -> None:
        self._receive_drops([src], category)


# ---------------------------------------------------------------------------
# Silhouette Browser sidebar — embedded catalog browser for Book Visualizer
# ---------------------------------------------------------------------------

_BROWSER_PANEL_W    = 420   # px — preferred width of the silhouette browser column (3 cols + margin)
_BROWSER_THUMB_SZ   = 80    # px per thumbnail cell
_BROWSER_THUMB_GAP  = 6     # px gap between cells
_BROWSER_PAGE_SIZE  = 50    # silhouettes per page
_BROWSER_LOAD_BATCH = 15    # loader yields UI thread every N images


class _BrowserThumbLoader(QThread):
    """Background loader: reads PNG thumbnails and emits QImages for the sidebar grid.

    Mirrors the _ThumbLoader in silhouette_visualizer.py but is self-contained
    here to avoid cross-visualizer imports.
    """

    thumb_ready   = pyqtSignal(int, QImage)  # (cell index, QImage)
    load_finished = pyqtSignal()

    def __init__(self, records: list, size: int, parent=None) -> None:
        super().__init__(parent)
        self._records   = records
        self._size      = size
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            from PIL import Image as _PIL
        except ImportError:
            self.load_finished.emit()
            return

        for i, rec in enumerate(self._records):
            if self._cancelled:
                break
            json_path = rec.get("path")
            if not json_path:
                continue
            png_path = Path(str(json_path)).with_suffix(".png")
            try:
                img = _PIL.open(str(png_path)).convert("RGBA")
                img.thumbnail((self._size, self._size), _PIL.LANCZOS)
                w, h = img.size
                data = img.tobytes("raw", "RGBA")
                qimg = QImage(data, w, h, 4 * w, QImage.Format_RGBA8888)
                self.thumb_ready.emit(i, qimg.copy())
            except Exception:
                pass
            if (i + 1) % _BROWSER_LOAD_BATCH == 0:
                self.msleep(2)   # yield so UI stays responsive

        self.load_finished.emit()


class _EngravingThumbLoader(QThread):
    """Background loader for engraving thumbnail images.

    Accepts a list of ``(index, abs_path)`` pairs and emits ``thumb_ready``
    for each successfully loaded image so the GUI thread can update cells
    without blocking.
    """

    thumb_ready = pyqtSignal(int, QImage)

    def __init__(self, paths: list, size: int, parent=None) -> None:
        super().__init__(parent)
        self._paths     = paths   # list of (int, str)
        self._size      = size
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            from PIL import Image as _PIL
        except ImportError:
            return
        for i, path in self._paths:
            if self._cancelled:
                break
            try:
                img = _PIL.open(path).convert("RGBA")
                img.thumbnail((self._size, self._size), _PIL.LANCZOS)
                w, h = img.size
                data = img.tobytes("raw", "RGBA")
                qimg = QImage(data, w, h, 4 * w, QImage.Format_RGBA8888)
                self.thumb_ready.emit(i, qimg.copy())
            except Exception:
                pass
            self.msleep(2)   # yield so UI stays responsive


class _BrowserThumbCell(QLabel):
    """Single thumbnail cell in the silhouette browser grid.

    Emits ``single_clicked`` on left-click and ``double_clicked`` on
    double-click so the browser can distinguish selection from insertion.
    """

    single_clicked = pyqtSignal(int)  # cell index
    double_clicked = pyqtSignal(int)  # cell index

    def __init__(self, index: int, tooltip: str = "", parent=None) -> None:
        super().__init__(parent)
        self._index          = index
        self._selected       = False
        self._drag_abs_path  = ""      # set by _rebuild_grid after construction
        self._drag_meta: dict = {}     # record dict used for the drag payload
        self._press_pos: Optional[QPoint] = None
        self.setFixedSize(_BROWSER_THUMB_SZ, _BROWSER_THUMB_SZ)
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.PointingHandCursor)
        if tooltip:
            self.setToolTip(tooltip)
        self._apply_style()
        self.setText("·")   # placeholder until image loads

    def set_image(self, qimg: QImage) -> None:
        """Called from the GUI thread when the loader delivers a QImage."""
        self.setPixmap(QPixmap.fromImage(qimg))
        self.setText("")

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self._apply_style()

    def _apply_style(self) -> None:
        border = (
            f"2px solid {theme.ACCENT}" if self._selected else "1px solid transparent"
        )
        self.setStyleSheet(
            f"background: transparent; border: {border};"
            f" color: {theme.TEXT_DIM};"
            f" font-family: '{theme.FAMILY_MONO}'; font-size: {theme.BASE_PT}pt;"
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._press_pos = event.pos()
            self.single_clicked.emit(self._index)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.double_clicked.emit(self._index)
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if (event.buttons() & Qt.LeftButton
                and self._press_pos is not None
                and self._drag_abs_path
                and (event.pos() - self._press_pos).manhattanLength()
                    >= QApplication.startDragDistance()):
            mime = QMimeData()
            payload = json.dumps({"abs_path": self._drag_abs_path,
                                   "meta":     self._drag_meta})
            mime.setData("application/x-crossing-illus-source",
                         QByteArray(payload.encode()))
            drag = QDrag(self)
            drag.setMimeData(mime)
            pix = self.pixmap()
            if pix and not pix.isNull():
                scaled = pix.scaled(48, 48, Qt.KeepAspectRatio,
                                    Qt.SmoothTransformation)
                drag.setPixmap(scaled)
                drag.setHotSpot(QPoint(scaled.width() // 2,
                                       scaled.height() // 2))
            self._press_pos = None  # prevent re-triggering on same press
            drag.exec_(Qt.CopyAction)
            return
        super().mouseMoveEvent(event)


class _SilhouetteBrowserPanel(QWidget):
    """Docked sidebar panel for browsing the silhouette catalog.

    Two tabs:
        Silhouettes — Scope / Field / Label filters + paginated thumbnail grid.
        Engravings  — generated asset library; entries are added by the
                       page-centric adaptive engraving workflow.

    Emits ``silhouette_insert_requested(png_path, metadata)`` when the user
    double-clicks a thumbnail.  ``BookVisualizerWindow`` connects this signal
    to ``_insert_silhouette``, which copies the PNG into the book's
    ``illustrations/silhouettes/`` folder and creates an Image layer.
    """

    silhouette_insert_requested = pyqtSignal(str, dict)  # (abs png_path, metadata)
    engraving_delete_requested   = pyqtSignal(str)        # layer_id of engraving to delete
    thumbnail_selected           = pyqtSignal(dict)       # catalog record on single-click
    engraving_selected           = pyqtSignal(dict)       # engraving entry on single-click

    # Background colour for the selected tab + its content pane.
    # Noticeably darker than PANEL_BG (#6e6e6e) so the active tab stands out.
    _TAB_CONTENT_BG = "#5a5a5a"

    # Standard annotation field order — same ordering as CatalogBrowser
    _FIELD_ORDER = [
        "--all", "setting", "description", "objects",
        "action", "humans", "wearing", "animals", "text",
    ]

    def __init__(
        self,
        project_path: str,
        media_type: str = "movies",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._project_path = project_path
        self._media_type   = media_type

        # Catalog data (populated by _load_catalog)
        self._field_map: dict = {}    # {field: {label: [records]}}
        self._film_list: list = []    # sorted list of film stems

        # Current filtered view
        self._current_records: list = []
        self._page_idx:        int  = 0
        self._page_count:      int  = 1

        # Grid state
        self._page_records: list = []   # records slice shown in the current grid
        self._cells:        list = []
        self._selected_idx: int  = -1
        self._loader: Optional[_BrowserThumbLoader] = None

        # Engraving collection — populated externally by BookVisualizerWindow
        self._engravings: list = []

        # Engraving thumbnail loader (background thread, like _loader for silhouettes)
        self._eng_loader = None
        self._eng_cells:      list = []   # _BrowserThumbCell instances for the current grid
        self._eng_containers: list = []   # matching container widgets (for resize re-layout)
        self._selected_eng_idx: int = -1

        self.setStyleSheet(_PANEL_STYLESHEET)
        self.setMinimumWidth(180)
        self._build_ui()

        # Defer catalog scan until after the window has finished its first layout pass
        QTimer.singleShot(0, self._load_catalog)

    # ------------------------------------------------------------------
    # UI construction

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        # drawBase draws a 1px platform-palette line at the bottom of the tab
        # bar using QPalette::Light (white on dark themes) regardless of any
        # stylesheet setting.  Disabling it removes the white line entirely.
        self._tabs.tabBar().setDrawBase(False)
        self._tabs.setStyleSheet(
            f"QTabWidget           {{ background: {theme.PANEL_BG}; border: none; }}"
            f"QTabWidget::pane     {{ border: none; background: transparent; top: -1px; }}"
            f"QTabBar              {{ background: {theme.PANEL_BG}; border: none; }}"
            f"QTabBar::tab {{"
            f"  background: {theme.PANEL_BG}; color: {theme.TEXT};"
            f"  padding: 4px 10px; border: none; margin-bottom: 0px;"
            f"  font-family: '{theme.FAMILY_UI}'; font-size: {theme.BASE_PT}pt;"
            f"}}"
            f"QTabBar::tab:selected {{ background: {self._TAB_CONTENT_BG};"
            f"                         color: {theme.TEXT}; border: none; }}"
            f"QTabBar::tab:hover    {{ background: {self._TAB_CONTENT_BG}; color: {theme.TEXT}; }}"
        )
        self._tabs.addTab(self._build_silhouettes_tab(), "Silhouettes")
        self._tabs.addTab(self._build_engravings_tab(),  "Engravings")
        self._tabs.currentChanged.connect(self._on_tab_changed)
        outer.addWidget(self._tabs)

    def _build_silhouettes_tab(self) -> QWidget:
        widget = QWidget()
        widget.setStyleSheet(_PANEL_STYLESHEET.replace(
            f"QWidget {{ background: {theme.PANEL_BG}; }}",
            f"QWidget {{ background: {self._TAB_CONTENT_BG}; }}",
        ))
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 8, 8, 4)
        layout.setSpacing(6)

        combo_style = (
            f"QComboBox {{ background: {theme.INPUT_BG}; color: {theme.TEXT};"
            f" font-size: {theme.BASE_PT}pt; }}"
        )

        # Scope ──────────────────────────────────────────────────────
        scope_grp = QGroupBox("Scope")
        sg = QVBoxLayout(scope_grp)
        sg.setContentsMargins(6, 8, 6, 6)
        self._scope_combo = QComboBox()
        self._scope_combo.setFocusPolicy(Qt.NoFocus)
        self._scope_combo.setStyleSheet(combo_style)
        self._scope_combo.currentIndexChanged.connect(self._on_scope_changed)
        sg.addWidget(self._scope_combo)
        layout.addWidget(scope_grp)

        # Field ──────────────────────────────────────────────────────
        field_grp = QGroupBox("Field")
        fg = QVBoxLayout(field_grp)
        fg.setContentsMargins(6, 8, 6, 6)
        self._field_combo = QComboBox()
        self._field_combo.setFocusPolicy(Qt.NoFocus)
        self._field_combo.setStyleSheet(combo_style)
        self._field_combo.currentIndexChanged.connect(self._on_field_changed)
        fg.addWidget(self._field_combo)
        layout.addWidget(field_grp)

        # Label ──────────────────────────────────────────────────────
        label_grp = QGroupBox("Label")
        lg = QVBoxLayout(label_grp)
        lg.setContentsMargins(6, 8, 6, 6)
        self._label_combo = QComboBox()
        self._label_combo.setFocusPolicy(Qt.NoFocus)
        self._label_combo.setStyleSheet(combo_style)
        self._label_combo.currentIndexChanged.connect(self._on_label_changed)
        lg.addWidget(self._label_combo)
        layout.addWidget(label_grp)

        # Status line ────────────────────────────────────────────────
        self._status_lbl = QLabel("—")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-size: {max(7, theme.BASE_PT - 1)}pt;"
        )
        layout.addWidget(self._status_lbl)

        # Thumbnail grid scroll area ─────────────────────────────────
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setFocusPolicy(Qt.NoFocus)
        self._scroll.setStyleSheet(
            f"QScrollArea {{ background: transparent; border: none; }}"
        )

        self._grid_widget = QWidget()
        self._grid_widget.setStyleSheet("background: transparent;")
        self._grid_layout = QGridLayout(self._grid_widget)
        self._grid_layout.setContentsMargins(
            _BROWSER_THUMB_GAP, _BROWSER_THUMB_GAP,
            _BROWSER_THUMB_GAP, _BROWSER_THUMB_GAP,
        )
        self._grid_layout.setSpacing(_BROWSER_THUMB_GAP)
        self._grid_layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._scroll.setWidget(self._grid_widget)

        layout.addWidget(self._scroll, 1)   # stretch=1 → grid fills remaining height

        # Pagination bar ─────────────────────────────────────────────
        page_row = QWidget()
        page_row.setStyleSheet(f"background: {self._TAB_CONTENT_BG};")
        pr = QHBoxLayout(page_row)
        pr.setContentsMargins(4, 4, 4, 4)
        pr.setSpacing(4)

        self._prev_btn = QPushButton("◀")
        self._prev_btn.setFixedSize(28, 24)
        self._prev_btn.setFocusPolicy(Qt.NoFocus)
        self._prev_btn.clicked.connect(self._on_prev_page)
        pr.addWidget(self._prev_btn)

        self._page_lbl = QLabel("—")
        self._page_lbl.setAlignment(Qt.AlignCenter)
        self._page_lbl.setStyleSheet(
            f"color: {theme.TEXT}; font-size: {max(7, theme.BASE_PT - 1)}pt;"
        )
        pr.addWidget(self._page_lbl, 1)

        self._next_btn = QPushButton("▶")
        self._next_btn.setFixedSize(28, 24)
        self._next_btn.setFocusPolicy(Qt.NoFocus)
        self._next_btn.clicked.connect(self._on_next_page)
        pr.addWidget(self._next_btn)

        layout.addWidget(page_row)
        return widget

    def _build_engravings_tab(self) -> QWidget:
        widget = QWidget()
        widget.setStyleSheet(_PANEL_STYLESHEET.replace(
            f"QWidget {{ background: {theme.PANEL_BG}; }}",
            f"QWidget {{ background: {self._TAB_CONTENT_BG}; }}",
        ))
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Scroll area containing the engraving thumbnail grid
        self._eng_scroll = QScrollArea()
        self._eng_scroll.setWidgetResizable(True)
        self._eng_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._eng_scroll.setFrameShape(QFrame.NoFrame)
        self._eng_scroll.setFocusPolicy(Qt.NoFocus)
        self._eng_scroll.setStyleSheet(
            f"QScrollArea {{ background: {self._TAB_CONTENT_BG}; border: none; }}"
        )

        self._eng_container = QWidget()
        self._eng_container.setStyleSheet(f"background: {self._TAB_CONTENT_BG};")
        self._eng_grid_layout = QGridLayout(self._eng_container)
        self._eng_grid_layout.setContentsMargins(
            _BROWSER_THUMB_GAP, _BROWSER_THUMB_GAP,
            _BROWSER_THUMB_GAP, _BROWSER_THUMB_GAP,
        )
        self._eng_grid_layout.setSpacing(_BROWSER_THUMB_GAP)
        self._eng_grid_layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)

        self._eng_scroll.setWidget(self._eng_container)
        layout.addWidget(self._eng_scroll, 1)
        return widget

    # ------------------------------------------------------------------
    # Catalog loading

    def _load_catalog(self) -> None:
        """Scan the silhouette catalog and populate filter combos."""
        self._stop_loader()
        try:
            from services.silhouette_catalog import scan_catalog
            all_records = scan_catalog(self._project_path, media_type=self._media_type)
        except Exception:
            self._status_lbl.setText("Catalog unavailable.")
            self._update_pagination(0, 1)
            return

        field_map: dict = {"--all": {}}
        film_set:  set  = set()
        for rec in all_records:
            if "error" in rec:
                continue
            label = rec.get("label") or ""
            field = rec.get("field") or "--all"
            if not label:
                continue
            field_map.setdefault(field, {}).setdefault(label, []).append(rec)
            field_map["--all"].setdefault(label, []).append(rec)
            stem = rec.get("filename_stem") or rec.get("filename") or ""
            if stem:
                film_set.add(stem)

        self._field_map = field_map
        self._film_list = sorted(film_set)

        # Scope combo
        self._scope_combo.blockSignals(True)
        self._scope_combo.clear()
        self._scope_combo.addItem("All Movies", userData=None)
        for stem in self._film_list:
            self._scope_combo.addItem(stem, userData=stem)
        self._scope_combo.blockSignals(False)

        # Field combo (standard order, only fields that have data)
        present = set(field_map.keys())
        self._field_combo.blockSignals(True)
        self._field_combo.clear()
        for f in self._FIELD_ORDER:
            if f in present:
                self._field_combo.addItem(f, userData=f)
        for f in sorted(present - set(self._FIELD_ORDER)):
            self._field_combo.addItem(f, userData=f)
        self._field_combo.blockSignals(False)

        if not field_map.get("--all"):
            self._status_lbl.setText("Catalog empty.\ncrossing index silhouette")
            self._label_combo.clear()
            self._clear_grid()
            self._update_pagination(0, 1)
            return

        self._field_combo.setCurrentIndex(0)
        self._on_field_changed(0)

    # ------------------------------------------------------------------
    # Filter cascade

    def _on_scope_changed(self, _idx: int) -> None:
        self._page_idx = 0
        self._apply_filters()

    def _on_field_changed(self, _idx: int) -> None:
        """Rebuild the label combo for the newly selected field."""
        field = self._field_combo.currentData() or "--all"
        label_counts = self._field_map.get(field, {})
        self._label_combo.blockSignals(True)
        self._label_combo.clear()
        for label in sorted(label_counts.keys()):
            count = len(label_counts[label])
            self._label_combo.addItem(f"{label}  ({count})", userData=label)
        self._label_combo.blockSignals(False)
        self._page_idx = 0
        if self._label_combo.count() > 0:
            self._label_combo.setCurrentIndex(0)
        self._apply_filters()

    def _on_label_changed(self, _idx: int) -> None:
        self._page_idx = 0
        self._apply_filters()

    def _apply_filters(self) -> None:
        field   = self._field_combo.currentData() or "--all"
        label   = self._label_combo.currentData()
        film    = self._scope_combo.currentData()
        records = self._field_map.get(field, {}).get(label, []) if label else []
        if film:
            records = [
                r for r in records
                if (r.get("filename_stem") or r.get("filename") or "") == film
            ]
        self._current_records = records
        self._selected_idx    = -1
        self._show_page(0)

    # ------------------------------------------------------------------
    # Pagination

    def _show_page(self, page_idx: int) -> None:
        total      = len(self._current_records)
        page_count = max(1, math.ceil(total / _BROWSER_PAGE_SIZE)) if total else 1
        self._page_idx   = max(0, min(page_idx, page_count - 1))
        self._page_count = page_count
        self._update_pagination(self._page_idx, page_count)

        start = self._page_idx * _BROWSER_PAGE_SIZE
        end   = min(start + _BROWSER_PAGE_SIZE, total)
        self._page_records = self._current_records[start:end]

        if total == 0:
            self._status_lbl.setText("No objects found")
        else:
            self._status_lbl.setText(f"{start + 1}–{end} of {total}")

        self._cells = []
        self._clear_grid()
        self._rebuild_grid()
        self._start_loader()

    def _update_pagination(self, page_idx: int, page_count: int) -> None:
        self._page_lbl.setText(f"Page {page_idx + 1} / {page_count}")
        self._prev_btn.setEnabled(page_idx > 0)
        self._next_btn.setEnabled(page_idx < page_count - 1)

    def _on_prev_page(self) -> None:
        if self._page_idx > 0:
            self._show_page(self._page_idx - 1)

    def _on_next_page(self) -> None:
        if self._page_idx < self._page_count - 1:
            self._show_page(self._page_idx + 1)

    # ------------------------------------------------------------------
    # Grid management

    def _clear_grid(self) -> None:
        self._stop_loader()
        while self._grid_layout.count():
            item = self._grid_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _cols(self) -> int:
        vw = self._scroll.viewport().width()
        if vw <= 0:
            vw = 200
        return max(1, (vw - _BROWSER_THUMB_GAP) // (_BROWSER_THUMB_SZ + _BROWSER_THUMB_GAP))

    def _rebuild_grid(self) -> None:
        cols      = self._cols()
        start_abs = self._page_idx * _BROWSER_PAGE_SIZE
        for i, rec in enumerate(self._page_records):
            label = rec.get("label", "")
            stem  = rec.get("filename_stem") or rec.get("filename") or ""
            frame = rec.get("frame", "")
            tip   = f"#{start_abs + i + 1}  {label}  {stem}  f:{frame}"
            cell  = _BrowserThumbCell(i, tooltip=tip)
            cell.single_clicked.connect(self._on_cell_single)
            cell.double_clicked.connect(self._on_cell_double)
            # Populate drag source — same resolution as _on_cell_double
            json_path = rec.get("path")
            if json_path:
                png_path = Path(str(json_path)).with_suffix(".png")
                if png_path.exists():
                    cell._drag_abs_path = str(png_path)
                    cell._drag_meta     = {k: str(v) if isinstance(v, Path) else v
                                           for k, v in rec.items()}
            self._grid_layout.addWidget(cell, i // cols, i % cols)
            self._cells.append(cell)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._cells:
            cols = self._cols()
            for i, cell in enumerate(self._cells):
                self._grid_layout.addWidget(cell, i // cols, i % cols)
        if self._eng_containers:
            vw = self._eng_scroll.viewport().width()
            if vw <= 0:
                vw = 200
            cols = max(1, (vw - _BROWSER_THUMB_GAP) // (_BROWSER_THUMB_SZ + _BROWSER_THUMB_GAP))
            for i, container in enumerate(self._eng_containers):
                self._eng_grid_layout.addWidget(container, i // cols, i % cols)

    def _on_tab_changed(self, index: int) -> None:
        """Re-layout the engravings grid when the Engravings tab becomes visible.

        The grid is built while the tab is hidden, so viewport().width() is 0
        at that point.  This fires once the tab is actually shown and the
        scroll area has its real width.
        """
        if index == 1 and self._eng_containers:  # 1 = Engravings tab
            vw = self._eng_scroll.viewport().width()
            if vw <= 0:
                vw = 200
            cols = max(1, (vw - _BROWSER_THUMB_GAP) // (_BROWSER_THUMB_SZ + _BROWSER_THUMB_GAP))
            for i, container in enumerate(self._eng_containers):
                self._eng_grid_layout.addWidget(container, i // cols, i % cols)

    # ------------------------------------------------------------------
    # Background loader

    def _start_loader(self) -> None:
        if not self._page_records:
            return
        self._loader = _BrowserThumbLoader(self._page_records, _BROWSER_THUMB_SZ)
        self._loader.thumb_ready.connect(self._on_thumb_ready)
        self._loader.start()

    def _stop_loader(self) -> None:
        if self._loader and self._loader.isRunning():
            self._loader.cancel()
            self._loader.wait(500)
        self._loader = None

    def _on_thumb_ready(self, idx: int, qimg: QImage) -> None:
        if 0 <= idx < len(self._cells):
            self._cells[idx].set_image(qimg)

    def _stop_eng_loader(self) -> None:
        if self._eng_loader and self._eng_loader.isRunning():
            self._eng_loader.cancel()
            self._eng_loader.wait(500)
        self._eng_loader    = None
        self._eng_cells     = []
        self._eng_containers = []
        self._selected_eng_idx = -1

    def _on_eng_thumb_ready(self, idx: int, qimg: QImage) -> None:
        if 0 <= idx < len(self._eng_cells):
            self._eng_cells[idx].set_image(qimg)

    # ------------------------------------------------------------------
    # Keyboard navigation helpers (called from BookVisualizerWindow.eventFilter)

    @staticmethod
    def _step_combo(combo: QComboBox, delta: int) -> None:
        """Advance *combo* by *delta* steps, wrapping at the ends."""
        n = combo.count()
        if n == 0:
            return
        new_idx = (combo.currentIndex() + delta) % n
        combo.setCurrentIndex(new_idx)

    def step_scope(self, delta: int) -> None:
        """Step the Scope combo by *delta* (works on both tabs)."""
        self._step_combo(self._scope_combo, delta)

    def step_field(self, delta: int) -> None:
        """Step the Field combo by *delta* (Silhouettes tab only)."""
        if self._tabs.currentIndex() == 0:
            self._step_combo(self._field_combo, delta)

    def step_label(self, delta: int) -> None:
        """Step the Label combo by *delta* (Silhouettes tab only)."""
        if self._tabs.currentIndex() == 0:
            self._step_combo(self._label_combo, delta)

    # ------------------------------------------------------------------
    # Selection and insertion

    def _on_eng_cell_single(self, idx: int) -> None:
        """Highlight the clicked engraving cell and emit engraving_selected."""
        if 0 <= self._selected_eng_idx < len(self._eng_cells):
            self._eng_cells[self._selected_eng_idx].set_selected(False)
        self._selected_eng_idx = idx
        if 0 <= idx < len(self._eng_cells):
            self._eng_cells[idx].set_selected(True)
        sorted_eng = sorted(self._engravings, key=lambda e: e.get("name", ""))
        if idx < len(sorted_eng):
            self.engraving_selected.emit(dict(sorted_eng[idx]))

    def _on_cell_single(self, idx: int) -> None:
        """Select the thumbnail at *idx* and highlight it."""
        if self._selected_idx == idx:
            return
        if 0 <= self._selected_idx < len(self._cells):
            self._cells[self._selected_idx].set_selected(False)
        self._selected_idx = idx
        if 0 <= idx < len(self._cells):
            self._cells[idx].set_selected(True)
        abs_idx = self._page_idx * _BROWSER_PAGE_SIZE + idx
        if abs_idx < len(self._current_records):
            self.thumbnail_selected.emit(dict(self._current_records[abs_idx]))

    def clear_selection(self) -> None:
        """Deselect the currently highlighted thumbnail without emitting a signal."""
        if 0 <= self._selected_idx < len(self._cells):
            self._cells[self._selected_idx].set_selected(False)
        self._selected_idx = -1

    def _on_cell_double(self, idx: int) -> None:
        """Select thumbnail and emit an insert request to the Book Visualizer."""
        self._on_cell_single(idx)
        abs_idx = self._page_idx * _BROWSER_PAGE_SIZE + idx
        if abs_idx >= len(self._current_records):
            return
        rec       = self._current_records[abs_idx]
        json_path = rec.get("path")
        if not json_path:
            return
        png_path = Path(str(json_path)).with_suffix(".png")
        if not png_path.exists():
            return
        self.silhouette_insert_requested.emit(str(png_path), dict(rec))

    # ------------------------------------------------------------------
    # Engraving collection — public API (called by BookVisualizerWindow)

    def set_engravings(self, entries: list) -> None:
        """Replace the full engraving list and refresh the Engravings tab."""
        self._engravings = list(entries)
        self._refresh_engravings_tab()

    def add_engraving(self, entry: dict) -> None:
        """Append one engraving entry and refresh the Engravings tab."""
        self._engravings.append(entry)
        self._refresh_engravings_tab()

    def _refresh_engravings_tab(self) -> None:
        """Rebuild the engraving thumbnail grid in alphabetical order."""
        # Stop any in-flight thumbnail loader and reset the cell list.
        self._stop_eng_loader()

        # Clear existing grid items
        while self._eng_grid_layout.count():
            item = self._eng_grid_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        sorted_eng = sorted(self._engravings, key=lambda e: e.get("name", ""))

        if not sorted_eng:
            empty_lbl = QLabel(
                "No engravings yet.\n"
                "Drop a silhouette on the page,\n"
                "then click \u2295 Adaptive Engraving."
            )
            empty_lbl.setWordWrap(True)
            empty_lbl.setAlignment(Qt.AlignCenter)
            empty_lbl.setStyleSheet(
                f"color: {theme.TEXT_DIM}; font-size: {theme.BASE_PT}pt;"
                f" background: transparent; padding: 16px;"
            )
            self._eng_grid_layout.addWidget(empty_lbl, 0, 0)
            return

        vw = self._eng_scroll.viewport().width()
        if vw <= 0:
            vw = 200
        cols = max(1, (vw - _BROWSER_THUMB_GAP) // (_BROWSER_THUMB_SZ + _BROWSER_THUMB_GAP))

        for i, entry in enumerate(sorted_eng):
            # Build tooltip: name + preprocessing size (if available)
            tip = entry.get("name", "")
            pre_size = entry.get("preprocessing_size")
            if pre_size and len(pre_size) == 2:
                tip = f"{tip}\n{pre_size[0]}×{pre_size[1]} px (preprocessed)"

            cell = _BrowserThumbCell(i, tooltip=tip)

            # Enable drag-and-drop onto book pages.
            # Use the best available image: output_png first, then fallbacks.
            drag_png = (
                entry.get("output_png", "") or
                entry.get("preprocessing_path", "") or
                entry.get("source_png", "")
            )
            if drag_png and Path(drag_png).exists():
                cell._drag_abs_path = drag_png
                cell._drag_meta = {
                    "label":       entry.get("name", "engraving"),
                    "layer_id":    entry.get("layer_id", ""),
                    "output_png":  entry.get("output_png", ""),
                    "source_png":  entry.get("source_png", ""),
                    "line_weight": entry.get("line_weight", 1.0),
                }

            # Wrap cell in a fixed-size container; overlay a trash button that
            # is hidden by default and revealed only on mouse-enter.
            layer_id = entry.get("layer_id", "")
            container = QWidget()
            container.setFixedSize(_BROWSER_THUMB_SZ, _BROWSER_THUMB_SZ)
            container.setStyleSheet("background: transparent;")
            cell.setParent(container)
            cell.move(0, 0)

            _TRASH_SZ = 20
            del_btn = QPushButton(container)
            del_btn.setIcon(_svg_icon("trash", _TRASH_SZ, "#ffffff"))
            del_btn.setIconSize(QSize(_TRASH_SZ, _TRASH_SZ))
            del_btn.setFixedSize(_TRASH_SZ + 4, _TRASH_SZ + 4)
            del_btn.move(_BROWSER_THUMB_SZ - _TRASH_SZ - 6, 4)
            del_btn.setCursor(Qt.PointingHandCursor)
            del_btn.setToolTip(f"Delete engraving \"{entry.get('name', '')}\"")
            del_btn.setStyleSheet(
                "QPushButton {"
                "  background: rgba(100,20,20,210);"
                "  border: none; border-radius: 3px;"
                "}"
                "QPushButton:hover { background: rgba(160,30,30,230); }"
            )
            del_btn.hide()
            del_btn.clicked.connect(
                lambda _checked, lid=layer_id: self.engraving_delete_requested.emit(lid)
            )

            # Show/hide the button on container hover via event filter.
            # Guard show/hide with try/except: deleteLater() is deferred so Qt
            # may still deliver a Leave event after the C++ object is gone.
            class _HoverFilter(QObject):
                def __init__(self, btn):
                    super().__init__(btn)
                    self._btn = btn
                def eventFilter(self, obj, ev):
                    try:
                        if ev.type() == QEvent.Enter:
                            self._btn.show()
                        elif ev.type() == QEvent.Leave:
                            self._btn.hide()
                    except RuntimeError:
                        pass  # C++ object already deleted — ignore
                    return False

            filt = _HoverFilter(del_btn)
            container.installEventFilter(filt)
            container.setMouseTracking(True)

            self._eng_cells.append(cell)
            self._eng_containers.append(container)
            cell.single_clicked.connect(self._on_eng_cell_single)
            self._eng_grid_layout.addWidget(container, i // cols, i % cols)

        # Start background thumbnail loader for engraving images.
        # Loading large FLUX/preprocessing PNGs synchronously would block
        # the GUI thread for several seconds when many engravings are present.
        # Do NOT call _stop_eng_loader() here — that would reset _eng_cells to
        # [] after we just populated it, so _on_eng_thumb_ready would never
        # find any cells.  Just cancel the previous loader if one is running.
        if self._eng_loader and self._eng_loader.isRunning():
            self._eng_loader.cancel()
            self._eng_loader.wait(500)
        self._eng_loader = None
        paths = [
            (i, thumb)
            for i, entry in enumerate(sorted_eng)
            if (thumb := (
                entry.get("output_png", "") or
                entry.get("preprocessing_path", "") or
                entry.get("source_png", "")
            )) and Path(thumb).exists()
        ]
        if paths:
            self._eng_loader = _EngravingThumbLoader(paths, _BROWSER_THUMB_SZ)
            self._eng_loader.thumb_ready.connect(self._on_eng_thumb_ready)
            self._eng_loader.start()

# ---------------------------------------------------------------------------
# Engraving generation worker
# ---------------------------------------------------------------------------


class _EngravingWorker(QObject):
    """Background worker that runs the engraving generator.

    Spawns ``crossing engraving smoke-test`` in a subprocess so each
    generation gets a fresh CUDA context.  Running CUDA inside a
    threading.Thread causes NVML reinitialisation failures on second
    and subsequent invocations.

    Signals
    -------
    finished(engraving_id, output_png, metadata_dict)
        Emitted on successful completion.
    failed(engraving_id, error_message)
        Emitted when generation raises any exception.
    """

    finished = pyqtSignal(str, str, dict)   # eng_id, output_png, metadata
    failed   = pyqtSignal(str, str)         # eng_id, error_message

    def __init__(
        self,
        *,
        project_path: str,
        engraving_id: str,
        preprocessing_path: str,
        preprocessing_size: list,
        cache_dir: Path,
        context_json: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._project_path       = project_path
        self._engraving_id       = engraving_id
        self._preprocessing_path = preprocessing_path
        self._preprocessing_size = preprocessing_size
        self._cache_dir          = Path(cache_dir)
        self._context_json       = context_json
        self._thread             = None

    def start(self) -> None:
        import threading
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            cmd = [
                sys.executable, str(_CLI_PATH),
                "engraving", "smoke-test",
                self._preprocessing_path,
                "--out-dir",      str(self._cache_dir),
                "--project-path", self._project_path,
            ]
            if self._context_json:
                cmd += ["--context", self._context_json]
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            # Echo subprocess output to the parent process terminal
            if proc.stdout:
                print(proc.stdout, end="", flush=True)
            if proc.returncode != 0:
                output = proc.stdout.strip()
                self.failed.emit(self._engraving_id, output)
                return

            # Read the generation JSON written by the smoke-test
            gen_json = self._cache_dir / f"{self._engraving_id}_generation.json"
            if not gen_json.exists():
                self.failed.emit(
                    self._engraving_id,
                    f"generation JSON not found: {gen_json}",
                )
                return

            meta = json.loads(gen_json.read_text(encoding="utf-8"))
            output_png = meta.get("output_png", "")
            if not output_png or not Path(output_png).exists():
                self.failed.emit(
                    self._engraving_id,
                    f"output_png missing from generation JSON: {gen_json}",
                )
                return

            self.finished.emit(self._engraving_id, output_png, meta)

        except Exception:
            import traceback
            self.failed.emit(self._engraving_id, traceback.format_exc())


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class _InspectorPanel(QWidget):
    """Inspector panel shown in the right column when an Image layer or catalog
    thumbnail is selected.  Replaces Book / Tools / Layers for the duration of
    the selection (Unity-style contextual Inspector).

    The parent window switches a ``QStackedWidget`` to index 1 to reveal this
    panel, and back to index 0 to restore the default layout.
    """

    line_weight_changed = pyqtSignal(float)  # slider moved
    open_in_shotlist    = pyqtSignal(str, str)  # (filename_stem, shot_id)
    open_in_silhouette  = pyqtSignal(str, str, str, str)  # (filename_stem, field, label, shot_id)

    # Line weight slider: integers 25–400 → displayed as value / 100
    _LW_MIN     = 25
    _LW_MAX     = 400
    _LW_DEFAULT = 100   # = 1.0

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._filename_stem = ""
        self._shot_id       = ""
        self._field         = ""
        self._label         = ""
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        dim  = f"color: {theme.TEXT_DIM}; font-size: {theme.BASE_PT}pt;"
        val  = f"color: {theme.TEXT};     font-size: {theme.BASE_PT}pt;"
        bold = f"color: {theme.TEXT};     font-size: {theme.BASE_PT + 1}pt; font-weight: bold;"

        # ── Header ─────────────────────────────────────────────────────
        # Helper: two-column key / value row
        def _row(label_text: str, value_widget: QWidget, parent_layout: QVBoxLayout) -> None:
            row = QHBoxLayout()
            lbl = QLabel(label_text + ":")
            lbl.setStyleSheet(dim)
            lbl.setFixedWidth(80)
            row.addWidget(lbl)
            row.addWidget(value_widget, 1)
            parent_layout.addLayout(row)

        def _val() -> QLabel:
            w = QLabel("—")
            w.setStyleSheet(val)
            w.setWordWrap(True)
            w.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            return w

        # ── Selection ──────────────────────────────────────────────────
        sel_group = QGroupBox("Selection")
        sel_lay   = QVBoxLayout(sel_group)
        sel_lay.setContentsMargins(8, 12, 8, 8)
        sel_lay.setSpacing(4)

        self._lbl_label = _val()
        self._lbl_field = _val()
        self._lbl_movie = _val()
        self._lbl_shot  = _val()
        self._lbl_shot.setStyleSheet(val + " font-family: monospace; font-size: 8pt;")
        _row("Label", self._lbl_label, sel_lay)
        _row("Field", self._lbl_field, sel_lay)
        _row("Movie", self._lbl_movie, sel_lay)
        _row("Shot",  self._lbl_shot,  sel_lay)
        layout.addWidget(sel_group)

        # ── Page Information ───────────────────────────────────────────
        page_group = QGroupBox("Page Information")
        page_lay   = QVBoxLayout(page_group)
        page_lay.setContentsMargins(8, 12, 8, 8)
        page_lay.setSpacing(4)

        self._lbl_dpi    = _val()
        self._lbl_page_w = _val()
        self._lbl_page_h = _val()
        _row("DPI",    self._lbl_dpi,    page_lay)
        _row("Width",  self._lbl_page_w, page_lay)
        _row("Height", self._lbl_page_h, page_lay)
        layout.addWidget(page_group)

        # ── Object Information ─────────────────────────────────────────
        obj_group = QGroupBox("Object Information")
        obj_lay   = QVBoxLayout(obj_group)
        obj_lay.setContentsMargins(8, 12, 8, 8)
        obj_lay.setSpacing(4)

        self._lbl_obj_w_px = _val()
        self._lbl_obj_h_px = _val()
        self._lbl_obj_w_mm = _val()
        self._lbl_obj_h_mm = _val()
        _row("Width (px)",  self._lbl_obj_w_px, obj_lay)
        _row("Height (px)", self._lbl_obj_h_px, obj_lay)
        _row("Width (mm)",  self._lbl_obj_w_mm, obj_lay)
        _row("Height (mm)", self._lbl_obj_h_mm, obj_lay)
        layout.addWidget(obj_group)

        # ── Line Weight ────────────────────────────────────────────────
        lw_group = QGroupBox("Line Weight")
        lw_lay   = QVBoxLayout(lw_group)
        lw_lay.setContentsMargins(8, 12, 8, 8)
        lw_lay.setSpacing(6)

        lw_row = QHBoxLayout()
        self._lw_slider = QSlider(Qt.Horizontal)
        self._lw_slider.setRange(self._LW_MIN, self._LW_MAX)
        self._lw_slider.setValue(self._LW_DEFAULT)
        self._lw_slider.setSingleStep(5)
        self._lw_slider.setPageStep(25)
        self._lw_slider.setTickInterval(75)
        self._lw_slider.setTickPosition(QSlider.TicksBelow)
        self._lw_slider.setFocusPolicy(Qt.NoFocus)
        lw_row.addWidget(self._lw_slider, 1)

        self._lw_val = QLabel("1.00")
        self._lw_val.setStyleSheet(val)
        self._lw_val.setFixedWidth(34)
        self._lw_val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lw_row.addWidget(self._lw_val)
        lw_lay.addLayout(lw_row)

        rng_row = QHBoxLayout()
        for txt, align in (("0.25", Qt.AlignLeft), ("4.00", Qt.AlignRight)):
            rl = QLabel(txt)
            rl.setStyleSheet(dim)
            rl.setAlignment(align)
            rng_row.addWidget(rl, 1)
        lw_lay.addLayout(rng_row)

        self._lw_slider.valueChanged.connect(self._on_lw_slider)
        layout.addWidget(lw_group)

        # ── Viewer buttons ──────────────────────────────────────────────
        btn_row = QHBoxLayout()
        self._shotlist_btn = QPushButton("Shotlist")
        self._shotlist_btn.setEnabled(False)
        self._shotlist_btn.clicked.connect(
            lambda: self.open_in_shotlist.emit(self._filename_stem, self._shot_id)
        )
        btn_row.addWidget(self._shotlist_btn)

        self._silhouette_btn = QPushButton("Silhouette")
        self._silhouette_btn.setEnabled(False)
        self._silhouette_btn.clicked.connect(
            lambda: self.open_in_silhouette.emit(
                self._filename_stem, self._field, self._label, self._shot_id
            )
        )
        btn_row.addWidget(self._silhouette_btn)
        layout.addLayout(btn_row)

        layout.addStretch()

    # ------------------------------------------------------------------

    def _on_lw_slider(self, value: int) -> None:
        lw = value / 100.0
        self._lw_val.setText(f"{lw:.2f}")
        self.line_weight_changed.emit(lw)

    def current_line_weight(self) -> float:
        return self._lw_slider.value() / 100.0

    def set_line_weight(self, value: float) -> None:
        self._lw_slider.blockSignals(True)
        self._lw_slider.setValue(
            max(self._LW_MIN, min(self._LW_MAX, int(round(value * 100))))
        )
        self._lw_slider.blockSignals(False)
        self._lw_val.setText(f"{value:.2f}")

    def load_layer(
        self,
        layer: dict,
        page_pt_w: float = 0.0,
        page_pt_h: float = 0.0,
        movie: str = "",
    ) -> None:
        """Populate inspector from a placed Image/Engraving layer dict."""
        self._lbl_label.setText(layer.get("label") or layer.get("name") or "—")
        self._lbl_field.setText(layer.get("field") or "—")
        self._lbl_movie.setText(movie or "—")
        self._lbl_shot.setText(layer.get("shot_id") or "—")

        try:
            from services.engraving_generate import build_size_context
            sc = build_size_context(
                preprocessing_size=layer.get("preprocessing_size") or [],
                preprocess_dpi=layer.get("preprocess_dpi") or 0,
                page_pt_w=page_pt_w,
                page_pt_h=page_pt_h,
                width_frac=layer.get("width", 0.0),
                height_frac=layer.get("height", 0.0),
            )
        except Exception:
            sc = {}

        def _mm(key: str) -> str:
            v = sc.get(key, "")
            return f"{v} mm" if v else "—"

        def _px(key: str) -> str:
            v = sc.get(key, "")
            return f"{v} px" if v else "—"

        self._lbl_dpi.setText(sc.get("page_dpi") or "—")
        self._lbl_page_w.setText(_mm("page_width_mm"))
        self._lbl_page_h.setText(_mm("page_height_mm"))
        self._lbl_obj_w_px.setText(_px("object_width_px"))
        self._lbl_obj_h_px.setText(_px("object_height_px"))
        self._lbl_obj_w_mm.setText(_mm("object_width_mm"))
        self._lbl_obj_h_mm.setText(_mm("object_height_mm"))

        self.set_line_weight(float(layer.get("line_weight") or 1.0))
        self._filename_stem = layer.get("filename_stem", "")
        self._shot_id       = layer.get("shot_id", "")
        self._field         = layer.get("field", "")
        self._label         = layer.get("label") or layer.get("name") or ""
        self._shotlist_btn.setEnabled(bool(self._filename_stem))
        self._silhouette_btn.setEnabled(bool(self._filename_stem))

    def load_record(self, record: dict) -> None:
        """Populate inspector from a catalog record (browser thumbnail selection)."""
        self._lbl_label.setText(record.get("label") or "—")
        self._lbl_field.setText(record.get("field") or "—")
        self._lbl_movie.setText(record.get("filename_stem") or "—")
        self._lbl_shot.setText(record.get("shot_id") or "—")
        for w in (
            self._lbl_dpi, self._lbl_page_w, self._lbl_page_h,
            self._lbl_obj_w_px, self._lbl_obj_h_px,
            self._lbl_obj_w_mm, self._lbl_obj_h_mm,
        ):
            w.setText("—")
        self.set_line_weight(float(record.get("line_weight") or 1.0))
        self._filename_stem = record.get("filename_stem", "")
        self._shot_id       = record.get("shot_id", "")
        self._field         = record.get("field", "")
        self._label         = record.get("label") or ""
        self._shotlist_btn.setEnabled(bool(self._filename_stem))
        self._silhouette_btn.setEnabled(bool(self._filename_stem))

    def clear(self) -> None:
        """Reset all display fields to '—'."""
        for w in (
            self._lbl_label, self._lbl_field, self._lbl_movie, self._lbl_shot,
            self._lbl_dpi, self._lbl_page_w, self._lbl_page_h,
            self._lbl_obj_w_px, self._lbl_obj_h_px,
            self._lbl_obj_w_mm, self._lbl_obj_h_mm,
        ):
            w.setText("—")
        self._lw_slider.blockSignals(True)
        self._lw_slider.setValue(self._LW_DEFAULT)
        self._lw_slider.blockSignals(False)
        self._lw_val.setText("1.00")
        self._filename_stem = ""
        self._shot_id       = ""
        self._field         = ""
        self._label         = ""
        self._shotlist_btn.setEnabled(False)
        self._silhouette_btn.setEnabled(False)


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

        # Active generation workers: engraving_id → _EngravingWorker
        self._engraving_workers: dict = {}

        # Inspector state
        self._inspector_layer:  Optional[dict] = None
        self._inspector_record: Optional[dict] = None

        self._build_ui()
        self._load_all_books()
        restore_window_geometry(self, "window_book")
        # Grab navigation keys regardless of which child widget has focus
        QApplication.instance().installEventFilter(self)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # Ensure the spread renders on first show (the initial _do_render call in
        # __init__ fires before the widget has a valid size, so we retry here).
        QTimer.singleShot(0, self._spread_view._do_render)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._sil_browser._stop_loader()
        self._sil_browser._stop_eng_loader()
        self._save_current_layers()
        self._close_doc()
        save_window_geometry(self, "window_book")
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # UI construction

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)

        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        splitter = GripSplitter(Qt.Horizontal)
        outer.addWidget(splitter, stretch=1)

        # ── LEFT: spread view + page bar ─────────────────────────────
        left_col = QWidget()
        left_col.setStyleSheet(f"background: {theme.PANEL_BG};")
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
        self._overlay.text_sel_committed.connect(self._on_text_sel_committed)
        self._overlay.text_sel_removed.connect(self._on_text_sel_removed)
        self._overlay.engraving_requested.connect(self._on_engraving_requested)
        self._overlay.silhouette_drop_requested.connect(self._on_silhouette_drop)

        self._page_bar = _PageBar()
        self._page_bar.jumped.connect(self._go_spread)
        left_layout.addWidget(self._page_bar)

        splitter.addWidget(left_col)

        # ── MIDDLE: Silhouette browser sidebar ────────────────────────
        self._sil_browser = _SilhouetteBrowserPanel(self._project_path)
        self._sil_browser.silhouette_insert_requested.connect(self._insert_silhouette)
        self._sil_browser.engraving_delete_requested.connect(self._on_engraving_delete_requested)
        self._sil_browser.thumbnail_selected.connect(self._on_browser_thumbnail_selected)
        self._sil_browser.engraving_selected.connect(self._on_browser_engraving_selected)
        splitter.addWidget(self._sil_browser)

        # ── RIGHT: control panel (Book / Tools / Layers) ──────────────
        panel = self._build_control_panel()
        self._inspector.line_weight_changed.connect(self._on_inspector_lw_changed)
        self._inspector.open_in_shotlist.connect(self._on_open_in_shotlist)
        self._inspector.open_in_silhouette.connect(self._on_open_in_silhouette)
        splitter.addWidget(panel)
        splitter.setStretchFactor(0, 1)   # canvas gets all extra space
        splitter.setStretchFactor(1, 0)   # silhouette browser: fixed
        splitter.setStretchFactor(2, 0)   # control panel: fixed
        splitter.setSizes([10000, _BROWSER_PANEL_W, _PANEL_WIDTH])

        self.setMinimumSize(700, 480)
        self.resize(1500, 800)

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

        # ── QStackedWidget: page 0 = default, page 1 = inspector ──────
        self._right_stack = QStackedWidget()
        self._right_stack.addWidget(scroll)          # page 0: default

        inspector_scroll = QScrollArea()
        inspector_scroll.setFocusPolicy(Qt.NoFocus)
        inspector_scroll.setWidgetResizable(True)
        inspector_scroll.setFrameShape(QFrame.NoFrame)
        inspector_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inspector_scroll.setStyleSheet(
            f"QScrollArea {{ background: {theme.PANEL_BG}; border: none; }}"
        )
        self._inspector = _InspectorPanel()
        self._inspector.setStyleSheet(_PANEL_STYLESHEET)
        inspector_scroll.setWidget(self._inspector)
        self._right_stack.addWidget(inspector_scroll)  # page 1: inspector

        outer_layout.addWidget(self._right_stack)

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

        self._text_btn = QPushButton()
        self._text_btn.setIcon(_svg_icon("text", ICON_SIZE))
        self._text_btn.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self._text_btn.setCheckable(True)
        self._text_btn.setFixedSize(BTN_SIZE, BTN_SIZE)
        self._text_btn.setToolTip("Text tool — drag to select PDF text")
        self._text_btn.clicked.connect(lambda checked: self._set_tool(_TOOL_TEXT if checked else _TOOL_NONE))
        tool_row.addWidget(self._text_btn)

        _mask_pair = QWidget()
        _mask_pair.setFixedSize(BTN_SIZE * 2, BTN_SIZE)
        _mask_pair_lay = QHBoxLayout(_mask_pair)
        _mask_pair_lay.setContentsMargins(0, 0, 0, 0)
        _mask_pair_lay.setSpacing(0)

        _mask_btn_style = (
            f"QPushButton {{ background-color: {theme.BTN_BG}; border: none;"
            f" padding: 0px; }}"
            f" QPushButton:hover    {{ background-color: {theme.BTN_HOVER}; }}"
            f" QPushButton:pressed  {{ background-color: {theme.BTN_PRESSED}; }}"
            f" QPushButton:checked  {{ background-color: {theme.ACCENT}; }}"
            f" QPushButton:disabled {{ color: {theme.TEXT_DIM};"
            f" background-color: {theme.BTN_BG}; }}"
        )

        self._mask_left_btn = QPushButton()
        self._mask_left_btn.setIcon(_svg_icon("mask-square", ICON_SIZE))
        self._mask_left_btn.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self._mask_left_btn.setCheckable(True)
        self._mask_left_btn.setFixedSize(BTN_SIZE, BTN_SIZE)
        self._mask_left_btn.setToolTip("Mask left page — show only selected text")
        self._mask_left_btn.setStyleSheet(
            _mask_btn_style + f" QPushButton {{ border-radius: 0px;"
            f" border-top-left-radius: 3px; border-bottom-left-radius: 3px; }}"
        )
        self._mask_left_btn.clicked.connect(self._toggle_mask_left)
        _mask_pair_lay.addWidget(self._mask_left_btn)

        self._mask_right_btn = QPushButton()
        self._mask_right_btn.setIcon(_svg_icon("mask-square", ICON_SIZE))
        self._mask_right_btn.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self._mask_right_btn.setCheckable(True)
        self._mask_right_btn.setFixedSize(BTN_SIZE, BTN_SIZE)
        self._mask_right_btn.setToolTip("Mask right page — show only selected text")
        self._mask_right_btn.setStyleSheet(
            _mask_btn_style + f" QPushButton {{ border-radius: 0px;"
            f" border-top-right-radius: 3px; border-bottom-right-radius: 3px; }}"
        )
        self._mask_right_btn.clicked.connect(self._toggle_mask_right)
        _mask_pair_lay.addWidget(self._mask_right_btn)

        tool_row.addWidget(_mask_pair)

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

        self._show_outlines_chk = QCheckBox("Handles")
        self._show_outlines_chk.setChecked(True)
        self._show_outlines_chk.setFocusPolicy(Qt.NoFocus)
        self._show_outlines_chk.toggled.connect(
            lambda checked: self._overlay.set_show_outlines(checked)
        )
        tools_layout.addWidget(self._show_outlines_chk)

        self._text_vis_chk = QCheckBox("Selections")
        self._text_vis_chk.setChecked(True)
        self._text_vis_chk.setFocusPolicy(Qt.NoFocus)
        self._text_vis_chk.toggled.connect(
            lambda checked: self._overlay.set_text_sels_visible(checked)
        )
        tools_layout.addWidget(self._text_vis_chk)
        layout.addWidget(tools_group)

        # ── Layers group ──────────────────────────────────────────────
        layers_group = QGroupBox("Layers")
        layers_group_layout = QVBoxLayout(layers_group)
        layers_group_layout.setContentsMargins(6, 6, 6, 6)
        layers_group_layout.setSpacing(4)

        # Eye (visibility) button in the group header area
        self._layers_visible_btn = QPushButton()
        self._layers_visible_btn.setCheckable(True)
        self._layers_visible_btn.setChecked(True)
        self._layers_visible_btn.setFixedSize(20, 20)
        self._layers_visible_btn.setFocusPolicy(Qt.NoFocus)
        self._layers_visible_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; }"
        )
        self._layers_visible_btn.setIcon(_svg_icon("eye-solid", 14, theme.ACCENT))
        self._layers_visible_btn.setIconSize(QSize(14, 14))
        self._layers_visible_btn.toggled.connect(self._on_layers_visible_toggled)
        layers_group_layout.addWidget(self._layers_visible_btn, 0, Qt.AlignRight)

        self._layer_panel = _LayerPanel()
        self._layer_panel.setMinimumHeight(100)
        self._layer_panel.layer_selected.connect(self._on_panel_layer_selected)
        self._layer_panel.layer_deleted.connect(self._on_panel_layer_deleted)
        self._layer_panel.layer_renamed.connect(self._on_panel_layer_renamed)
        self._layer_panel.layers_reordered.connect(self._on_panel_layers_reordered)
        self._layer_panel.layer_visibility_toggled.connect(self._on_panel_layer_visibility)
        self._layer_panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layers_group_layout.addWidget(self._layer_panel, stretch=1)

        layers_group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(layers_group, stretch=1)

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
            self._overlay.set_book_dir(None)
            self._spread_view.set_book_dir(None)
            return

        from data.book import book_dir
        pdf_path = book_dir(self._project_path, slug) / pdf_rel

        if not pdf_path.exists():
            self._pages_label.setText("PDF file missing.")
            self._import_btn.setText("Import")
            self._import_btn.setToolTip("Import a PDF into this book")
            self._persist_current(slug)
            self._overlay.set_book_dir(book_dir(self._project_path, slug))
            self._spread_view.set_book_dir(book_dir(self._project_path, slug))
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
        # Set book dir first so image-layer resolution is available during load
        self._overlay.set_book_dir(book_dir(self._project_path, slug))
        self._spread_view.set_book_dir(book_dir(self._project_path, slug))
        # Load layers for this book (also rebuilds the Engravings tab)
        self._load_book_layers(slug)
        # Load text selections for this book
        self._load_book_text_sels(slug)
        # Load mask state for this book
        self._load_book_mask(slug)

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
        self._sync_mask_buttons()

    def _close_doc(self) -> None:
        """Close the open fitz document and reset the spread view."""
        self._save_current_layers()
        self._save_current_text_sels()
        self._overlay.set_layers([])
        self._overlay.set_text_selections([])
        self._layer_panel.remove_all()
        self._spread_view.set_text_sels_for_mask([])
        self._spread_view.set_masked_pages(set())
        self._mask_left_btn.setChecked(False)
        self._mask_right_btn.setChecked(False)
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
        self._text_btn.setChecked(tool == _TOOL_TEXT)
        self._cut_btn.setChecked(tool == _TOOL_CUT)
        self._erase_btn.setChecked(tool == _TOOL_ERASE)
        # Cancel in-progress WIP when switching away from CUT
        if tool != _TOOL_CUT:
            self._overlay.cancel_wip()

    def _toggle_mask_left(self, checked: bool) -> None:
        left_i = self._spread_view._left_i
        if left_i is None:
            self._mask_left_btn.setChecked(False)
            return
        self._spread_view.set_page_masked(left_i, checked)
        self._save_current_mask()

    def _toggle_mask_right(self, checked: bool) -> None:
        right_i = self._spread_view._right_i
        if right_i is None:
            self._mask_right_btn.setChecked(False)
            return
        self._spread_view.set_page_masked(right_i, checked)
        self._save_current_mask()

    def _sync_mask_buttons(self) -> None:
        """Update mask button checked states to match current spread pages."""
        left_i  = self._spread_view._left_i
        right_i = self._spread_view._right_i
        masked  = self._spread_view._masked_pages
        self._mask_left_btn.setEnabled(left_i is not None)
        self._mask_right_btn.setEnabled(right_i is not None)
        self._mask_left_btn.setChecked(left_i is not None and left_i in masked)
        self._mask_right_btn.setChecked(right_i is not None and right_i in masked)

    def _on_layers_visible_toggled(self, checked: bool) -> None:
        icon_name = "eye-solid" if checked else "eye-closed"
        color = theme.ACCENT if checked else theme.TEXT_DIM
        self._layers_visible_btn.setIcon(_svg_icon(icon_name, 14, color))
        self._overlay.set_layers_visible(checked)
        self._spread_view.set_layers_visible(checked)

    # ------------------------------------------------------------------
    # Engraving workflow (page-centric)

    def _on_engraving_requested(self, source_layer: dict) -> None:
        """Create an engraving child layer and run page-aware preprocessing."""
        now        = datetime.datetime.now()
        timestamp  = now.strftime("%Y%m%d_%H%M%S")
        label      = source_layer.get("name", "engraving").strip() or "engraving"
        safe_label = label.lower().replace(" ", "_")
        name       = f"{safe_label}_{timestamp}"
        eng_id     = f"eng_{uuid.uuid4().hex[:8]}"

        # Resolve source PNG path for preprocessing and thumbnail
        source_rel = source_layer.get("source", "")
        source_png = ""
        book_dir   = self._overlay._book_dir
        if book_dir and source_rel:
            candidate = book_dir / source_rel
            if candidate.exists():
                source_png = str(candidate)

        # ------------------------------------------------------------------
        # Page-aware preprocessing
        # ------------------------------------------------------------------
        preprocessing_path: str   = ""
        preprocessing_size: list  = []
        preprocess_cache_key: str = ""
        preprocess_dpi: int       = 0
        page_pt_w: float          = 0.0
        page_pt_h: float          = 0.0

        if source_png and self._doc is not None:
            try:
                page_idx = source_layer["page"]
                pdf_page = self._doc[page_idx]
                page_pt_w = float(pdf_page.rect.width)
                page_pt_h = float(pdf_page.rect.height)

                from data.book import book_dir as _book_dir_fn
                eng_cache_dir = _book_dir_fn(self._project_path, self._slug) / "engravings"

                from services.engraving_preprocess import preprocess_engraving_source
                pre = preprocess_engraving_source(
                    source_png=source_png,
                    engraving_id=eng_id,
                    parent_layer_id=source_layer["id"],
                    source_silhouette_id=source_rel,
                    page_idx=page_idx,
                    x=source_layer["x"],
                    y=source_layer["y"],
                    width=source_layer["width"],
                    height=source_layer["height"],
                    rotation=source_layer.get("rotation", 0.0),
                    flip_h=source_layer.get("flip_h", False),
                    flip_v=source_layer.get("flip_v", False),
                    page_pt_w=page_pt_w,
                    page_pt_h=page_pt_h,
                    cache_dir=eng_cache_dir,
                )
                preprocessing_path    = pre["preprocessing_path"]
                preprocessing_size    = pre["preprocessing_size"]
                preprocess_cache_key  = pre["cache_key"]
                preprocess_dpi        = pre.get("preprocess_dpi", 0)
            except Exception as _exc:
                # Preprocessing failure is non-fatal; the layer is still created.
                import traceback
                traceback.print_exc()

        # ------------------------------------------------------------------
        # Build the engraving layer
        # ------------------------------------------------------------------
        eng_layer = {
            "id":                    eng_id,
            "type":                  "Image",
            "layer_subtype":         "Engraving",
            "name":                  name,
            "parent_layer_id":       source_layer["id"],
            "source_silhouette_id":  source_rel,
            "source":                source_rel,
            "page":                  source_layer["page"],
            "spread":                source_layer["spread"],
            "x":                     source_layer["x"],
            "y":                     source_layer["y"],
            "width":                 source_layer["width"],
            "height":                source_layer["height"],
            "rotation":              source_layer.get("rotation", 0.0),
            "flip_h":                source_layer.get("flip_h", False),
            "flip_v":                source_layer.get("flip_v", False),
            "z_index":               len(self._overlay.current_layers()),
            "visible":               True,
            "created":               now.isoformat(),
            "line_weight":           float(source_layer.get("line_weight") or 1.0),
            "label":                 source_layer.get("label", ""),
            "field":                 source_layer.get("field", ""),
            "shot_id":               source_layer.get("shot_id", ""),
            "filename_stem":         source_layer.get("filename_stem", ""),
            "description":           source_layer.get("description", ""),
            # Preprocessing provenance:
            "preprocessing_path":    preprocessing_path,
            "preprocessing_size":    preprocessing_size,
            "preprocess_cache_key":  preprocess_cache_key,
            "preprocess_dpi":        preprocess_dpi,
            # Future AI generation fields (reserved):
            "model":          None,
            "preset":         None,
            "line_density":   None,
            "line_thickness": None,
            "white_space":    None,
            "output_png":     None,
        }

        # Hide parent silhouette — keep it in the document, record child link
        parent = self._overlay._layer_by_id(source_layer["id"])
        if parent is not None:
            parent["visible"] = False
            parent.setdefault("child_layers", []).append(eng_id)

        # Add engraving layer and select it
        self._overlay._layers.append(eng_layer)
        self._overlay._sel_id = eng_id
        self._overlay._sel_pt = None
        self._overlay.update()

        # Persist layers and rebuild the layer panel (so hidden parent dims)
        self._on_layer_committed(eng_layer)
        self._refresh_layer_panel()

        # Add to the Engravings tab and switch to it
        entry = {
            "name":                  name,
            "layer_id":              eng_id,
            "parent_layer_id":       source_layer["id"],
            "source_silhouette_id":  source_rel,
            "source_png":            source_png,
            "page":                  source_layer["page"],
            "width":                 source_layer["width"],
            "height":                source_layer["height"],
            "created":               now.isoformat(),
            # Preprocessing result:
            "preprocessing_path":    preprocessing_path,
            "preprocessing_size":    preprocessing_size,
            "preprocess_cache_key":  preprocess_cache_key,
            "preprocess_dpi":        preprocess_dpi,
            "line_weight":           float(source_layer.get("line_weight") or 1.0),
            "label":                 source_layer.get("label", ""),
            "field":                 source_layer.get("field", ""),
            "shot_id":               source_layer.get("shot_id", ""),
            "filename_stem":         source_layer.get("filename_stem", ""),
            "description":           source_layer.get("description", ""),
            # Generation fields — populated when worker finishes:
            "model":          None,
            "preset":         None,
            "line_density":   None,
            "line_thickness": None,
            "white_space":    None,
            "output_png":     None,
        }
        self._sil_browser.add_engraving(entry)
        self._sil_browser._tabs.setCurrentIndex(1)

        # ------------------------------------------------------------------
        # Launch generation worker if preprocessing produced an asset
        # ------------------------------------------------------------------
        if preprocessing_path and preprocessing_size:
            # ------------------------------------------------------------------
            # Build silhouette context for $variable prompt template expansion.
            # Resolve movie title from metadata if filename_stem is available.
            # ------------------------------------------------------------------
            filename_stem = source_layer.get("filename_stem", "")
            movie = ""
            if filename_stem:
                try:
                    from data.metadata import get_metadata
                    metas = get_metadata(self._project_path, filename_stem)
                    if metas:
                        m = metas[0]
                        title = m.get("title", "")
                        year  = m.get("year", "")
                        movie = f"{title} ({year})" if title and year else title
                except Exception:
                    pass

            context = {
                "label":       source_layer.get("label", source_layer.get("name", "")),
                "field":       source_layer.get("field", ""),
                "movie":       movie,
                "shot_id":     source_layer.get("shot_id", ""),
                "description": source_layer.get("description", ""),
                "line_weight": str(source_layer.get("line_weight", 1.0)),
            }

            # Merge size-aware variables (v4 prompt support)
            try:
                from services.engraving_generate import build_size_context
                size_ctx = build_size_context(
                    preprocessing_size=preprocessing_size,
                    preprocess_dpi=preprocess_dpi,
                    page_pt_w=page_pt_w,
                    page_pt_h=page_pt_h,
                    width_frac=source_layer["width"],
                    height_frac=source_layer["height"],
                )
                context.update(size_ctx)
            except Exception:
                pass

            self._start_engraving_worker(eng_id, preprocessing_path, preprocessing_size, context=context)

    def _start_engraving_worker(
        self,
        eng_id: str,
        preprocessing_path: str,
        preprocessing_size: list,
        context: dict | None = None,
    ) -> None:
        """Spawn a background generation worker for *eng_id*."""
        import json as _json
        from data.book import book_dir as _book_dir_fn
        eng_cache_dir = _book_dir_fn(self._project_path, self._slug) / "engravings"

        context_json = _json.dumps(context, ensure_ascii=False) if context else ""

        worker = _EngravingWorker(
            project_path=self._project_path,
            engraving_id=eng_id,
            preprocessing_path=preprocessing_path,
            preprocessing_size=preprocessing_size,
            cache_dir=eng_cache_dir,
            context_json=context_json,
            parent=self,
        )
        worker.finished.connect(self._on_engraving_generated)
        worker.failed.connect(self._on_engraving_failed)
        self._engraving_workers[eng_id] = worker

        # Show spinner on the page overlay
        self._overlay.start_spinner_for(eng_id)

        worker.start()

    def _on_engraving_generated(self, eng_id: str, output_png: str, metadata: dict) -> None:
        """Called on the GUI thread when generation succeeds."""
        # Stop spinner
        self._overlay.stop_spinner_for(eng_id)

        # Update the layer: set output_png so the overlay draws it
        layer = self._overlay._layer_by_id(eng_id)
        if layer is not None:
            layer["output_png"]          = output_png
            layer["model"]               = metadata.get("model")
            layer["generator"]           = metadata.get("generator")
            layer["line_weight"]         = metadata.get("line_weight", 1.0)
            layer["seed"]                = metadata.get("seed")
            layer["num_inference_steps"] = metadata.get("num_inference_steps")
            layer["guidance_scale"]      = metadata.get("guidance_scale")
            layer["binary_threshold"]    = metadata.get("binary_threshold")
            layer["prompt_filename"]     = metadata.get("prompt_filename")
            # Evict the pixmap cache so the new file is loaded
            self._overlay._pixmap_cache.pop(output_png, None)
            self._overlay.update()
            self._on_layer_committed(layer)

        # Update the Engravings tab entry
        for entry in self._sil_browser._engravings:
            if entry.get("layer_id") == eng_id:
                entry["output_png"]          = output_png
                entry["model"]               = metadata.get("model")
                entry["generator"]           = metadata.get("generator")
                entry["line_weight"]         = metadata.get("line_weight", 1.0)
                entry["seed"]                = metadata.get("seed")
                entry["num_inference_steps"] = metadata.get("num_inference_steps")
                entry["guidance_scale"]      = metadata.get("guidance_scale")
                entry["binary_threshold"]    = metadata.get("binary_threshold")
                entry["prompt_filename"]     = metadata.get("prompt_filename")
                break
        self._sil_browser._refresh_engravings_tab()

        # Also refresh the spread view (it renders separately from overlay)
        self._spread_view._cache.clear()
        self._spread_view._reveal_cache.clear()
        self._spread_view._do_render()

        self._engraving_workers.pop(eng_id, None)

    def _on_engraving_failed(self, eng_id: str, error_msg: str) -> None:
        """Called on the GUI thread when generation fails."""
        self._overlay.mark_generate_error(eng_id)
        self._overlay.update()
        self._engraving_workers.pop(eng_id, None)
        print(f"[engraving] generation failed for {eng_id}:\n{error_msg}")

    def _on_engraving_delete_requested(self, layer_id: str) -> None:
        """Delete an engraving: confirm if the layer exists on any page."""
        # Find the engraving entry in the browser list
        entry = next(
            (e for e in self._sil_browser._engravings if e.get("layer_id") == layer_id),
            None,
        )
        name = entry.get("name", layer_id) if entry else layer_id

        # Check whether the layer is currently placed on any page
        eng_layer = self._overlay._layer_by_id(layer_id)
        if eng_layer is not None:
            page = eng_layer.get("page")
            page_label = f"page {page + 1}" if page is not None else "a page"
            reply = QMessageBox.question(
                self,
                "Delete Engraving",
                f"The engraving \"{name}\" is placed on {page_label}.\n\n"
                "Deleting it will also remove the layer from that page.\n\n"
                "Continue?",
                QMessageBox.Ok | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if reply != QMessageBox.Ok:
                return

            # Remove the layer from the overlay and layer panel
            self._overlay._layers.remove(eng_layer)
            if self._overlay._sel_id == layer_id:
                self._overlay._sel_id = None
                self._overlay._sel_pt = None
            self._overlay.update()
            self._layer_panel.remove_layer(layer_id)

            # Remove child_layers reference from the parent silhouette
            # and restore its visibility if it has no remaining children.
            parent_id = eng_layer.get("parent_layer_id")
            if parent_id:
                parent = self._overlay._layer_by_id(parent_id)
                if parent is not None:
                    child_list = parent.get("child_layers", [])
                    if layer_id in child_list:
                        child_list.remove(layer_id)
                    if not child_list:
                        parent["visible"] = True

            self._save_current_layers()

        # Remove from the Engravings tab list and refresh
        self._sil_browser._engravings = [
            e for e in self._sil_browser._engravings if e.get("layer_id") != layer_id
        ]
        self._sil_browser._refresh_engravings_tab()

        # Delete all files on disk belonging to this engraving ID
        if entry is not None:
            self._delete_engraving_files(layer_id)

    def _delete_engraving_files(self, eng_id: str) -> None:
        """Remove all cached files for *eng_id* from the engravings directory."""
        try:
            from data.book import book_dir as _book_dir_fn
            eng_cache_dir = _book_dir_fn(self._project_path, self._slug) / "engravings"
            if not eng_cache_dir.is_dir():
                return
            for f in eng_cache_dir.iterdir():
                if f.name.startswith(f"{eng_id}_"):
                    try:
                        f.unlink()
                    except OSError:
                        pass
        except Exception:
            pass

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
        self._rebuild_engravings_from_layers(layers)

    def _rebuild_engravings_from_layers(self, layers: list) -> None:
        """Reconstruct the Engravings tab from persisted Engraving layers.

        Primary source: Engraving layers in *layers* (loaded from layers.json).
        Recovery source: ``*_generation.json`` files in the book's engravings/
        directory.  Any engraving found on disk whose ID is not already
        represented by a layer is added as a thumbnail-only entry so previously
        generated assets are never silently lost between sessions.

        Called every time a book is (re)loaded.
        """
        book_d = self._overlay._book_dir

        entries = []
        seen_ids: set = set()

        # ── Primary: rebuild from Engraving layers ─────────────────────────
        for layer in layers:
            if layer.get("layer_subtype") != "Engraving":
                continue

            eng_id = layer["id"]
            seen_ids.add(eng_id)

            source_rel = layer.get("source_silhouette_id") or layer.get("source", "")
            source_png = ""
            if book_d and source_rel:
                candidate = book_d / source_rel
                if candidate.exists():
                    source_png = str(candidate)

            entries.append({
                "name":                  layer.get("name", ""),
                "layer_id":              eng_id,
                "parent_layer_id":       layer.get("parent_layer_id", ""),
                "source_silhouette_id":  source_rel,
                "source_png":            source_png,
                "page":                  layer.get("page"),
                "width":                 layer.get("width"),
                "height":                layer.get("height"),
                "created":               layer.get("created", ""),
                "preprocessing_path":    layer.get("preprocessing_path", ""),
                "preprocessing_size":    layer.get("preprocessing_size", []),
                "preprocess_cache_key":  layer.get("preprocess_cache_key", ""),
                "preprocess_dpi":        layer.get("preprocess_dpi", 0),
                "model":                 layer.get("model"),
                "preset":                layer.get("preset"),
                "line_density":          layer.get("line_density"),
                "line_thickness":        layer.get("line_thickness"),
                "white_space":           layer.get("white_space"),
                "output_png":            layer.get("output_png"),
                "line_weight":           float(layer.get("line_weight") or 1.0),
                "label":                 layer.get("label", ""),
                "field":                 layer.get("field", ""),
                "shot_id":               layer.get("shot_id", ""),
                "filename_stem":         layer.get("filename_stem", ""),
                "description":           layer.get("description", ""),
            })

        # ── Recovery: scan engravings directory for orphaned generation JSON ─
        if book_d:
            eng_dir = book_d / "engravings"
            if eng_dir.is_dir():
                for gen_json in sorted(eng_dir.glob("*_generation.json")):
                    # Derive eng_id from filename: "<eng_id>_generation.json"
                    eng_id = gen_json.stem.replace("_generation", "")
                    if eng_id in seen_ids:
                        continue  # already represented by a layer entry
                    try:
                        meta = json.loads(gen_json.read_text(encoding="utf-8"))
                    except Exception:
                        continue

                    output_png       = meta.get("output_png", "")
                    preprocessing_path = meta.get("preprocessing_path", "")

                    # Load preprocessing size from sidecar JSON if available
                    preprocessing_size = []
                    pre_json = eng_dir / f"{eng_id}_preprocess_v1.json"
                    if pre_json.exists():
                        try:
                            pre = json.loads(pre_json.read_text(encoding="utf-8"))
                            preprocessing_size = pre.get("preprocessing_size", [])
                        except Exception:
                            pass

                    entries.append({
                        "name":               eng_id,
                        "layer_id":           eng_id,
                        "parent_layer_id":    "",
                        "source_silhouette_id": "",
                        "source_png":         "",
                        "page":               None,
                        "width":              None,
                        "height":             None,
                        "created":            "",
                        "preprocessing_path": preprocessing_path,
                        "preprocessing_size": preprocessing_size,
                        "preprocess_cache_key": "",
                        "preprocess_dpi":     0,
                        "model":              meta.get("model"),
                        "preset":             None,
                        "line_density":       None,
                        "line_thickness":     None,
                        "white_space":        None,
                        "output_png":         output_png,
                        "line_weight":        float(meta.get("line_weight") or 1.0),
                    })

        self._sil_browser.set_engravings(entries)

    def _save_current_layers(self) -> None:
        if not self._slug:
            return
        layers = self._overlay.current_layers()
        try:
            _save_layers(self._project_path, self._slug, layers)
        except Exception:
            pass
        self._spread_view.set_layers(layers)

    def _load_book_text_sels(self, slug: str) -> None:
        sels = _load_text_sels(self._project_path, slug)
        self._overlay.set_text_selections(sels)
        self._spread_view.set_text_sels_for_mask(sels)

    def _save_current_text_sels(self) -> None:
        if not self._slug:
            return
        sels = self._overlay.current_text_sels()
        try:
            _save_text_sels(self._project_path, self._slug, sels)
        except Exception:
            pass

    def _load_book_mask(self, slug: str) -> None:
        masked = _load_mask(self._project_path, slug)
        self._spread_view.set_masked_pages(masked)
        self._sync_mask_buttons()

    def _save_current_mask(self) -> None:
        if not self._slug:
            return
        try:
            _save_mask(self._project_path, self._slug, self._spread_view._masked_pages)
        except Exception:
            pass

    def _on_text_sel_committed(self, sel: dict) -> None:
        self._save_current_text_sels()
        self._spread_view.set_text_sels_for_mask(self._overlay.current_text_sels())

    def _on_text_sel_removed(self, sid: str) -> None:  # noqa: ARG002
        self._save_current_text_sels()
        self._spread_view.set_text_sels_for_mask(self._overlay.current_text_sels())

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
        if lid:
            layer = self._overlay._layer_by_id(lid)
            if layer and layer.get("type") == "Image":
                self._show_inspector(layer=layer)
                return
        self._hide_inspector()

    # ------------------------------------------------------------------
    # Inspector

    def _show_inspector(
        self,
        layer: Optional[dict] = None,
        record: Optional[dict] = None,
    ) -> None:
        """Switch the right panel to Inspector mode."""
        if layer is not None:
            page_pt_w, page_pt_h = 0.0, 0.0
            if self._doc is not None:
                try:
                    pdf_page  = self._doc[layer["page"]]
                    page_pt_w = float(pdf_page.rect.width)
                    page_pt_h = float(pdf_page.rect.height)
                except Exception:
                    pass
            movie = ""
            filename_stem = layer.get("filename_stem", "")
            if filename_stem:
                try:
                    from data.metadata import get_metadata
                    metas = get_metadata(self._project_path, filename_stem)
                    if metas:
                        m     = metas[0]
                        title = m.get("title", "")
                        year  = m.get("year", "")
                        movie = f"{title} ({year})" if title and year else title
                except Exception:
                    pass
            self._inspector.load_layer(layer, page_pt_w, page_pt_h, movie)
            self._inspector_layer  = layer
            self._inspector_record = None
        elif record is not None:
            self._inspector.load_record(record)
            self._inspector_layer  = None
            self._inspector_record = record
        else:
            self._hide_inspector()
            return
        self._right_stack.setCurrentIndex(1)

    def _hide_inspector(self) -> None:
        """Return the right panel to the default state."""
        self._inspector_layer  = None
        self._inspector_record = None
        self._inspector.clear()
        self._sil_browser.clear_selection()
        self._right_stack.setCurrentIndex(0)

    def _on_browser_engraving_selected(self, entry: dict) -> None:
        """Show Inspector when an engraving browser thumbnail is single-clicked."""
        layer_id = entry.get("layer_id")
        layer = self._overlay._layer_by_id(layer_id) if layer_id else None
        if layer is not None:
            self._show_inspector(layer=layer)
        else:
            self._show_inspector(record=entry)

    def _on_browser_thumbnail_selected(self, record: dict) -> None:
        """Show Inspector when a browser thumbnail is single-clicked."""
        if self._overlay._sel_id is not None:
            self._overlay._sel_id = None
            self._overlay._sel_pt = None
            self._overlay.update()
            self._layer_panel.select_layer(None)
        self._show_inspector(record=record)

    def _on_open_in_silhouette(self, filename_stem: str, field: str, label: str = "", shot_id: str = "") -> None:
        """Open the Silhouette Visualizer filtered to the film, field, label and shot."""
        if not filename_stem:
            return
        try:
            from visualizers.silhouette_visualizer import open_at_silhouette
            open_at_silhouette(
                self._project_path,
                filename_stem=filename_stem,
                field=field or None,
                label=label or None,
                shot_id=shot_id or None,
            )
        except Exception:
            import traceback
            traceback.print_exc()

    def _on_open_in_shotlist(self, filename_stem: str, shot_id: str) -> None:
        """Open the Shotlist Visualizer at the shot linked to the selected layer."""
        if not filename_stem:
            return
        try:
            from data.metadata import get_metadata
            metas = get_metadata(self._project_path, filename_stem)
            if not metas:
                return
            filename = metas[0].get("filename", "")
            if not filename:
                return
            from visualizers.shot_visualizer import open_at_shot
            open_at_shot(
                self._project_path,
                filename,
                "movies",
                shot_id=shot_id or "",
                loop=True,
            )
        except Exception:
            import traceback
            traceback.print_exc()

    def _on_inspector_lw_changed(self, value: float) -> None:
        """Persist line_weight change to the active placed layer."""
        if self._inspector_layer is not None:
            self._inspector_layer["line_weight"] = value
            self._save_current_layers()

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
        # Sync panel row for the mirror if it's visible
        layer = self._overlay._layer_by_id(lid)
        if layer:
            mirror_id = layer.get("mirror_id")
            if mirror_id:
                self._layer_panel.update_layer_name(mirror_id, name)
        self._save_current_layers()

    def _on_panel_layers_reordered(self, new_order: list) -> None:
        self._overlay.reorder_layers(new_order)
        self._save_current_layers()

    def _on_panel_layer_visibility(self, lid: str, visible: bool) -> None:
        """Toggle a layer's visible flag from the layer-panel eye button."""
        layer = self._overlay._layer_by_id(lid)
        if layer is not None:
            layer["visible"] = visible
            self._overlay.update()
            self._spread_view.set_layers(self._overlay.current_layers())
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
    # Silhouette insertion (from the browser panel)

    def _insert_silhouette(self, png_path: str, meta: dict) -> None:
        """Insert a catalog silhouette PNG centred on the active spread page."""
        if self._doc is None or not self._slug:
            return
        right_i  = self._spread_view._right_i
        left_i   = self._spread_view._left_i
        page_idx = right_i if right_i is not None else left_i
        if page_idx is None:
            return
        self._insert_silhouette_at(png_path, meta, page_idx, 0.5, 0.5)

    def _on_silhouette_drop(self, abs_path: str, page_idx: int,
                             nx: float, ny: float, meta: dict) -> None:
        """Handle a drag-drop from the silhouette browser onto a page."""
        self._insert_silhouette_at(abs_path, meta, page_idx, nx, ny)

    def _insert_silhouette_at(self, png_path: str, meta: dict,
                               page_idx: int, nx: float, ny: float) -> None:
        """Copy *png_path* into the book asset dir and create an Image layer.

        Copies the source PNG into ``illustrations/silhouettes/`` inside the
        current book directory so it becomes part of the book's local asset
        set, then creates an Image layer at the normalised position *(nx, ny)*
        on *page_idx*.
        """
        if self._doc is None or not self._slug:
            return

        # Copy PNG into book's illustrations/silhouettes/ directory
        from data.book import book_dir as _book_dir_fn
        bdir     = _book_dir_fn(self._project_path, self._slug)
        dest_dir = bdir / "illustrations" / "silhouettes"
        dest_dir.mkdir(parents=True, exist_ok=True)

        src       = Path(png_path)
        dest_name = f"{meta.get('label', 'sil')}_{src.stem}_{uuid.uuid4().hex[:6]}.png"
        dest      = dest_dir / dest_name
        try:
            _shutil.copy2(str(src), str(dest))
        except OSError:
            return

        source_rel = str(dest.relative_to(bdir))

        # Compute aspect-correct default height
        default_w = _IMG_DEFAULT_W
        default_h = default_w
        pix = QPixmap(str(dest))
        if not pix.isNull() and pix.height() > 0:
            rects = self._overlay._visible_page_rects()
            r = rects.get(page_idx)
            if r and r.width() > 0 and r.height() > 0:
                pix_aspect = pix.width() / pix.height()
                sw = default_w * r.width()
                sh = sw / pix_aspect
                default_h = sh / r.height()

        layer = {
            "id":             meta.get("layer_id") or f"img_{uuid.uuid4().hex[:8]}",
            "type":           "Image",
            "name":           Path(source_rel).stem,
            "label":          meta.get("label", ""),
            "field":          meta.get("field", ""),
            "shot_id":        meta.get("shot_id", ""),
            "filename_stem":  meta.get("filename_stem", ""),
            "description":    meta.get("description", ""),
            "source":         source_rel,
            "page":           page_idx,
            "spread":         self._spread_idx,
            "x":              nx,
            "y":              ny,
            "width":          default_w,
            "height":         default_h,
            "rotation":       0.0,
            "z_index":        len(self._overlay.current_layers()),
            "line_weight":    float(meta.get("line_weight") or 1.0),
        }
        self._overlay._layers.append(layer)
        self._overlay._sel_id = layer["id"]
        self._overlay._sel_pt = None
        self._overlay.update()
        self._on_layer_committed(layer)

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
            # Only steal keys when this window is the active top-level window.
            # Without this guard every key press in any other in-process window
            # (e.g. Shot Visualizer) would also be caught here, because the
            # filter is registered on QApplication rather than on a child widget.
            if QApplication.activeWindow() is not self:
                return super().eventFilter(obj, event)
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
                if key in (Qt.Key_Home, Qt.Key_End):
                    self._sil_browser.step_scope(
                        -1 if key == Qt.Key_Home else 1
                    )
                    return True
                if key in (Qt.Key_PageUp, Qt.Key_PageDown):
                    self._sil_browser.step_field(
                        -1 if key == Qt.Key_PageUp else 1
                    )
                    return True
                if key in (Qt.Key_Up, Qt.Key_Down):
                    self._sil_browser.step_label(
                        -1 if key == Qt.Key_Up else 1
                    )
                    return True
                if key == Qt.Key_T:
                    new_tool = _TOOL_NONE if self._tool == _TOOL_TEXT else _TOOL_TEXT
                    self._set_tool(new_tool)
                    return True
                if key == Qt.Key_C:
                    new_tool = _TOOL_NONE if self._tool == _TOOL_CUT else _TOOL_CUT
                    self._set_tool(new_tool)
                    return True
                if key == Qt.Key_E:
                    new_tool = _TOOL_NONE if self._tool == _TOOL_ERASE else _TOOL_ERASE
                    self._set_tool(new_tool)
                    return True
                if key in (Qt.Key_BracketLeft, Qt.Key_BraceLeft):
                    new_state = not self._mask_left_btn.isChecked()
                    self._mask_left_btn.setChecked(new_state)
                    self._toggle_mask_left(new_state)
                    return True
                if key in (Qt.Key_BracketRight, Qt.Key_BraceRight):
                    new_state = not self._mask_right_btn.isChecked()
                    self._mask_right_btn.setChecked(new_state)
                    self._toggle_mask_right(new_state)
                    return True
                if key == Qt.Key_H:
                    self._show_outlines_chk.setChecked(not self._show_outlines_chk.isChecked())
                    return True
                if key == Qt.Key_S:
                    self._text_vis_chk.setChecked(not self._text_vis_chk.isChecked())
                    return True
                if key == Qt.Key_L:
                    self._layers_visible_btn.setChecked(not self._layers_visible_btn.isChecked())
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
                        layer_type = new_layer.get("type", "Cut")
                        prefix = "img" if layer_type == "Image" else "cut"
                        new_layer["id"] = f"{prefix}_{uuid.uuid4().hex[:8]}"
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
                        if layer_type == "Image":
                            new_layer["x"] = min(0.95, new_layer.get("x", 0.5) + 0.03)
                            new_layer["y"] = min(0.95, new_layer.get("y", 0.5) + 0.03)
                            self._overlay._layers.append(new_layer)
                            self._overlay._sel_id = new_layer["id"]
                            self._overlay._sel_pt = None
                            self._overlay.update()
                            self._on_layer_committed(new_layer)
                        else:
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
                self._overlay.selection_changed.emit("")
            elif self._right_stack.currentIndex() == 1:
                self._hide_inspector()
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

        if mods == Qt.NoModifier:
            if key == Qt.Key_T:
                new_tool = _TOOL_NONE if self._tool == _TOOL_TEXT else _TOOL_TEXT
                self._set_tool(new_tool)
                return
            if key == Qt.Key_C:
                new_tool = _TOOL_NONE if self._tool == _TOOL_CUT else _TOOL_CUT
                self._set_tool(new_tool)
                return
            if key == Qt.Key_E:
                new_tool = _TOOL_NONE if self._tool == _TOOL_ERASE else _TOOL_ERASE
                self._set_tool(new_tool)
                return
            if key in (Qt.Key_BracketLeft, Qt.Key_BraceLeft):
                self._mask_left_btn.setChecked(not self._mask_left_btn.isChecked())
                self._toggle_mask_left(self._mask_left_btn.isChecked())
                return
            if key in (Qt.Key_BracketRight, Qt.Key_BraceRight):
                self._mask_right_btn.setChecked(not self._mask_right_btn.isChecked())
                self._toggle_mask_right(self._mask_right_btn.isChecked())
                return
            if key == Qt.Key_H:
                self._show_outlines_chk.setChecked(not self._show_outlines_chk.isChecked())
                return
            if key == Qt.Key_S:
                self._text_vis_chk.setChecked(not self._text_vis_chk.isChecked())
                return

        super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_visualizer(project_path: str) -> None:
    """Launch the Book Visualizer window."""
    from visualizers._window_helpers import raise_existing_window
    if raise_existing_window("book"):
        return

    app = QApplication.instance() or QApplication(sys.argv)
    theme.apply_theme(app)
    window = BookVisualizerWindow(project_path)
    window.show()
    app.exec_()

