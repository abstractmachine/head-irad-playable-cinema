#!/usr/bin/env python3
"""Book Visualizer — browse imported books as page spreads.

Launched via:
    crossing visualizer book

Layout:
  LEFT  — open-book spread view (one spread at a time) + yellow page bar
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

import json
import math
import os
import sys
import uuid
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

_CLI_PATH = Path(__file__).parent.parent / "cli.py"

from styles import theme
from styles.theme import GripSplitter, save_window_geometry, restore_window_geometry
from visualizers.window_visualizer import WindowVisualizer
from visualizers.components.collapsible_section import CollapsibleSection
from visualizers.components.hover_icon_button import HoverIconButton, build_icon_pair
from visualizers.components.illustration_browser import IllustrationBrowser
from visualizers.components.illustration_source import EngravingSource
from visualizers.components.metadata_block import (
    InspectorTable,
    MetadataBlock,
    INSPECTOR_DIVIDER_THICKNESS,
    INSPECTOR_ROW_HEIGHT,
    inspector_action_icon_size,
)

from PyQt5.QtCore import Qt, QByteArray, QEvent, pyqtSignal, QMimeData, QPoint, QRect, QRectF, QSize, QThread, QTimer, QPointF
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
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

_TAB_CONTENT_STYLESHEET = (
    f"QWidget {{ background: {theme.TAB_BG}; }}"
    f" QPushButton {{ background-color: {theme.BTN_BG}; border: none;"
    f" padding: 0 10px; border-radius: 3px;"
    f" min-height: {theme.BTN_H}px; max-height: {theme.BTN_H}px; }}"
    f" QPushButton:hover    {{ background-color: {theme.BTN_HOVER}; }}"
    f" QPushButton:pressed  {{ background-color: {theme.BTN_PRESSED}; }}"
    f" QPushButton:checked  {{ background-color: {theme.ACCENT}; color: {theme.TEXT}; }}"
    f" QPushButton:disabled {{ color: {theme.TEXT_DIM};"
    f" background-color: {theme.BTN_BG}; }}"
)

_WORKSPACE_TABS_STYLESHEET = (
    f"QTabWidget {{ background: {theme.CANVAS_BG}; border: none; }}"
    f"QTabWidget::pane {{ background: {theme.TAB_BG}; border: none; }}"
    f"QTabBar {{ background: {theme.CANVAS_BG}; border: none; }}"
    f"QTabBar::tab {{"
    f" background: {theme.CANVAS_BG}; color: {theme.TEXT_DIM};"
    f" padding: 2px 12px; border: none;"
    f" font-family: '{theme.FAMILY_UI}'; font-size: {theme.BASE_PT}pt;"
    f" font-weight: {theme.WEIGHT_UI};"
    f" min-height: 20px;"
    f" min-width: 0px;"
    f"}}"
    f"QTabBar::tab:selected {{ background: {theme.TAB_BG}; color: {theme.TEXT}; }}"
    f"QTabBar::tab:hover {{ background: {theme.TAB_BG}; color: {theme.TEXT}; }}"
)


# ---------------------------------------------------------------------------
# SVG icon helper (canonical implementation lives in styles.theme)
# ---------------------------------------------------------------------------

from styles.theme import svg_icon as _svg_icon  # noqa: E402


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
    text_sel_committed = pyqtSignal(dict) # emitted when a text selection is created
    text_sel_removed   = pyqtSignal(str)  # emitted with text sel id on deletion
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
        self._action_btn_rects: dict = {}                  # lid → action-name → QRect
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
            self.setCursor(self._make_cross_cursor("#ffff00"))   # yellow
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
                "delete":  _svg_icon("trash-solid",       14, "#ffffff"),
                "flip_h":  _svg_icon("flip",              14, "#ffffff"),
                "flip_v":  _svg_icon("flip-reverse",      14, "#ffffff"),
            }
        return self._action_icons_cache

    def _draw_img_action_buttons(self, p: QPainter, layer: dict, rects: dict) -> None:
        """Draw in-canvas action buttons above the selected/hovered image.

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
        # Three buttons left-aligned: [delete] [flip_h] [flip_v]
        del_rect = QRect(min_x,                  btn_y, sz, sz)
        fh_rect  = QRect(min_x +     (sz + gap), btn_y, sz, sz)
        fv_rect  = QRect(min_x + 2 * (sz + gap), btn_y, sz, sz)
        self._action_btn_rects[layer["id"]] = {
            "delete": del_rect,
            "flip_h": fh_rect,
            "flip_v": fv_rect,
        }
        icons = self._get_action_icons()
        for btn_rect, icon_key, btn_color in [
            (del_rect, "delete",  QColor(160,  60,  60, 220)),
            (fh_rect,  "flip_h",  QColor( 60,  90,  90, 220)),
            (fv_rect,  "flip_v",  QColor( 60,  90,  90, 220)),
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
            del_rect = btns.get("delete")
            fh_rect  = btns.get("flip_h")
            fv_rect  = btns.get("flip_v")
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
        SEL_COLOR  = QColor(theme.ACCENT)        # highlight color for selected
        FILL_ALPHA = QColor(230, 230, 230, 12)   # very subtle light fill
        _accent_fill = QColor(theme.ACCENT)
        _accent_fill.setAlpha(theme.ACCENT_FILL_ALPHA)
        SEL_FILL   = _accent_fill
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
                p.setPen(QPen(QColor(theme.ACCENT), 1.0, Qt.DashLine))
                _drag_fill = QColor(theme.ACCENT)
                _drag_fill.setAlpha(theme.ACCENT_FILL_ALPHA)
                p.setBrush(_drag_fill)
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
                    # all handles accent color when layer selected; grey otherwise
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
                # Generating state: accent color placeholder + spinner arc
                p.setPen(QPen(QColor("#ffff00"), 1, Qt.DashLine))
                p.setBrush(QColor(120, 0, 120, 60))
                p.drawRect(QRectF(-sw / 2, -sh / 2, sw, sh))
                # Spinner arc (drawn in unscaled/unflipped space)
                r_spin = min(sw, sh) * 0.18
                r_spin = max(8.0, min(32.0, r_spin))
                arc_rect = QRectF(-r_spin, -r_spin, r_spin * 2, r_spin * 2)
                spin_pen = QPen(QColor("#ffff00"), max(2.0, r_spin * 0.18))
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
                if layer.get("layer_subtype") == "Engraving":
                    p.setPen(QPen(QColor("#ffffff"), 1))
                    p.drawText(
                        QRectF(-sw / 2, -sh / 2, sw, sh),
                        Qt.AlignCenter,
                        "Missing Engraving",
                    )
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
# _PageBar — horizontal yellow position indicator
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
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

    def set_state(self, current: int, total: int) -> None:
        self._current = current
        self._spread_count = total
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.transparent)
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
# _LayerPanel — table-driven layer list used in the right control panel
# ---------------------------------------------------------------------------

class _LayerPanel(QWidget):
    """Scrollable layer list used inside the right control panel."""

    layer_selected    = pyqtSignal(str)       # id
    layer_deleted     = pyqtSignal(str)       # id
    layer_renamed     = pyqtSignal(str, str)  # id, name
    layers_reordered  = pyqtSignal(list)      # new id order
    layer_visibility_toggled = pyqtSignal(str, bool)  # id, visible
    layer_deselected  = pyqtSignal()          # selection cleared

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._rows: dict = {}          # lid → row state
        self._order: list = []         # lid order (top = front)
        self._sel_id: Optional[str] = None
        self._updating_table = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._table_view = InspectorTable(3)
        self._table = self._table_view.table()
        self._table_view.configure_interactive_rows()
        self._table_view.set_column_resize_mode(0, QHeaderView.Stretch)
        self._table_view.set_column_resize_mode(1, QHeaderView.Fixed)
        self._table_view.set_column_resize_mode(2, QHeaderView.Fixed)
        self._table_view.set_column_width(1, INSPECTOR_ROW_HEIGHT)
        self._table_view.set_column_width(2, INSPECTOR_ROW_HEIGHT)
        self._table.itemSelectionChanged.connect(self._on_table_selection_changed)
        self._table.itemChanged.connect(self._on_table_item_changed)
        self._table.model().rowsMoved.connect(self._on_rows_moved)
        # Deselect when clicking empty space below the rows.
        self._table.viewport().installEventFilter(self)
        outer.addWidget(self._table_view)

    def set_layers(self, layers: list) -> None:
        self._updating_table = True
        self._table_view.set_row_count(0)
        self._rows.clear()
        self._order.clear()
        for layer in layers:
            self._append_layer(layer)
        self._updating_table = False
        self._refresh_row_positions()

    def add_layer(self, layer: dict) -> None:
        self._append_layer(layer)

    def _append_layer(self, layer: dict) -> None:
        lid = layer["id"]
        self.remove_layer(lid)

        row_idx = self._table_view.row_count()
        self._table_view.insert_row(row_idx)

        name_item = QTableWidgetItem(layer.get("name", "Cut"))
        name_item.setData(Qt.UserRole, lid)
        name_item.setFlags(
            Qt.ItemIsEnabled
            | Qt.ItemIsSelectable
            | Qt.ItemIsEditable
            | Qt.ItemIsDragEnabled
            | Qt.ItemIsDropEnabled
        )
        name_item.setTextAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self._table_view.set_item(row_idx, 0, name_item)

        eye_cell, eye_btn = self._table_view.make_action_button("eye-solid", "Toggle layer visibility")
        eye_btn.clicked.connect(lambda _=False, _lid=lid: self._toggle_visibility(_lid))

        del_cell, del_btn = self._table_view.make_action_button("trash", "Delete layer")
        del_btn.clicked.connect(lambda _=False, _lid=lid: self.layer_deleted.emit(_lid))

        self._table_view.set_cell_widget(row_idx, 1, eye_cell)
        self._table_view.set_cell_widget(row_idx, 2, del_cell)

        self._rows[lid] = {
            "name": name_item.text(),
            "visible": bool(layer.get("visible", True)),
            "eye_btn": eye_btn,
            "del_btn": del_btn,
        }
        self._order.append(lid)
        self._set_eye_icon(lid)
        self._refresh_row_positions()

    def remove_layer(self, lid: str) -> None:
        row_idx = self._row_for_id(lid)
        if row_idx is not None:
            self._table_view.remove_row(row_idx)
        self._rows.pop(lid, None)
        if lid in self._order:
            self._order.remove(lid)
        if self._sel_id == lid:
            self._sel_id = None
        self._refresh_row_positions()

    def remove_all(self) -> None:
        self._table_view.set_row_count(0)
        self._rows.clear()
        self._order.clear()
        self._sel_id = None

    def select_layer(self, lid: Optional[str]) -> None:
        self._sel_id = lid
        self._updating_table = True
        if lid is None:
            self._table.clearSelection()
        else:
            row_idx = self._row_for_id(lid)
            if row_idx is not None:
                self._table.selectRow(row_idx)
                self._table.setCurrentCell(row_idx, 0)
        self._updating_table = False
        self._refresh_row_positions()

    def update_layer_name(self, lid: str, name: str) -> None:
        row_idx = self._row_for_id(lid)
        state = self._rows.get(lid)
        if row_idx is None or state is None:
            return
        item = self._table.item(row_idx, 0)
        if item is None:
            return
        self._updating_table = True
        item.setText(name)
        self._updating_table = False
        state["name"] = name

    def update_layer_visibility(self, lid: str, visible: bool) -> None:
        state = self._rows.get(lid)
        if state is None:
            return
        state["visible"] = bool(visible)
        self._set_eye_icon(lid)
        self._refresh_row_positions()

    def _on_table_selection_changed(self) -> None:
        if self._updating_table:
            return
        row_idx = self._table.currentRow()
        if row_idx < 0:
            self.select_layer(None)
            self.layer_deselected.emit()
            return
        item = self._table.item(row_idx, 0)
        if item is None:
            return
        lid = item.data(Qt.UserRole)
        self.select_layer(lid)
        self.layer_selected.emit(lid)

    def _on_table_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating_table or item.column() != 0:
            return
        lid = item.data(Qt.UserRole)
        if not lid or lid not in self._rows:
            return
        state = self._rows[lid]
        old_name = state.get("name", "Cut")
        new_name = item.text().strip() or old_name
        if new_name != item.text():
            self._updating_table = True
            item.setText(new_name)
            self._updating_table = False
        if new_name != old_name:
            state["name"] = new_name
            self.layer_renamed.emit(lid, new_name)

    def _on_rows_moved(self) -> None:
        self._rebuild_order()
        self._refresh_row_positions()
        self.layers_reordered.emit(list(self._order))

    def _refresh_row_positions(self) -> None:
        self._rebuild_order()
        for idx, lid in enumerate(self._order):
            item = self._table.item(idx, 0)
            if item is None:
                continue
            state = self._rows.get(lid)
            if state is None:
                continue
            selected = lid == self._sel_id
            self._table_view.style_text_item(item, selected, dimmed=not state.get("visible", True))

            for col in (1, 2):
                w = self._table.cellWidget(idx, col)
                if w is not None:
                    self._table_view.style_action_cell(w, selected, add_left_divider=True)

    def _set_eye_icon(self, lid: str) -> None:
        state = self._rows.get(lid)
        if state is None:
            return
        icon_name = "eye-solid" if state.get("visible", True) else "eye-closed"
        btn = state.get("eye_btn")
        if btn is None:
            return
        n, h = build_icon_pair(icon_name, inspector_action_icon_size(), normal_color=theme.TEXT_DIM)
        btn.set_icons(n, h)

    def _toggle_visibility(self, lid: str) -> None:
        state = self._rows.get(lid)
        if state is None:
            return
        state["visible"] = not state.get("visible", True)
        self._set_eye_icon(lid)
        self._refresh_row_positions()
        self.layer_visibility_toggled.emit(lid, state["visible"])

    def _row_for_id(self, lid: str) -> Optional[int]:
        for i in range(self._table.rowCount()):
            item = self._table.item(i, 0)
            if item is not None and item.data(Qt.UserRole) == lid:
                return i
        return None

    def _rebuild_order(self) -> None:
        new_order = []
        for i in range(self._table.rowCount()):
            item = self._table.item(i, 0)
            if item is not None:
                new_order.append(item.data(Qt.UserRole))
        self._order = new_order

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self._table.viewport() and event.type() == QEvent.MouseButtonPress:
            idx = self._table.indexAt(event.pos())
            if not idx.isValid():
                # Let itemSelectionChanged fire naturally to emit layer_deselected.
                self._table.clearSelection()
        return False


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
# Engraving Browser sidebar — framework-backed browser for Book Visualizer
# ---------------------------------------------------------------------------

_BROWSER_PANEL_W   = 420   # px — preferred width of the engraving browser column
_BROWSER_THUMB_SZ  = 80    # px per thumbnail cell


class _IllustrationBrowserPanel(QWidget):
    """Book-side engraving browser composed from framework components.

    This panel is intentionally data-limited to engravings only:
    IllustrationBrowser + EngravingSource.
    """

    silhouette_insert_requested = pyqtSignal(str, dict)  # (abs png_path, metadata)
    thumbnail_selected = pyqtSignal(dict)                # selected engraving record
    engraving_selected = pyqtSignal(dict)                # alias for compatibility

    def __init__(
        self,
        project_path: str,
        media_type: str = "movie",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._project_path = project_path
        self._source = EngravingSource(project_path)
        self._sort_mode = "catalog"

        self._browser = IllustrationBrowser(
            source=self._source,
            media_type=media_type,
            thumb_size=_BROWSER_THUMB_SZ,
            detach_controls=True,
            light_bg=True,
            parent=self,
        )
        self._browser.selectionChanged.connect(self._on_selection_changed)
        self._browser.itemActivated.connect(self._on_item_activated)
        self._browser.catalogReloaded.connect(self._apply_sort_mode)

        self.filter_panel = self._browser.filter_panel
        self.status_bar = self._browser.status_bar
        self.pagination_panel = self._browser.pagination_panel

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._browser)

    def _record_png(self, rec: dict) -> Optional[Path]:
        """Return the best usable image path for an engraving record."""
        p = self._source.thumbnail_path(rec)
        if p is not None and p.exists():
            return p
        for key in ("output_png", "raw_png"):
            val = rec.get(key)
            if val:
                cand = Path(str(val))
                if cand.exists():
                    return cand
        return None

    def _on_selection_changed(self, rec: dict) -> None:
        self.thumbnail_selected.emit(dict(rec))
        self.engraving_selected.emit(dict(rec))

    def _on_item_activated(self, rec: dict) -> None:
        png = self._record_png(rec)
        if png is None:
            return
        self.silhouette_insert_requested.emit(str(png), dict(rec))

    def _apply_sort_mode(self) -> None:
        """Apply the active sort mode to the source cache and refresh the grid."""
        items = self._source.items()
        if self._sort_mode == "alphabetical":
            items = sorted(items, key=lambda r: str.casefold(r.get("label") or ""))
        self._source._records = list(items)
        self._browser.refresh()

    def set_sort_mode(self, mode: str) -> None:
        self._sort_mode = mode
        self._apply_sort_mode()

    # ------------------------------------------------------------------
    # Public compatibility surface used by BookVisualizerWindow

    def step_scope(self, delta: int) -> None:
        self._browser.stepItem(delta)

    def step_field(self, delta: int) -> None:
        self._browser.stepField(delta)

    def step_label(self, delta: int) -> None:
        self._browser.stepKeyword(delta)

    def clear_selection(self) -> None:
        if 0 <= self._browser._selected_index < len(self._browser._filtered_items):
            page_idx = self._browser._selected_index - self._browser._page_index * self._browser._page_size
            if 0 <= page_idx < len(self._browser._cells):
                self._browser._cells[page_idx].set_selected(False)
        self._browser._selected_index = -1

    def _stop_loader(self) -> None:
        self._browser._stop_loader()

    def _stop_eng_loader(self) -> None:
        self._browser._stop_loader()

    def set_engravings(self, _entries: list) -> None:
        """Compatibility no-op.

        Engravings are now sourced from EngravingSource, not maintained as a
        separate per-window list.
        """

    def add_engraving(self, _entry: dict) -> None:
        """Compatibility no-op for old per-book engraving list updates."""

class BookVisualizerWindow(WindowVisualizer):
    """Main window for the Book Visualizer."""

    def __init__(self, project_path: str) -> None:
        # Initialize attributes used by create_browser/create_inspector
        # before WindowVisualizer.__init__ runs (it calls those hooks).
        self._project_path = project_path

        self._books: list[dict] = []
        self._current_book_idx: int = 0   # index into self._books
        self._doc = None                  # open fitz.Document (or None)
        self._slug: str = ""            # slug of the currently open book
        self._spread_idx: int = 0         # current spread index
        self._updating_combo: bool = False

        # tool / layer state
        self._tool: str = _TOOL_NONE
        self._next_layer_id: int = 1      # used by _CutOverlay for id generation
        self._clipboard_layer: Optional[dict] = None  # copy/cut clipboard

        # Let WindowVisualizer manage geometry persistence for this window.
        super().__init__(pref_key="window_book")
        self.setWindowTitle("Crossing — Book Visualizer")
        # normal initialization (no debug reporting)

        # Post-layout adjustments
        self.setMinimumSize(700, 480)
        self.resize(1500, 800)

        # Populate books + restore per-book state
        self._load_all_books()
        self._inspectors_hidden: bool = False
        self._saved_inspector_split_sizes: Optional[list[int]] = None

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # Ensure the spread renders on first show (the initial _do_render call in
        # __init__ fires before the widget has a valid size, so we retry here).
        QTimer.singleShot(0, self._spread_view._do_render)
        # post-show actions (no debug reporting)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._sil_browser._stop_loader()
        self._sil_browser._stop_eng_loader()
        self._save_current_layers()
        self._close_doc()
        # WindowVisualizer handles geometry persistence
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # WindowVisualizer hooks
    def create_browser(self) -> QWidget:
        # Build the left canvas column (spread view + page bar) and return it
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
        self._overlay.text_sel_committed.connect(self._on_text_sel_committed)
        self._overlay.text_sel_removed.connect(self._on_text_sel_removed)
        self._overlay.silhouette_drop_requested.connect(self._on_silhouette_drop)

        self._page_bar = _PageBar()
        self._page_bar.jumped.connect(self._go_spread)
        left_layout.addWidget(self._page_bar)

        return left_col

    def create_inspector(self) -> QWidget:
        # Build the combined inspector: illustration browser + control panel
        panel_split = GripSplitter(Qt.Horizontal)
        self._panel_splitter = panel_split
        self._engraving_split_sizes: Optional[list[int]] = None

        # Middle: engraving browser
        self._sil_browser = _IllustrationBrowserPanel(self._project_path)
        self._sil_browser.silhouette_insert_requested.connect(self._insert_silhouette)
        self._sil_browser.thumbnail_selected.connect(self._on_browser_thumbnail_selected)
        panel_split.addWidget(self._sil_browser)

        # Right: control panel
        self._control_panel = self._build_control_panel()
        panel_split.addWidget(self._control_panel)

        panel_split.setStretchFactor(0, 0)
        panel_split.setStretchFactor(1, 0)
        panel_split.setSizes([_BROWSER_PANEL_W, _PANEL_WIDTH])
        self._engraving_split_sizes = [_BROWSER_PANEL_W, _PANEL_WIDTH]
        QTimer.singleShot(0, self._collapse_engraving_browser)
        return panel_split

        self.setMinimumSize(700, 480)
        self.resize(1500, 800)

    def _build_control_panel(self) -> QWidget:
        # Outer container with fixed width
        outer = QWidget()
        outer.setFixedWidth(_PANEL_WIDTH)
        # normal construction of control panel wrapper
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
        panel.setStyleSheet(_TAB_CONTENT_STYLESHEET)
        scroll.setWidget(panel)

        section_gap = theme.SECTION_GAP
        action_btn_style = (
            f"QPushButton {{"
            f"  background-color: {theme.BTN_BG}; color: {theme.TEXT};"
            f"  border: none; border-radius: 3px; padding: 0 8px;"
            f"  min-height: {theme.BTN_H}px; max-height: {theme.BTN_H}px;"
            f"}}"
            f"QPushButton:hover   {{ background-color: {theme.ACCENT}; color: {theme.ACCENT_TEXT}; }}"
            f"QPushButton:pressed {{ background-color: {theme.BTN_PRESSED}; }}"
            f"QPushButton:checked {{ background-color: {theme.ACCENT}; color: {theme.ACCENT_TEXT}; }}"
            f"QPushButton:disabled {{ background-color: {theme.BTN_BG};"
            f" color: rgba(255,255,255,0.15); }}"
        )
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(section_gap, section_gap, section_gap, section_gap)
        layout.setSpacing(section_gap)

        # ── Info section ──────────────────────────────────────────────
        book_sec = CollapsibleSection("Info", pref_key="book_section_info")
        book_wrap = QWidget()
        book_layout = QVBoxLayout(book_wrap)
        book_layout.setContentsMargins(0, 0, 0, 0)
        book_layout.setSpacing(section_gap)

        self._combo = QComboBox()
        self._combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._combo.setFocusPolicy(Qt.NoFocus)
        self._combo.setMaxVisibleItems(10)
        self._combo.setSizeAdjustPolicy(QComboBox.AdjustToContentsOnFirstShow)

        _book_sv = QListView(self._combo)
        _book_sv.setUniformItemSizes(True)
        _book_sv.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        _book_sv.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        _book_sv.setFrameShape(QFrame.NoFrame)
        _book_sv.setLineWidth(0)
        _book_sv.setMidLineWidth(0)
        _book_sv.setContentsMargins(0, 0, 0, 0)
        _book_sv.setStyleSheet(
            f"QListView {{ background: {theme.INPUT_BG}; color: {theme.TEXT};"
            f" border: 0px; margin: 0px; padding: 0px; outline: 0px; }}"
            f"QListView::item {{ background: {theme.INPUT_BG}; padding: 0px 8px;"
            f" min-height: 24px; border: 0px; }}"
            f"QListView::item:selected {{ background: {theme.ACCENT}; color: {theme.ACCENT_TEXT}; }}"
        )
        self._combo.setView(_book_sv)
        _book_sv.setViewportMargins(0, 0, 0, 0)
        _book_sc = _book_sv.parentWidget()
        if _book_sc is not None:
            _book_sc.setFrameStyle(QFrame.NoFrame)
            _book_sc.setLineWidth(0)
            _book_sc.setMidLineWidth(0)
            _book_sc.setStyleSheet(
                f"QFrame {{ background: {theme.INPUT_BG}; border: 0px; margin: 0px; padding: 0px; }}"
            )
            if _book_sc.layout():
                _book_sc.layout().setContentsMargins(0, 0, 0, 0)
                _book_sc.layout().setSpacing(0)

        def _refresh_book_combo_color(_idx: int = 0) -> None:
            _has_choice = bool(self._combo.currentText().strip())
            _col = theme.TEXT if _has_choice else theme.TEXT_DIM
            self._combo.setStyleSheet(
                f"QComboBox {{ background: {theme.BTN_BG}; color: {_col};"
                f" border: none; border-radius: 3px; padding: 0px 6px;"
                f" min-height: {theme.BTN_H}px; max-height: {theme.BTN_H}px; }}"
                f"QComboBox::drop-down {{ border: none; }}"
            )

        self._combo.currentIndexChanged.connect(_refresh_book_combo_color)
        _refresh_book_combo_color()
        self._combo.currentIndexChanged.connect(self._on_combo_changed)
        self._combo.installEventFilter(self)
        book_layout.addWidget(self._combo)

        # Page info row
        dim_style = f"color: {theme.TEXT_DIM}; font-size: {theme.BASE_PT}pt;"

        # Import / New buttons
        book_btn_row = QHBoxLayout()
        book_btn_row.setContentsMargins(0, 0, 0, 0)
        book_btn_row.setSpacing(section_gap)

        self._import_btn = QPushButton("Replace")
        self._import_btn.setToolTip("Import a PDF into this book")
        self._import_btn.setStyleSheet(action_btn_style)
        self._import_btn.clicked.connect(self._on_import)
        book_btn_row.addWidget(self._import_btn, 1)

        self._new_btn = QPushButton("New")
        self._new_btn.setToolTip("Create a new book")
        self._new_btn.setStyleSheet(action_btn_style)
        self._new_btn.clicked.connect(self._on_new_book)
        book_btn_row.addWidget(self._new_btn, 1)

        del_n, del_h = build_icon_pair("trash", theme.BTN_ICON, normal_color=theme.TEXT_DIM)
        self._delete_book_btn = HoverIconButton("", del_n, del_h)
        self._delete_book_btn.setIconSize(QSize(theme.BTN_ICON, theme.BTN_ICON))
        self._delete_book_btn.setFixedSize(theme.BTN_H, theme.BTN_H)
        self._delete_book_btn.setStyleSheet(action_btn_style)
        self._delete_book_btn.setToolTip("Delete this book")
        self._delete_book_btn.setFocusPolicy(Qt.NoFocus)
        self._delete_book_btn.clicked.connect(self._on_delete_book)
        book_btn_row.addWidget(self._delete_book_btn)

        book_layout.addLayout(book_btn_row)

        spread_row = QWidget()
        spread_row_layout = QHBoxLayout(spread_row)
        spread_row_layout.setContentsMargins(0, 0, 0, 0)
        spread_row_layout.setSpacing(section_gap)

        self._spread_page_edit = QLineEdit("")
        self._spread_page_edit.setPlaceholderText("\u2014")
        self._spread_page_edit.setStyleSheet(
            f"{dim_style} background: {theme.INPUT_BG}; border: none;"
            f" min-height: {theme.BTN_H}px; max-height: {theme.BTN_H}px;"
            f" padding: 0px 6px; border-radius: 3px;"
            f" selection-background-color: {theme.ACCENT}; selection-color: {theme.ACCENT_TEXT};"
        )
        self._spread_page_edit.setToolTip("Type a page number and press Enter to jump")
        self._spread_page_edit.setFixedWidth(60)
        self._spread_page_edit.setFixedHeight(theme.BTN_H)
        self._spread_page_edit.returnPressed.connect(self._on_page_entered)

        def _spread_page_mouse_press(e):
            QLineEdit.mousePressEvent(self._spread_page_edit, e)
            QTimer.singleShot(0, self._spread_page_edit.selectAll)

        self._spread_page_edit.mousePressEvent = _spread_page_mouse_press
        spread_row_layout.addWidget(self._spread_page_edit)

        self._spread_suffix = QLabel("")
        self._spread_suffix.setStyleSheet(dim_style)
        self._spread_suffix.setFixedHeight(theme.BTN_H)
        self._spread_suffix.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        spread_row_layout.addWidget(self._spread_suffix)
        spread_row_layout.addStretch()

        self._spread_row = spread_row
        book_layout.addWidget(spread_row)
        self._spread_row.hide()

        # Match shared section-body grid used by Engravings panels.
        self._loading_label = QLabel("")
        self._loading_label.setWordWrap(True)
        self._loading_label.setStyleSheet(
            f"color: {theme.TEXT}; background: {theme.ACCENT};"
            f" font-size: {theme.BASE_PT}pt; padding: 3px 6px; border-radius: 3px;"
        )
        self._loading_label.hide()
        book_layout.addWidget(self._loading_label)

        book_sec.add_widget(book_wrap)
        layout.addWidget(book_sec)

        # ── Tools section ─────────────────────────────────────────────
        tools_sec = CollapsibleSection("Tools", pref_key="book_section_tools")
        tools_wrap = QWidget()
        tools_layout = QVBoxLayout(tools_wrap)
        tools_layout.setContentsMargins(0, 0, 0, 0)
        tools_layout.setSpacing(section_gap)

        tool_row = QHBoxLayout()
        tool_row.setContentsMargins(0, 0, 0, 0)
        tool_row.setSpacing(section_gap)

        ICON_SIZE = theme.BTN_ICON
        BTN_SIZE  = theme.BTN_H

        text_n, text_h = build_icon_pair("text", ICON_SIZE)
        self._text_btn = HoverIconButton("", text_n, text_h)
        self._text_btn.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self._text_btn.setCheckable(True)
        self._text_btn.setFixedSize(BTN_SIZE, BTN_SIZE)
        self._text_btn.setStyleSheet(action_btn_style)
        self._text_btn.setToolTip("Text tool — drag to select PDF text")
        self._text_btn.clicked.connect(lambda checked: self._set_tool(_TOOL_TEXT if checked else _TOOL_NONE))
        tool_row.addWidget(self._text_btn)

        _mask_pair = QWidget()
        _mask_pair.setFixedSize(BTN_SIZE * 2, BTN_SIZE)
        _mask_pair_lay = QHBoxLayout(_mask_pair)
        _mask_pair_lay.setContentsMargins(0, 0, 0, 0)
        _mask_pair_lay.setSpacing(0)

        _mask_btn_style = action_btn_style

        mask_n, mask_h = build_icon_pair("mask-square", ICON_SIZE)
        self._mask_left_btn = HoverIconButton("", mask_n, mask_h)
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

        self._mask_right_btn = HoverIconButton("", mask_n, mask_h)
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

        cut_n, cut_h = build_icon_pair("cut", ICON_SIZE)
        self._cut_btn = HoverIconButton("", cut_n, cut_h)
        self._cut_btn.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self._cut_btn.setCheckable(True)
        self._cut_btn.setFixedSize(BTN_SIZE, BTN_SIZE)
        self._cut_btn.setStyleSheet(action_btn_style)
        self._cut_btn.setToolTip("Cut tool — draw polygon cuts")
        self._cut_btn.clicked.connect(lambda checked: self._set_tool(_TOOL_CUT if checked else _TOOL_NONE))
        tool_row.addWidget(self._cut_btn)

        erase_n, erase_h = build_icon_pair("erase", ICON_SIZE)
        self._erase_btn = HoverIconButton("", erase_n, erase_h)
        self._erase_btn.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self._erase_btn.setCheckable(True)
        self._erase_btn.setFixedSize(BTN_SIZE, BTN_SIZE)
        self._erase_btn.setStyleSheet(action_btn_style)
        self._erase_btn.setToolTip("Erase tool — click a cut to remove it")
        self._erase_btn.clicked.connect(lambda checked: self._set_tool(_TOOL_ERASE if checked else _TOOL_NONE))
        tool_row.addWidget(self._erase_btn)

        trash_n, trash_h = build_icon_pair("trash-solid", ICON_SIZE)
        self._trash_btn = HoverIconButton("", trash_n, trash_h)
        self._trash_btn.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self._trash_btn.setFixedSize(BTN_SIZE, BTN_SIZE)
        self._trash_btn.setStyleSheet(action_btn_style)
        self._trash_btn.setToolTip("Delete all cuts on this spread")
        self._trash_btn.clicked.connect(self._on_trash)
        tool_row.addWidget(self._trash_btn)

        tool_row.addStretch()
        tools_layout.addLayout(tool_row)

        self._show_outlines_chk = QCheckBox("Handles")
        self._show_outlines_chk.setChecked(True)
        self._show_outlines_chk.setFixedHeight(theme.BTN_H)
        self._show_outlines_chk.setFocusPolicy(Qt.NoFocus)
        self._show_outlines_chk.setStyleSheet(
            f"QCheckBox {{ color: {theme.TEXT};"
            f" min-height: {theme.BTN_H}px; max-height: {theme.BTN_H}px; padding: 0px; }}"
            f"QCheckBox::indicator {{ width: {theme.BTN_ICON}px; height: {theme.BTN_ICON}px; }}"
        )
        self._show_outlines_chk.toggled.connect(
            lambda checked: self._overlay.set_show_outlines(checked)
        )
        tools_layout.addWidget(self._show_outlines_chk)

        self._text_vis_chk = QCheckBox("Selections")
        self._text_vis_chk.setChecked(True)
        self._text_vis_chk.setFixedHeight(theme.BTN_H)
        self._text_vis_chk.setFocusPolicy(Qt.NoFocus)
        self._text_vis_chk.setStyleSheet(
            f"QCheckBox {{ color: {theme.TEXT};"
            f" min-height: {theme.BTN_H}px; max-height: {theme.BTN_H}px; padding: 0px; }}"
            f"QCheckBox::indicator {{ width: {theme.BTN_ICON}px; height: {theme.BTN_ICON}px; }}"
        )
        self._text_vis_chk.toggled.connect(
            lambda checked: self._overlay.set_text_sels_visible(checked)
        )
        tools_layout.addWidget(self._text_vis_chk)
        tools_sec.add_widget(tools_wrap)
        layout.addWidget(tools_sec)

        # ── Layers section ────────────────────────────────────────────
        layers_sec = CollapsibleSection("Layers", pref_key="book_section_layers")
        layers_wrap = QWidget()
        layers_group_layout = QVBoxLayout(layers_wrap)
        layers_group_layout.setContentsMargins(0, 0, 0, 0)
        layers_group_layout.setSpacing(0)

        # Eye (visibility) button in title bar
        self._layers_visible_btn = QPushButton()
        self._layers_visible_btn.setCheckable(True)
        self._layers_visible_btn.setChecked(True)
        self._layers_visible_btn.setFixedSize(theme.BTN_H, theme.BTN_H)
        self._layers_visible_btn.setFocusPolicy(Qt.NoFocus)
        self._layers_visible_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.TITLE_BG}; border: none; }}"
            f"QPushButton:hover {{ background: {theme.TITLE_BG}; border: none; }}"
            f"QPushButton:pressed {{ background: {theme.TITLE_BG}; border: none; }}"
            f"QPushButton:checked {{ background: {theme.TITLE_BG}; border: none; }}"
        )
        self._layers_visible_btn.setIcon(_svg_icon("eye-solid", theme.BTN_ICON, theme.ACCENT))
        self._layers_visible_btn.setIconSize(QSize(theme.BTN_ICON, theme.BTN_ICON))
        self._layers_visible_btn.toggled.connect(self._on_layers_visible_toggled)
        layers_sec.set_header_widget(self._layers_visible_btn)

        self._layer_panel = _LayerPanel()
        self._layer_panel.setMinimumHeight(100)
        self._layer_panel.layer_selected.connect(self._on_panel_layer_selected)
        self._layer_panel.layer_deselected.connect(self._on_panel_layer_deselected)
        self._layer_panel.layer_deleted.connect(self._on_panel_layer_deleted)
        self._layer_panel.layer_renamed.connect(self._on_panel_layer_renamed)
        self._layer_panel.layers_reordered.connect(self._on_panel_layers_reordered)
        self._layer_panel.layer_visibility_toggled.connect(self._on_panel_layer_visibility)
        self._layer_panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layers_group_layout.addWidget(self._layer_panel, stretch=1)

        layers_sec.add_widget(layers_wrap)
        layers_sec.set_fill_vertical(True)
        layers_index = layout.count()
        layout.addWidget(layers_sec)

        def _sync_layers_section_stretch(expanded: bool) -> None:
            layout.setStretch(layers_index, 1 if expanded else 0)

        layers_sec.expandedChanged.connect(_sync_layers_section_stretch)
        _sync_layers_section_stretch(layers_sec.is_expanded())
        layout.addStretch()

        self._workspace_tabs = QTabWidget()
        self._workspace_tabs.setDocumentMode(True)
        self._workspace_tabs.tabBar().setDrawBase(False)
        self._workspace_tabs.tabBar().setExpanding(False)
        self._workspace_tabs.tabBar().setUsesScrollButtons(False)
        self._workspace_tabs.setFocusPolicy(Qt.NoFocus)
        self._workspace_tabs.tabBar().setFocusPolicy(Qt.NoFocus)
        self._workspace_tabs.setStyleSheet(_WORKSPACE_TABS_STYLESHEET)

        book_workspace = QWidget()
        book_workspace_layout = QVBoxLayout(book_workspace)
        book_workspace_layout.setContentsMargins(0, 0, 0, 0)
        book_workspace_layout.setSpacing(0)
        book_workspace_layout.addWidget(scroll)

        engr_workspace = self._build_engraving_workspace_panel()
        self._book_workspace_idx = self._workspace_tabs.addTab(book_workspace, " Book ")
        self._engr_workspace_idx = self._workspace_tabs.addTab(engr_workspace, " Engravings ")
        self._workspace_tabs.currentChanged.connect(self._on_workspace_tab_changed)
        self._workspace_tabs.setCurrentIndex(self._book_workspace_idx)

        outer_layout.addWidget(self._workspace_tabs)
        return outer

    def _build_engraving_workspace_panel(self) -> QWidget:
        """Build the Engravings inspector domain: Filter, Sort, and Info."""
        panel = QWidget()
        panel.setStyleSheet(_TAB_CONTENT_STYLESHEET)
        section_gap = theme.SECTION_GAP
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(section_gap, section_gap, section_gap, section_gap)
        layout.setSpacing(section_gap)

        filter_sec = CollapsibleSection("Filter", pref_key="book_eng_section_filter")
        fwrap = QWidget()
        fwrap_lay = QVBoxLayout(fwrap)
        fwrap_lay.setContentsMargins(0, 0, 0, 0)
        fwrap_lay.setSpacing(0)
        fwrap_lay.addWidget(self._sil_browser.filter_panel)
        fwrap_lay.addWidget(self._sil_browser.status_bar)
        fwrap_lay.addWidget(self._sil_browser.pagination_panel)
        filter_sec.add_widget(fwrap)
        layout.addWidget(filter_sec)

        sort_sec = CollapsibleSection("Sort", pref_key="book_eng_section_sort")
        self._eng_sort_combo = QComboBox()
        self._eng_sort_combo.setFocusPolicy(Qt.NoFocus)
        self._eng_sort_combo.addItem("Catalog", userData="catalog")
        self._eng_sort_combo.addItem("Alphabetical", userData="alphabetical")
        self._eng_sort_combo.currentIndexChanged.connect(self._on_engraving_sort_changed)
        sort_sec.add_widget(self._eng_sort_combo)
        layout.addWidget(sort_sec)

        info_sec = CollapsibleSection("Info", pref_key="book_eng_section_info")
        self._eng_info_block = MetadataBlock([
            "label", "movie", "field", "shot", "dpi", "width", "height"
        ])
        info_sec.add_widget(self._eng_info_block)
        # Keep section header -> first row gap on the shared 2px grid.
        layout.addWidget(info_sec)

        layout.addStretch()
        return panel

    def _on_workspace_tab_changed(self, idx: int) -> None:
        if idx == getattr(self, "_engr_workspace_idx", -1):
            self._expand_engraving_browser()
        else:
            self._collapse_engraving_browser()

    # Debug helper removed

    # Debug helpers removed

    # Debug helpers removed

    def _toggle_inspectors(self) -> None:
        """Toggle between full-canvas mode and inspector-visible mode.

        Mirrors Illustration Visualizer's Tab behavior: hide/show the
        side inspectors as a single operation.
        """
        # Save internal panel sizes, delegate shell show/hide to WindowVisualizer,
        # and restore internal sizes when the inspector is shown again. Do not
        # modify the outer shell splitter here.
        panel = getattr(self, "_panel_splitter", None)

        # toggle inspectors: preserve behavior without debug prints

        try:
            self._saved_panel_sizes = list(panel.sizes()) if panel is not None else None
        except Exception:
            self._saved_panel_sizes = None

        # Delegate show/hide to the shell (WindowVisualizer manages outer splitter)
        super()._toggle_inspector()

        # If inspector is now visible, restore the internal panel sizes.
        try:
            if not getattr(self, "_inspector_hidden", False) and panel is not None and self._saved_panel_sizes:
                self._panel_splitter.setSizes(self._saved_panel_sizes)
        except Exception:
            pass

        # Ensure internal engravings panel state matches the active tab.
        if getattr(self, "_workspace_tabs", None) and self._workspace_tabs.currentIndex() != getattr(self, "_engr_workspace_idx", -1):
            self._collapse_engraving_browser()

        # end toggle inspectors

    def _expand_engraving_browser(self) -> None:
        panel = getattr(self, "_panel_splitter", None)
        # expand engraving browser (no debug prints)
        if panel is None:
            return
        sizes = list(panel.sizes())
        # only expand when currently collapsed (left size == 0)
        if len(sizes) != 2 or sizes[0] > 0:
            return
        # Restore saved internal sizes if available, otherwise use defaults.
        if self._engraving_split_sizes and len(self._engraving_split_sizes) == 2:
            panel.setSizes(list(self._engraving_split_sizes))
            return
        # Fallback defaults for internal panel widths
        mid = max(280, 360)
        right = max(_PANEL_WIDTH, 300)
        panel.setSizes([mid, right])

        # end expand engraving browser

    def _collapse_engraving_browser(self) -> None:
        panel = getattr(self, "_panel_splitter", None)
        # collapse engraving browser (no debug prints)
        if panel is None:
            return
        sizes = list(panel.sizes())
        if len(sizes) != 2:
            return
        mid, right = sizes[0], sizes[1]
        if mid > 0:
            self._engraving_split_sizes = [int(mid), int(right)]
        if mid == 0:
            return
        total = max(1, int(mid + right))
        # collapse the engraving pane internally — do not touch outer splitter
        panel.setSizes([0, total])

        # end collapse engraving browser

    def _on_engraving_sort_changed(self, _idx: int) -> None:
        mode = self._eng_sort_combo.currentData() or "catalog"
        self._sil_browser.set_sort_mode(mode)

    def _update_engraving_info(self, record: Optional[dict]) -> None:
        if record is None:
            self._eng_info_block.clear()
            return
        movie = str(record.get("movie") or "").strip()
        if not movie:
            stem = str(record.get("filename_stem") or "").strip()
            if stem:
                try:
                    from data.metadata import get_metadata
                    metas = get_metadata(self._project_path, stem)
                    if metas:
                        m = metas[0]
                        title = m.get("title", "")
                        year = m.get("year", "")
                        movie = f"{title} ({year})" if title and year else (title or stem)
                    else:
                        movie = stem
                except Exception:
                    movie = stem

        self._eng_info_block.set("label", str(record.get("label") or "—"))
        self._eng_info_block.set("movie", movie or "—")
        self._eng_info_block.set("field", str(record.get("field") or "—"))
        self._eng_info_block.set("shot", str(record.get("shot_id") or "—"))
        self._eng_info_block.set("dpi", str(record.get("dpi") or "—"))
        self._eng_info_block.set("width", str(record.get("width") or "—"))
        self._eng_info_block.set("height", str(record.get("height") or "—"))

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
            self._spread_page_edit.setText("")
            self._spread_suffix.setText("No books yet.")
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
        self._spread_page_edit.setText("")
        self._spread_suffix.setText("")
        self._loading_label.hide()

        if not pdf_rel:
            self._spread_suffix.setText("No PDF imported.")
            self._import_btn.setText("Import")
            self._import_btn.setToolTip("Import a PDF into this book")
            self._persist_current(slug)
            self._overlay.set_book_dir(None)
            self._spread_view.set_book_dir(None)
            return

        from data.book import book_dir
        pdf_path = book_dir(self._project_path, slug) / pdf_rel

        if not pdf_path.exists():
            self._spread_suffix.setText("PDF file missing.")
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
            self._spread_suffix.setText(f"Error: {exc}")
            return
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
            self._spread_page_edit.setText("")
            self._spread_suffix.setText("")
            return
        page_count = self._doc.page_count
        n = _spread_count(page_count)
        if n == 0:
            self._spread_view.clear()
            self._page_bar.set_state(0, 0)
            self._spread_page_edit.setText("")
            self._spread_suffix.setText("")
            return
        left_i, right_i = _pages_for_spread(self._spread_idx, page_count)
        self._spread_view.set_spread(self._doc, self._slug, left_i, right_i, self._spread_idx)
        self._page_bar.set_state(self._spread_idx, n)
        if left_i is None:
            self._spread_page_edit.setText("1")
        elif right_i is None:
            self._spread_page_edit.setText(str(left_i + 1))
        else:
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
        self._layers_visible_btn.setIcon(_svg_icon(icon_name, theme.BTN_ICON, color))
        self._overlay.set_layers_visible(checked)
        self._spread_view.set_layers_visible(checked)

    def _on_engraving_delete_requested(self, layer_id: str) -> None:
        """Delete only the selected page reference for an engraving layer.

        Book Visualizer never deletes engraving assets from project storage.
        """
        eng_layer = self._overlay._layer_by_id(layer_id)
        if eng_layer is None:
            return

        page = eng_layer.get("page")
        page_label = f"page {page + 1}" if page is not None else "this page"
        reply = QMessageBox.question(
            self,
            "Remove From Page",
            f"Remove this engraving from {page_label}?\n\n"
            "This only removes the page reference."
            " Project engraving assets are not deleted.",
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if reply != QMessageBox.Ok:
            return

        self._overlay._layers.remove(eng_layer)
        if self._overlay._sel_id == layer_id:
            self._overlay._sel_id = None
            self._overlay._sel_pt = None
        self._overlay.update()
        self._layer_panel.remove_layer(layer_id)

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
        self._sil_browser._browser.reload()

    def _delete_engraving_files(self, eng_id: str) -> None:
        """Deprecated in Book Visualizer: project-level delete is unsupported."""
        return

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
        if not lid:
            return
        layer = self._overlay._layer_by_id(lid)
        if not layer or layer.get("type") != "Image":
            return

        if hasattr(self, "_workspace_tabs"):
            self._workspace_tabs.setCurrentIndex(self._engr_workspace_idx)

        page_pt_w = 0.0
        page_pt_h = 0.0
        if self._doc is not None:
            try:
                pdf_page = self._doc[layer.get("page")]
                page_pt_w = float(pdf_page.rect.width)
                page_pt_h = float(pdf_page.rect.height)
            except Exception:
                pass

        dpi = "—"
        obj_w = "—"
        obj_h = "—"
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
            dpi = str(sc.get("page_dpi") or "—")
            wmm = sc.get("object_width_mm")
            hmm = sc.get("object_height_mm")
            obj_w = f"{wmm} mm" if wmm else "—"
            obj_h = f"{hmm} mm" if hmm else "—"
        except Exception:
            pass

        self._update_engraving_info({
            "label": layer.get("label") or layer.get("name") or "—",
            "filename_stem": layer.get("filename_stem") or "",
            "field": layer.get("field") or "—",
            "shot_id": layer.get("shot_id") or "—",
            "dpi": dpi,
            "width": obj_w,
            "height": obj_h,
        })

    def _on_browser_engraving_selected(self, entry: dict) -> None:
        """Compatibility alias for browser selection callbacks."""
        self._on_browser_thumbnail_selected(entry)

    def _on_browser_thumbnail_selected(self, record: dict) -> None:
        """Update Engravings Info when a browser item is selected."""
        if self._overlay._sel_id is not None:
            self._overlay._sel_id = None
            self._overlay._sel_pt = None
            self._overlay.update()
            self._layer_panel.select_layer(None)
        if hasattr(self, "_workspace_tabs"):
            self._workspace_tabs.setCurrentIndex(self._engr_workspace_idx)

        page_pt_w = 0.0
        page_pt_h = 0.0
        if self._doc is not None:
            try:
                page = record.get("page")
                if page is not None:
                    pdf_page = self._doc[int(page)]
                    page_pt_w = float(pdf_page.rect.width)
                    page_pt_h = float(pdf_page.rect.height)
            except Exception:
                pass

        dpi = "—"
        obj_w = "—"
        obj_h = "—"
        try:
            from services.engraving_generate import build_size_context
            sc = build_size_context(
                preprocessing_size=record.get("preprocessing_size") or [],
                preprocess_dpi=record.get("preprocess_dpi") or 0,
                page_pt_w=page_pt_w,
                page_pt_h=page_pt_h,
                width_frac=record.get("width", 0.0),
                height_frac=record.get("height", 0.0),
            )
            dpi = str(sc.get("page_dpi") or "—")
            wmm = sc.get("object_width_mm")
            hmm = sc.get("object_height_mm")
            obj_w = f"{wmm} mm" if wmm else "—"
            obj_h = f"{hmm} mm" if hmm else "—"
        except Exception:
            pass

        self._update_engraving_info({
            "label": record.get("label") or record.get("name") or "—",
            "filename_stem": record.get("filename_stem") or "",
            "field": record.get("field") or "—",
            "shot_id": record.get("shot_id") or "—",
            "dpi": dpi,
            "width": obj_w,
            "height": obj_h,
        })

    def _on_open_in_illustration(self, filename_stem: str, field: str, label: str = "", shot_id: str = "") -> None:
        """Open the Illustration Visualizer filtered to the film, field, label and shot."""
        if not filename_stem:
            return
        try:
            from visualizers.illustration_visualizer import open_at_illustration
            open_at_illustration(
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
                "movie",
                shot_id=shot_id or "",
                loop=True,
            )
        except Exception:
            import traceback
            traceback.print_exc()

    # ------------------------------------------------------------------
    # Layer panel callbacks

    def _on_panel_layer_selected(self, lid: str) -> None:
        self._overlay.select_layer(lid)

    def _on_panel_layer_deselected(self) -> None:
        self._overlay.select_layer(None)

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
            mirror_id = layer.get("mirror_id")
            if mirror_id:
                mirror = self._overlay._layer_by_id(mirror_id)
                if mirror is not None:
                    mirror["visible"] = visible
                    self._layer_panel.update_layer_visibility(mirror_id, visible)
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
    # Illustration insertion (from the browser panel)

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
            if not in_text:
                if key in (Qt.Key_Backtab, Qt.Key_Tab) and mods & Qt.ShiftModifier and not (
                    mods & (Qt.ControlModifier | Qt.MetaModifier | Qt.AltModifier)
                ):
                    if self.isFullScreen():
                        self.showNormal()
                    else:
                        self.showFullScreen()
                    return True
                if key == Qt.Key_Tab and not (
                    mods & (Qt.ControlModifier | Qt.MetaModifier | Qt.AltModifier | Qt.ShiftModifier)
                ):
                    self._toggle_inspectors()
                    return True
                if key == Qt.Key_Escape and mods == Qt.NoModifier:
                    self.close()
                    return True
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
            self.close()
            return
        if key in (Qt.Key_Q, Qt.Key_W) and mods == Qt.ControlModifier:
            self.close()
            return
        if key in (Qt.Key_Backtab, Qt.Key_Tab) and mods & Qt.ShiftModifier and not (
            mods & (Qt.ControlModifier | Qt.MetaModifier | Qt.AltModifier)
        ):
            if self.isFullScreen():
                self.showNormal()
            else:
                self.showFullScreen()
            return
        if key == Qt.Key_Tab and not (
            mods & (Qt.ControlModifier | Qt.MetaModifier | Qt.AltModifier | Qt.ShiftModifier)
        ):
            self._toggle_inspectors()
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

