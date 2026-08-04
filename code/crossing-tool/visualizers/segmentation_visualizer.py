#!/usr/bin/env python3
"""Segmentation Visualizer — interactive SAM-3 concept segmentation explorer.

Browse movies → scenes → shots, view each shot's best frame, run SAM-3
concept segmentation on it, and inspect every returned mask.

Built on the shared Visualizer Framework:
    WindowVisualizer -> Inspector -> TabbedPanel -> Tab -> TabPanel
        -> CollapsibleSection -> MetadataBlock -> InspectorValue

Browser (left):
    The frame canvas, with mask/blob overlays and hover highlighting.

Inspector (right), "Segmentation" tab:
    Navigate       — media type / movie / scene / shot
    Concept        — the SAM-3 concept text to segment
    Tools          — Run Segmentation + status
    Current Shot   — film / scene / shot / frame / model / blob count
    Hovered Blob   — index / area / bbox / iou / stability of the hovered mask

Launched via:
    crossing visualizer segmentation
    crossing visualizer segmentation --media gameplay

Also reachable from the Project Visualizer (F6 / "Segmentation" button) or
from the Illustration Visualizer's "Open in Segmentation" action.

Keyboard shortcuts:
  Left / Right              — previous / next frame (Shift = 1 second)
  Up / Down                 — previous / next shot
  PgUp / PgDn               — previous / next scene
  Home / End                — previous / next movie
  s                         — run segmentation
  Tab                       — toggle inspector panel
  Shift+Tab                 — toggle fullscreen
  Escape / Ctrl+Q / Ctrl+W  — close
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from styles import theme

# Fix Qt plugin conflict with OpenCV — del env var before first PyQt5 import
if "QT_QPA_PLATFORM_PLUGIN_PATH" in os.environ:
    del os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"]

from PyQt5.QtCore import Qt, QEvent, QPoint, QThread, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
    QPainter,
    QPen,
    QPixmap,
    QPolygon,
)

from tool.shortcuts import (
    KEY_PREV_TITLE, KEY_NEXT_TITLE,
    KEY_PREV_ITEM, KEY_NEXT_ITEM,
    KEY_PREV_SHOT, KEY_NEXT_SHOT,
    KEY_PREV_FRAME, KEY_NEXT_FRAME,
)

# Framework components
from visualizers.window_visualizer import WindowVisualizer
from visualizers.components.inspector import Inspector
from visualizers.components.tab_panel import TabPanel
from visualizers.components.combo_popup import canonical_combo_stylesheet, style_canonical_combo
from visualizers.components.metadata_block import MetadataBlock, status_label_stylesheet
from visualizers.components.sweep_bar import SweepBar


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MODEL = "sam3.pt"

# 24 visually distinct blob overlay colours (R, G, B)
_BLOB_COLORS: list[tuple[int, int, int]] = [
    (255,  80,   0),   # orange
    (  0, 200, 255),   # cyan
    (180,   0, 255),   # purple
    (  0, 255,  80),   # green
    (255, 220,   0),   # yellow
    (255,   0, 120),   # hot pink
    (  0, 120, 255),   # blue
    (160, 255,   0),   # lime
    (255, 100, 180),   # rose
    (  0, 255, 200),   # teal
    (255, 160,   0),   # amber
    (100,   0, 255),   # indigo
    (200, 255,   0),   # chartreuse
    (255,  50, 200),   # magenta
    (  0, 180, 120),   # emerald
    (255,  80,  80),   # coral
    ( 80, 120, 255),   # periwinkle
    (200, 100,   0),   # brown-orange
    (  0, 255, 100),   # mint
    (255, 200, 100),   # peach
    (120, 255, 220),   # aqua
    (255,  80, 220),   # orchid
    ( 80, 200,   0),   # olive green
    (200, 160, 255),   # lavender
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _display_name(filename: str) -> str:
    """Strip TMDb suffix from a video filename for clean display."""
    name = Path(filename).stem
    return re.sub(r"\s*\{tmdb-\d+\}", "", name).strip()


def _mask_to_display_polygon(mask: np.ndarray) -> list[list[int]] | None:
    """Convert a binary mask to a polygon for visual overlay.

    Uses a slightly finer simplification than the silhouette pipeline
    (0.3 % of perimeter) for cleaner display without excessive vertex count.
    Returns None when no valid contour is found.
    """
    mask_u8 = (np.asarray(mask, dtype=bool).astype(np.uint8)) * 255
    contours, _ = cv2.findContours(mask_u8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    main = max(contours, key=cv2.contourArea)
    if cv2.contourArea(main) < 50:
        return None
    perimeter = cv2.arcLength(main, True)
    if perimeter <= 0:
        return None
    epsilon = 0.003 * perimeter
    approx = cv2.approxPolyDP(main, epsilon, True)
    if len(approx) < 3:
        return None
    squeezed = approx.squeeze(axis=1)
    if squeezed.ndim != 2 or squeezed.shape[1] != 2:
        return None
    return squeezed.tolist()


def _load_best_frame(
    project_path: str,
    media_type: str,
    filename: str,
    shot_id: str,
    shot: dict,
) -> Optional[np.ndarray]:
    """Load the best frame for a shot as a BGR numpy array.

    Priority:
      1. Pre-computed best-frame PNG cache.
      2. Video extraction at the midpoint of the shot.

    Returns None on failure.
    """
    from services.frame_match import best_frame_path

    # 1. Cached best-frame PNG
    bf_png = best_frame_path(project_path, media_type, filename, shot_id)
    if bf_png.exists():
        bgr = cv2.imread(str(bf_png))
        if bgr is not None:
            return bgr

    # 2. Video extraction fallback
    video_path = Path(project_path) / "media" / "videos" / media_type / filename
    if not video_path.exists():
        return None

    start = int(shot.get("start_frame") or 0)
    end = int(shot.get("end_frame") or start)
    mid_frame = (start + end) // 2

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return None
    cap.set(cv2.CAP_PROP_POS_FRAMES, mid_frame)
    ret, frame = cap.read()
    cap.release()
    return frame if ret else None


# ---------------------------------------------------------------------------
# Canvas widget
# ---------------------------------------------------------------------------

class _CanvasWidget(QLabel):
    """Image canvas that overlays SAM-3 concept segmentation polygons and tracks hover state."""

    hover_changed = pyqtSignal(int)  # emits blob index, or -1 when leaving

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(400, 300)
        self.setStyleSheet(f"background: {theme.CANVAS_BG};")
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

        self._bgr: Optional[np.ndarray] = None
        self._blobs: list[dict] = []
        self._hovered: int = -1
        # Pixmap cache — invalidated when frame or canvas size changes
        self._cached_pixmap: Optional[QPixmap] = None
        self._cached_size: tuple[int, int] = (-1, -1)

    # ------------------------------------------------------------------
    # Data setters

    def set_frame(self, bgr: Optional[np.ndarray]) -> None:
        self._bgr = bgr
        self._blobs = []
        self._hovered = -1
        self._cached_pixmap = None
        self.update()

    def set_blobs(self, blobs: list[dict]) -> None:
        self._blobs = blobs
        self._hovered = -1
        self.update()

    def clear_blobs(self) -> None:
        self._blobs = []
        self._hovered = -1
        self.update()

    # ------------------------------------------------------------------
    # Layout helpers

    def _compute_layout(self) -> tuple[int, int, float]:
        """Return (offset_x, offset_y, scale) fitting the image in the widget."""
        if self._bgr is None:
            return 0, 0, 1.0
        h_src, w_src = self._bgr.shape[:2]
        if w_src == 0 or h_src == 0:
            return 0, 0, 1.0
        w_c, h_c = self.width(), self.height()
        scale = min(w_c / w_src, h_c / h_src)
        sw = int(w_src * scale)
        sh = int(h_src * scale)
        return (w_c - sw) // 2, (h_c - sh) // 2, scale

    def _get_scaled_pixmap(self) -> Optional[QPixmap]:
        if self._bgr is None:
            return None
        h_src, w_src = self._bgr.shape[:2]
        ox, oy, s = self._compute_layout()
        sw, sh = int(w_src * s), int(h_src * s)
        if sw <= 0 or sh <= 0:
            return None
        if self._cached_pixmap is None or self._cached_size != (sw, sh):
            rgb = cv2.cvtColor(self._bgr, cv2.COLOR_BGR2RGB)
            qimg = QImage(
                rgb.data.tobytes(), w_src, h_src, 3 * w_src, QImage.Format_RGB888
            )
            self._cached_pixmap = QPixmap.fromImage(qimg).scaled(
                sw, sh, Qt.IgnoreAspectRatio, Qt.SmoothTransformation
            )
            self._cached_size = (sw, sh)
        return self._cached_pixmap

    # ------------------------------------------------------------------
    # Hit-testing

    def _find_blob_at(self, cx: int, cy: int) -> int:
        """Return index of smallest-area blob containing point (cx, cy), or -1."""
        ox, oy, s = self._compute_layout()
        best_idx = -1
        best_area = float("inf")
        for i, blob in enumerate(self._blobs):
            polygon = blob.get("polygon")
            if not polygon or len(polygon) < 3:
                continue
            pts = QPolygon(
                [QPoint(int(x * s + ox), int(y * s + oy)) for x, y in polygon]
            )
            if pts.containsPoint(QPoint(cx, cy), Qt.OddEvenFill):
                area = blob.get("area", float("inf"))
                if area < best_area:
                    best_area = area
                    best_idx = i
        return best_idx

    # ------------------------------------------------------------------
    # Mouse events

    def mouseMoveEvent(self, event) -> None:
        new_hover = self._find_blob_at(event.x(), event.y())
        if new_hover != self._hovered:
            self._hovered = new_hover
            self.hover_changed.emit(new_hover)
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        if self._hovered != -1:
            self._hovered = -1
            self.hover_changed.emit(-1)
            self.update()
        super().leaveEvent(event)

    # ------------------------------------------------------------------
    # Painting

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), QColor(theme.CANVAS_BG))

        pix = self._get_scaled_pixmap()
        if pix is None:
            painter.setPen(QColor(theme.TEXT_DIM))
            painter.setFont(QFont(theme.FAMILY_UI, theme.BASE_PT))
            painter.drawText(self.rect(), Qt.AlignCenter, "No frame loaded")
            painter.end()
            return

        ox, oy, s = self._compute_layout()
        painter.drawPixmap(ox, oy, pix)

        # Blob overlays — normal blobs first, hovered blob on top
        for pass_idx in range(2):
            for i, blob in enumerate(self._blobs):
                is_hovered = i == self._hovered
                if pass_idx == 0 and is_hovered:
                    continue
                if pass_idx == 1 and not is_hovered:
                    continue

                polygon = blob.get("polygon")
                if not polygon or len(polygon) < 3:
                    continue

                r, g, b = _BLOB_COLORS[i % len(_BLOB_COLORS)]
                pts = QPolygon(
                    [QPoint(int(x * s + ox), int(y * s + oy)) for x, y in polygon]
                )

                fill_alpha = 90 if is_hovered else 30
                painter.setBrush(QBrush(QColor(r, g, b, fill_alpha)))
                painter.setPen(Qt.NoPen)
                painter.drawPolygon(pts)

                pen = QPen(QColor(r, g, b, 230 if is_hovered else 180))
                pen.setWidth(3 if is_hovered else 1)
                painter.setPen(pen)
                painter.setBrush(Qt.NoBrush)
                painter.drawPolygon(pts)

                if is_hovered and len(polygon) >= 3:
                    cx = int(sum(pt[0] for pt in polygon) / len(polygon) * s + ox)
                    cy = int(sum(pt[1] for pt in polygon) / len(polygon) * s + oy)
                    lbl = f"#{i}"
                    # Drop shadow
                    painter.setPen(QColor(0, 0, 0, 200))
                    for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                        painter.drawText(
                            cx - 12 + dx, cy - 8 + dy, 28, 16, Qt.AlignCenter, lbl
                        )
                    painter.setPen(QColor(255, 255, 255, 240))
                    painter.setFont(QFont(theme.FAMILY_MONO, 8, QFont.Bold))
                    painter.drawText(cx - 12, cy - 8, 28, 16, Qt.AlignCenter, lbl)

        painter.end()

    def resizeEvent(self, event) -> None:
        self._cached_pixmap = None  # force rescale on next paint
        super().resizeEvent(event)
        self.update()


# ---------------------------------------------------------------------------
# Background segmentation worker
# ---------------------------------------------------------------------------

class _SegmentationWorker(QThread):
    """Run concept segmentation in a background thread."""

    masks_ready = pyqtSignal(list, str)   # (raw_masks, effective_model_name)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(
        self,
        project_path: str,
        model_name: str,
        bgr: np.ndarray,
        concept: str,
    ) -> None:
        super().__init__()
        self._project_path = project_path
        self._model_name = model_name
        self._bgr = bgr.copy()
        self._concept = concept

    def run(self) -> None:
        try:
            from PIL import Image
            from services.silhouette import load_sam_model

            self.progress.emit(f"Loading model '{self._model_name}'…")
            segmenter, effective_name, device = load_sam_model(
                self._project_path, self._model_name
            )
            self.progress.emit(f"Running segmentation for '{self._concept}'…")
            rgb = cv2.cvtColor(self._bgr, cv2.COLOR_BGR2RGB)
            image_pil = Image.fromarray(rgb)
            masks = segmenter.segment_concept(image_pil, self._concept)
            # Sort by predicted_iou desc then stability_score desc
            masks.sort(
                key=lambda m: (
                    -m.get("stability_score", 0.0),
                    -m.get("predicted_iou", 0.0),
                )
            )
            self.masks_ready.emit(masks, effective_name)
        except Exception as exc:
            self.error.emit(str(exc))


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class SAMExplorer(WindowVisualizer):
    """Segmentation visualizer.

    Browse movies → scenes → shots, see each shot's best frame, run segmentation
    concept segmentation on it, and inspect every mask returned.
    """

    def __init__(
        self,
        project_path: str,
        media_type: str = "movie",
        model_name: str = _DEFAULT_MODEL,
    ) -> None:
        # Instance attributes must be set before super().__init__() since the
        # base class calls create_browser()/create_inspector() synchronously.
        self._project_path = project_path
        self._media_type = media_type
        self._model_name = model_name

        # Navigation state
        self._films: list[dict] = []
        self._film_idx: int = -1   # -1 == no movie selected ("<Title>" placeholder shown)
        self._filename: str = ""

        self._shots: list[dict] = []
        self._scene_nums: list[str] = []
        self._shots_by_scene: dict[str, list[dict]] = {}
        self._scene_idx: int = -1  # -1 == no scene selected ("<Scene>" placeholder shown)
        self._shot_idx: int = -1   # -1 == no shot selected ("<Shot>" placeholder shown)

        # Frame + segmentation state
        self._bgr: Optional[np.ndarray] = None
        self._frame_source: str = ""
        self._current_frame_num: int = 0
        self._video_fps: float = 0.0
        self._masks: list[dict] = []
        self._blobs: list[dict] = []
        self._effective_model: str = ""
        self._seg_worker: Optional[_SegmentationWorker] = None

        # Concept input
        self._concept: str = ""

        # Guard against recursive combo signal handling
        self._updating: bool = False

        super().__init__(pref_key="window_sam_explorer")
        self.setWindowTitle("Segmentation")
        self.setMinimumSize(780, 480)
        self.resize(1200, 700)

        self._load_films()

    def closeEvent(self, event) -> None:
        if self._seg_worker and self._seg_worker.isRunning():
            self._seg_worker.terminate()
            self._seg_worker.wait(2000)
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # UI construction

    def create_browser(self) -> QWidget:
        self._canvas = _CanvasWidget()
        self._canvas.hover_changed.connect(self._on_hover_changed)
        return self._canvas

    def create_inspector(self) -> QWidget:
        panel = TabPanel()

        # ── Navigate section — media type / movie / scene / shot ────
        nav_wrap = QWidget()
        nav_layout = QVBoxLayout(nav_wrap)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(theme.SECTION_GAP)

        self._media_type_combo = QComboBox()
        self._media_type_combo.addItems(["movie", "gameplay"])
        self._media_type_combo.setCurrentText(self._media_type)
        style_canonical_combo(self._media_type_combo)
        self._media_type_combo.currentTextChanged.connect(self._on_media_type_changed)
        nav_layout.addWidget(self._media_type_combo)

        self._movie_combo = QComboBox()
        style_canonical_combo(self._movie_combo)
        self._movie_combo.addItem("<Title>", userData=None)
        self._movie_combo.currentIndexChanged.connect(self._on_movie_changed)
        self._refresh_combo_dim_style(self._movie_combo)
        nav_layout.addWidget(self._movie_combo)

        self._scene_combo = QComboBox()
        style_canonical_combo(self._scene_combo)
        self._scene_combo.addItem("<Scene>", userData=None)
        self._scene_combo.currentIndexChanged.connect(self._on_scene_changed)
        self._refresh_combo_dim_style(self._scene_combo)
        nav_layout.addWidget(self._scene_combo)

        self._shot_combo = QComboBox()
        style_canonical_combo(self._shot_combo)
        self._shot_combo.addItem("<Shot>", userData=None)
        self._shot_combo.currentIndexChanged.connect(self._on_shot_changed)
        self._refresh_combo_dim_style(self._shot_combo)
        nav_layout.addWidget(self._shot_combo)

        panel.add_section("Navigate", nav_wrap, pref_key="sam_explorer_section_navigate")

        # ── Concept section ──────────────────────────────────────────
        concept_wrap = QWidget()
        concept_layout = QVBoxLayout(concept_wrap)
        concept_layout.setContentsMargins(0, 0, 0, 0)
        concept_layout.setSpacing(theme.SECTION_GAP)
        self._concept_edit = QLineEdit()
        self._concept_edit.setPlaceholderText("e.g. horse, revolver, hat…")
        self._concept_edit.setStyleSheet(
            f"background: {theme.INPUT_BG}; color: {theme.TEXT};"
            f" font-family: '{theme.FAMILY_MONO}'; font-size: {theme.BASE_PT}pt;"
        )
        self._concept_edit.textChanged.connect(self._on_concept_changed)
        concept_layout.addWidget(self._concept_edit)
        panel.add_section("Concept", concept_wrap, pref_key="sam_explorer_section_concept")

        # ── Tools section — run action + status ──────────────────────
        tools_wrap = QWidget()
        tools_layout = QVBoxLayout(tools_wrap)
        tools_layout.setContentsMargins(0, 0, 0, 0)
        tools_layout.setSpacing(theme.SECTION_GAP)

        self._run_btn = QPushButton("▶  Run Segmentation")
        self._run_btn.setStyleSheet(theme.action_button_stylesheet())
        self._run_btn.setFocusPolicy(Qt.NoFocus)
        self._run_btn.clicked.connect(self._run_segmentation)
        tools_layout.addWidget(self._run_btn)

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(status_label_stylesheet())
        self._status_lbl.setWordWrap(True)
        tools_layout.addWidget(self._status_lbl)

        tools_sec = panel.add_section("Tools", tools_wrap, pref_key="sam_explorer_section_tools")

        # Accent sweep-bar shown on the section title while segmentation runs
        # in the background — same loading behavior as Cloud's Tools section
        # and Illustration's Silhouettes/Engravings tabs.
        self._loading_bar = SweepBar(self)
        self._loading_timer = QTimer(self)
        self._loading_timer.setInterval(20)   # ~50 fps
        self._loading_timer.timeout.connect(self._loading_bar.tick)
        tools_sec.set_subbar(self._loading_bar)

        # ── Current Shot section ─────────────────────────────────────
        shot_info_wrap = QWidget()
        shot_info_layout = QVBoxLayout(shot_info_wrap)
        shot_info_layout.setContentsMargins(0, 0, 0, 0)
        shot_info_layout.setSpacing(0)
        self._frame_info = MetadataBlock(["film", "scene", "shot", "frame", "model", "blobs"])
        shot_info_layout.addWidget(self._frame_info)
        panel.add_section("Current Shot", shot_info_wrap, pref_key="sam_explorer_section_shot")

        # ── Hovered Blob section ──────────────────────────────────────
        blob_info_wrap = QWidget()
        blob_info_layout = QVBoxLayout(blob_info_wrap)
        blob_info_layout.setContentsMargins(0, 0, 0, 0)
        blob_info_layout.setSpacing(0)
        self._blob_info = MetadataBlock(["#", "area", "bbox", "iou", "stability"])
        blob_info_layout.addWidget(self._blob_info)
        panel.add_section("Hovered Blob", blob_info_wrap, pref_key="sam_explorer_section_blob")

        for _w in (
            self._media_type_combo, self._movie_combo,
            self._scene_combo, self._shot_combo,
            self._concept_edit,
        ):
            _w.installEventFilter(self)

        self._inspector = Inspector()
        self._inspector.add_tab(panel, "Segmentation")
        return self._inspector

    # ------------------------------------------------------------------
    # Data loading

    def _on_media_type_changed(self, media_type: str) -> None:
        if media_type == self._media_type:
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
        self._movie_combo.clear()
        self._movie_combo.addItem("<Title>", userData=None)
        for i, entry in enumerate(self._films):
            name = entry.get("title") or _display_name(entry.get("filename", "?"))
            year = entry.get("year", "")
            label = f"{name} ({year})" if year else name
            self._movie_combo.addItem(label, userData=i)
        self._movie_combo.setCurrentIndex(0)
        self._updating = False
        self._refresh_combo_dim_style(self._movie_combo)

        # No film chosen yet — require an explicit pick (matches Scene/Shot).
        self._film_idx = -1
        self._filename = ""
        self._reset_navigation_downstream()

    def _on_movie_changed(self, idx: int) -> None:
        if self._updating:
            return
        data = self._movie_combo.itemData(idx) if idx >= 0 else None
        if data is None:
            # "<Title>" placeholder selected — no film chosen; clear the
            # canvas and reset Scene/Shot back to their own placeholders.
            self._film_idx = -1
            self._filename = ""
            self._video_fps = 0.0
            self._reset_navigation_downstream()
            return
        self._film_idx = int(data)
        self._filename = self._films[self._film_idx].get("filename", "")
        self._video_fps = 0.0
        self._drop_placeholder_after_selection(self._movie_combo)
        self._load_shots()

    def _set_combo_placeholder(self, combo: QComboBox, text: str) -> None:
        """Reset *combo* to a single dimmed placeholder item (e.g. "<Scene>")."""
        self._updating = True
        combo.clear()
        combo.addItem(text, userData=None)
        self._updating = False
        self._refresh_combo_dim_style(combo)

    def _refresh_combo_dim_style(self, combo: QComboBox) -> None:
        """Dim *combo*'s text while its current item is the placeholder."""
        color = theme.TEXT_DIM if combo.currentData() is None else theme.TEXT
        combo.setStyleSheet(canonical_combo_stylesheet(color))

    def _drop_placeholder_after_selection(self, combo: QComboBox) -> None:
        """Permanently remove *combo*'s leading placeholder item now that a
        real item has been chosen — once chosen, the placeholder (e.g.
        "<Scene>") is no longer offered as a choice until the combo is
        reset back to its placeholder state.
        """
        if combo.count() == 0 or combo.itemData(0) is not None:
            return  # no placeholder present
        keep_data = combo.currentData()
        if keep_data is None:
            return  # placeholder is still the current selection
        self._updating = True
        combo.removeItem(0)
        new_idx = combo.findData(keep_data)
        if new_idx >= 0:
            combo.setCurrentIndex(new_idx)
        self._updating = False
        self._refresh_combo_dim_style(combo)

    def _clear_current_frame(self) -> None:
        """Clear the canvas/frame and per-frame info — no shot is selected."""
        self._canvas.set_frame(None)
        self._bgr = None
        self._masks = []
        self._blobs = []
        self._frame_info.clear()
        self._blob_info.clear()

    def _reset_navigation_downstream(self) -> None:
        """Reset Scene/Shot to their placeholders and clear the canvas —
        used whenever no film is selected, or before loading a newly
        selected film's shotlist.
        """
        self._shots = []
        self._scene_nums = []
        self._shots_by_scene = {}
        self._scene_idx = -1
        self._shot_idx = -1
        self._set_combo_placeholder(self._scene_combo, "<Scene>")
        self._set_combo_placeholder(self._shot_combo, "<Shot>")
        self._clear_current_frame()
        self._status_lbl.setText("")

    def _load_shots(self) -> None:
        """Load the shotlist for the current film and populate the scene combo."""
        from data.shotlist import read_shotlist, attach_shot_ids
        from data.media_id import compute_media_id

        self._reset_navigation_downstream()

        if not self._filename:
            return

        try:
            shots = read_shotlist(self._project_path, self._filename, self._media_type)
        except FileNotFoundError:
            self._status_lbl.setText("No shotlist found for this film.")
            return
        except Exception as exc:
            self._status_lbl.setText(f"Error loading shotlist: {exc}")
            return

        # Normalise numeric fields
        for s in shots:
            for k in ("start_frame", "end_frame"):
                v = s.get(k)
                if isinstance(v, str):
                    try:
                        s[k] = int(v)
                    except ValueError:
                        s[k] = 0

        # Attach stable shot IDs
        meta_entry = self._films[self._film_idx] if self._films else {}
        media_id = compute_media_id(meta_entry, self._media_type)
        attach_shot_ids(shots, media_id)

        # Merge best_frame info from annotation JSON (for display only)
        try:
            import json as _json
            ann_path = (
                Path(self._project_path)
                / "data" / "annotations" / "shots"
                / self._media_type
                / f"{Path(self._filename).stem}.json"
            )
            if ann_path.exists():
                ann_entries = _json.loads(ann_path.read_text(encoding="utf-8"))
                bf_index: dict[str, dict] = {}
                for entry in ann_entries:
                    shot_block = entry.get("shot")
                    if isinstance(shot_block, dict):
                        sid = shot_block.get("shot_id")
                        bf = shot_block.get("best_frame")
                        if sid and bf:
                            bf_index[str(sid)] = bf
                for shot in shots:
                    sid = shot.get("shot_id", "")
                    if sid in bf_index and "best_frame" not in shot:
                        shot["best_frame"] = bf_index[sid]
        except Exception:
            pass  # annotation data is optional

        self._shots = shots

        # Collect ordered unique scene numbers
        seen_scenes: list[str] = []
        seen_set: set[str] = set()
        for shot in shots:
            scene = str(shot.get("Scene", "0") or "0")
            if scene not in seen_set:
                seen_scenes.append(scene)
                seen_set.add(scene)
        self._scene_nums = seen_scenes

        # Group shots by scene
        self._shots_by_scene = {sn: [] for sn in seen_scenes}
        for shot in shots:
            scene = str(shot.get("Scene", "0") or "0")
            self._shots_by_scene[scene].append(shot)

        # Populate the scene combo with a "<Scene>" placeholder followed by
        # the real scenes — the user must explicitly pick a scene (and then
        # a shot); nothing is auto-selected or auto-navigated to.
        self._updating = True
        self._scene_combo.clear()
        self._scene_combo.addItem("<Scene>", userData=None)
        for sn in seen_scenes:
            n = len(self._shots_by_scene[sn])
            self._scene_combo.addItem(f"Scene {sn}  ({n} shots)", userData=sn)
        self._scene_combo.setCurrentIndex(0)
        self._updating = False
        self._refresh_combo_dim_style(self._scene_combo)

    def _on_scene_changed(self, idx: int) -> None:
        if self._updating:
            return
        data = self._scene_combo.itemData(idx) if idx >= 0 else None
        if data is None:
            # "<Scene>" placeholder selected — reset the shot combo too and
            # clear the canvas; nothing is selected downstream.
            self._scene_idx = -1
            self._shot_idx = -1
            self._set_combo_placeholder(self._shot_combo, "<Shot>")
            self._clear_current_frame()
            return
        self._scene_idx = self._scene_nums.index(data)
        self._drop_placeholder_after_selection(self._scene_combo)
        scene_shots = self._shots_by_scene.get(data, [])

        # Populate the shot combo with a "<Shot>" placeholder followed by the
        # real shots for this scene — again, no auto-selection.
        self._updating = True
        self._shot_combo.clear()
        self._shot_combo.addItem("<Shot>", userData=None)
        for i, shot in enumerate(scene_shots):
            start = shot.get("start_time") or f"f{shot.get('start_frame', '?')}"
            self._shot_combo.addItem(f"Shot {i + 1}  [{start}]", userData=i)
        self._shot_combo.setCurrentIndex(0)
        self._updating = False
        self._refresh_combo_dim_style(self._shot_combo)
        self._shot_idx = -1
        self._clear_current_frame()

    def _on_shot_changed(self, idx: int) -> None:
        if self._updating:
            return
        data = self._shot_combo.itemData(idx) if idx >= 0 else None
        if data is None:
            # "<Shot>" placeholder selected — no shot chosen; clear canvas.
            self._shot_idx = -1
            self._clear_current_frame()
            return
        self._shot_idx = int(data)
        self._drop_placeholder_after_selection(self._shot_combo)
        self._masks = []
        self._blobs = []
        self._canvas.clear_blobs()
        self._status_lbl.setText("")
        self._load_best_frame_for_current_shot()


    def _load_best_frame_for_current_shot(self) -> None:
        scene = (
            self._scene_nums[self._scene_idx]
            if self._scene_nums and self._scene_idx >= 0 else ""
        )
        scene_shots = self._shots_by_scene.get(scene, [])
        if self._shot_idx < 0 or self._shot_idx >= len(scene_shots):
            return
        shot = scene_shots[self._shot_idx]
        shot_id = shot.get("shot_id", "")

        bgr = _load_best_frame(
            self._project_path,
            self._media_type,
            self._filename,
            shot_id,
            shot,
        )

        self._bgr = bgr
        self._canvas.set_frame(bgr)
        self._effective_model = ""
        self._masks = []
        self._blobs = []
        self._blob_info.clear()

        # Determine source description for the info block
        from services.frame_match import best_frame_path
        bf_png = best_frame_path(
            self._project_path, self._media_type, self._filename, shot_id
        )
        start = int(shot.get("start_frame") or 0)
        end   = int(shot.get("end_frame") or start)
        mid   = (start + end) // 2
        if bgr is not None and bf_png.exists():
            bf = shot.get("best_frame", {}) or {}
            fn = bf.get("frame")
            self._current_frame_num = int(fn) if fn is not None else mid
            self._frame_source = f"cached f{self._current_frame_num}"
        elif bgr is not None:
            self._current_frame_num = mid
            self._frame_source = f"midpoint f{mid}"
        else:
            self._current_frame_num = mid
            self._frame_source = "not found"

        self._refresh_frame_info()

    def _refresh_frame_info(self) -> None:
        film_title = ""
        if self._films and 0 <= self._film_idx < len(self._films):
            entry = self._films[self._film_idx]
            film_title = entry.get("title") or _display_name(self._filename)

        scene = (
            self._scene_nums[self._scene_idx]
            if self._scene_nums and self._scene_idx >= 0 else "—"
        )
        scene_shots = self._shots_by_scene.get(scene, []) if self._scene_nums else []
        shot = scene_shots[self._shot_idx] if 0 <= self._shot_idx < len(scene_shots) else {}
        shot_id = shot.get("shot_id", "—")

        model_lbl = self._effective_model or self._model_name
        blob_lbl = str(len(self._blobs)) if self._blobs else "—"

        # film/shot values are handed to InspectorValue as-is — its own
        # character wrap (see visualizers/components/inspector_value.py)
        # keeps unbroken strings from forcing the Inspector wider, so no
        # local truncation is needed here.
        self._frame_info.set("film", film_title or "—")
        self._frame_info.set("scene", f"Scene {scene}")
        self._frame_info.set("shot", shot_id)
        self._frame_info.set("frame", self._frame_source or "—")
        self._frame_info.set("model", model_lbl)
        self._frame_info.set("blobs", blob_lbl)

    # ------------------------------------------------------------------
    # Segmentation execution

    def _on_concept_changed(self, text: str) -> None:
        self._concept = text.strip()

    def _run_segmentation(self) -> None:
        if self._bgr is None:
            self._status_lbl.setText("No frame loaded — select a shot first.")
            return
        if not self._concept:
            self._status_lbl.setText("Enter a concept before running SAM-3.")
            return
        if self._seg_worker and self._seg_worker.isRunning():
            return

        self._run_btn.setEnabled(False)
        self._status_lbl.setText(f"Starting segmentation for '{self._concept}'…")
        self._masks = []
        self._blobs = []
        self._canvas.clear_blobs()
        self._loading_bar.start()
        self._loading_timer.start()

        self._seg_worker = _SegmentationWorker(
            self._project_path, self._model_name, self._bgr, self._concept
        )
        self._seg_worker.progress.connect(self._status_lbl.setText)
        self._seg_worker.masks_ready.connect(self._on_masks_ready)
        self._seg_worker.error.connect(self._on_seg_error)
        self._seg_worker.start()

    def _on_masks_ready(self, raw_masks: list, effective_model: str) -> None:
        self._loading_timer.stop()
        self._loading_bar.stop()
        self._run_btn.setEnabled(True)
        self._effective_model = effective_model
        self._masks = raw_masks

        h_src, w_src = self._bgr.shape[:2] if self._bgr is not None else (1, 1)
        total_px = max(h_src * w_src, 1)

        blobs = []
        for m in raw_masks:
            seg = m.get("segmentation")
            if seg is None:
                continue
            polygon = _mask_to_display_polygon(np.asarray(seg, dtype=bool))
            if polygon is None:
                continue
            area = int(m.get("area", 0))
            bbox = m.get("bbox", [0, 0, 0, 0])
            iou = float(m.get("predicted_iou", 0.0))
            stab = float(m.get("stability_score", 0.0))
            blobs.append(
                {
                    "polygon": polygon,
                    "area": area,
                    "area_pct": 100.0 * area / total_px,
                    "bbox": bbox,
                    "predicted_iou": iou,
                    "stability_score": stab,
                }
            )

        self._blobs = blobs
        self._canvas.set_blobs(blobs)
        self._status_lbl.setText(f"{len(blobs)} blob(s) found.")
        self._refresh_frame_info()

    def _on_seg_error(self, msg: str) -> None:
        self._loading_timer.stop()
        self._loading_bar.stop()
        self._run_btn.setEnabled(True)
        self._status_lbl.setText(f"SAM error: {msg}")

    # ------------------------------------------------------------------
    # Hover info

    def _on_hover_changed(self, idx: int) -> None:
        if idx < 0 or idx >= len(self._blobs):
            self._blob_info.clear()
            return
        blob = self._blobs[idx]
        area = blob.get("area", 0)
        area_pct = blob.get("area_pct", 0.0)
        bbox = blob.get("bbox", [0, 0, 0, 0])
        iou = blob.get("predicted_iou", 0.0)
        stab = blob.get("stability_score", 0.0)

        bbox_str = f"{int(bbox[0])},{int(bbox[1])} {int(bbox[2])}×{int(bbox[3])}"
        self._blob_info.set("#", str(idx))
        self._blob_info.set("area", f"{area:,}  ({area_pct:.1f}%)")
        self._blob_info.set("bbox", bbox_str)
        self._blob_info.set("iou", f"{iou:.4f}")
        self._blob_info.set("stability", f"{stab:.4f}")

    # ------------------------------------------------------------------
    # Keyboard navigation

    def keyPressEvent(self, event) -> None:
        key = event.key()
        mod = event.modifiers()

        if key == Qt.Key_S:
            self._run_segmentation()
            return
        if key == KEY_PREV_FRAME:
            fps = self._video_fps if self._video_fps > 0 else 25.0
            self._navigate_frame(-int(round(fps)) if mod & Qt.ShiftModifier else -1)
            return
        if key == KEY_NEXT_FRAME:
            fps = self._video_fps if self._video_fps > 0 else 25.0
            self._navigate_frame(int(round(fps)) if mod & Qt.ShiftModifier else 1)
            return
        if key == KEY_PREV_SHOT:
            self._navigate_shot(-1)
            return
        if key == KEY_NEXT_SHOT:
            self._navigate_shot(1)
            return
        if key == KEY_PREV_ITEM:
            self._navigate_scene(-1)
            return
        if key == KEY_NEXT_ITEM:
            self._navigate_scene(1)
            return
        if key == KEY_PREV_TITLE:
            self._navigate_movie(-1)
            return
        if key == KEY_NEXT_TITLE:
            self._navigate_movie(1)
            return

        super().keyPressEvent(event)

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.KeyPress:
            key = event.key()
            mod = event.modifiers()
            ctrl_meta = bool(mod & (Qt.ControlModifier | Qt.MetaModifier))
            if obj is self._concept_edit:
                # Inside the text field: pass cursor keys through, but steal
                # shot/scene/movie nav and Shift+arrows for frame-second jump.
                if key in (KEY_PREV_SHOT, KEY_NEXT_SHOT, KEY_PREV_ITEM, KEY_NEXT_ITEM):
                    self.keyPressEvent(event)
                    return True
                if key in (KEY_PREV_FRAME, KEY_NEXT_FRAME) and (mod & Qt.ShiftModifier):
                    self.keyPressEvent(event)
                    return True
            else:
                # Combo boxes: steal all navigation keys.
                if key in (
                    KEY_PREV_SHOT, KEY_NEXT_SHOT, KEY_PREV_FRAME, KEY_NEXT_FRAME,
                    KEY_PREV_ITEM, KEY_NEXT_ITEM, KEY_PREV_TITLE, KEY_NEXT_TITLE,
                ) and not ctrl_meta:
                    self.keyPressEvent(event)
                    return True
                if key == Qt.Key_S and not ctrl_meta:
                    self.keyPressEvent(event)
                    return True
        return super().eventFilter(obj, event)

    def _load_frame_at(self, frame_num: int) -> None:
        """Load *frame_num* from the current film's video and update the canvas."""
        if not self._filename:
            return
        video_path = (
            Path(self._project_path) / "media" / "videos"
            / self._media_type / self._filename
        )
        if not video_path.exists():
            return
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            cap.release()
            return
        if self._video_fps <= 0:
            self._video_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
        ret, bgr = cap.read()
        cap.release()
        if not ret:
            return
        self._bgr = bgr
        self._current_frame_num = frame_num
        self._frame_source = f"f{frame_num}"
        self._canvas.set_frame(bgr)
        self._masks = []
        self._blobs = []
        self._canvas.clear_blobs()
        self._blob_info.clear()
        self._refresh_frame_info()

    def _navigate_frame(self, delta: int) -> None:
        """Move +/-delta frames within the current shot's start/end range."""
        scene = (
            self._scene_nums[self._scene_idx]
            if self._scene_nums and self._scene_idx >= 0 else ""
        )
        scene_shots = self._shots_by_scene.get(scene, [])
        if self._shot_idx < 0 or self._shot_idx >= len(scene_shots):
            return
        shot = scene_shots[self._shot_idx]
        start = int(shot.get("start_frame") or 0)
        end   = int(shot.get("end_frame") or start)
        new_frame = max(start, min(end, self._current_frame_num + delta))
        if new_frame != self._current_frame_num:
            self._load_frame_at(new_frame)

    def navigate_to(self, filename: str, shot_id: str, concept: str = "") -> None:
        """Navigate to the given film and shot, optionally pre-filling the concept."""
        target_film_idx = None
        for i, entry in enumerate(self._films):
            if entry.get("filename") == filename:
                target_film_idx = i
                break
        if target_film_idx is None:
            return

        if concept:
            self._concept_edit.setText(concept)

        movie_combo_idx = self._movie_combo.findData(target_film_idx)
        if movie_combo_idx < 0:
            return
        if self._movie_combo.currentIndex() != movie_combo_idx:
            self._movie_combo.setCurrentIndex(movie_combo_idx)
        else:
            self._on_movie_changed(movie_combo_idx)

        shot_id_str = str(shot_id)
        target_scene: str | None = None
        for shot in self._shots:
            if str(shot.get("shot_id", "")) == shot_id_str:
                target_scene = str(shot.get("Scene", "0") or "0")
                break
        if target_scene is None:
            return

        scene_combo_idx = self._scene_combo.findData(target_scene)
        if scene_combo_idx >= 0:
            if self._scene_combo.currentIndex() != scene_combo_idx:
                self._scene_combo.setCurrentIndex(scene_combo_idx)
            else:
                self._on_scene_changed(scene_combo_idx)

        scene_shots = self._shots_by_scene.get(target_scene, [])
        for i, shot in enumerate(scene_shots):
            if str(shot.get("shot_id", "")) == shot_id_str:
                shot_combo_idx = self._shot_combo.findData(i)
                if shot_combo_idx >= 0:
                    self._shot_combo.setCurrentIndex(shot_combo_idx)
                break

    def _navigate_shot(self, delta: int) -> None:
        scene = (
            self._scene_nums[self._scene_idx]
            if self._scene_nums and self._scene_idx >= 0 else ""
        )
        scene_shots = self._shots_by_scene.get(scene, [])
        if not scene_shots:
            return
        new_idx = self._shot_idx + delta
        if 0 <= new_idx < len(scene_shots):
            combo_idx = self._shot_combo.findData(new_idx)
            if combo_idx >= 0:
                self._shot_combo.setCurrentIndex(combo_idx)

    def _navigate_scene(self, delta: int) -> None:
        if not self._scene_nums:
            return
        new_idx = self._scene_idx + delta
        if 0 <= new_idx < len(self._scene_nums):
            combo_idx = self._scene_combo.findData(self._scene_nums[new_idx])
            if combo_idx >= 0:
                self._scene_combo.setCurrentIndex(combo_idx)

    def _navigate_movie(self, delta: int) -> None:
        if not self._films:
            return
        new_idx = self._film_idx + delta
        if 0 <= new_idx < len(self._films):
            combo_idx = self._movie_combo.findData(new_idx)
            if combo_idx >= 0:
                self._movie_combo.setCurrentIndex(combo_idx)



# ---------------------------------------------------------------------------
# Public launcher
# ---------------------------------------------------------------------------

def run_visualizer(
    project_path: str,
    media_type: str = "movie",
) -> None:
    """Create QApplication (if needed) and open the segmentation visualizer."""
    from tool import prefs as _prefs
    model_name = _prefs.get("model_segmentation", _DEFAULT_MODEL) or _DEFAULT_MODEL
    from visualizers.launcher import run_visualizer_window
    run_visualizer_window(
        "segmentation",
        lambda: SAMExplorer(project_path, media_type=media_type, model_name=model_name),
    )


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--media", default="movie")
    parsed = ap.parse_args()
    run_visualizer(parsed.project, media_type=parsed.media)
