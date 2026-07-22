#!/usr/bin/env python3
"""Illustration Visualizer — reference implementation of the Visualizer Framework.

This module demonstrates framework composition in production code:

- IllustrationBrowser provides canonical collection browsing and selection.
- IllustrationSource subclasses provide records.
- Inspector panels are composed from reusable sections/components.
- styles.theme provides shared visual language tokens.

Ownership model used here:

- Browser owns selection and browsing state.
- Source owns record access and caching.
- Inspector owns presentation and selection-scoped controls.
- Services own business logic.
- CLI commands remain canonical project operations.
- Metadata files own persistent project state.

UI actions in this visualizer should call existing services/CLI-backed flows
instead of duplicating project logic. Interactive editing tools are the only
expected case where no meaningful CLI equivalent exists.

This module remains a visualizer implementation, not a second project backend.

Two tabs in one window:

  **Catalog** (default)
    Browse extracted silhouette objects by vocabulary label.
    Shows all transparent PNG objects already in the catalog.
    Left panel: label list → film/shot tree.
    Right panel: thumbnail grid of all objects for the selected entry.
    Clicking a thumbnail shows the full object and its metadata.

  **Segmentation Visualizer**
    Interactive shot inspection.  Browse movies → scenes → shots,
    enter a concept, click Run Segmentation to see masks on the best frame.

Keyboard shortcuts (Catalog tab):
  Up / Down    — previous / next label
  Left / Right — previous / next object thumbnail (when grid focused)
  Escape / Ctrl+Q / Ctrl+W — close

Launched via:
    crossing visualizer illustration
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
from styles.theme import GripSplitter, JumpScrollBar, save_window_geometry, restore_window_geometry

# Fix Qt plugin conflict with OpenCV — del env var before first PyQt5 import
if "QT_QPA_PLATFORM_PLUGIN_PATH" in os.environ:
    del os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"]

from PyQt5.QtCore import Qt, QEvent, QPoint, QSize, QThread, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListView,
    QSlider,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from PyQt5.QtWidgets import QSlider  # kept for any future use; not used in Catalog panel
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QFont,
    QIcon,
    QImage,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QPolygon,
)

from styles.theme import svg_icon as _svg_icon

# Framework components
from tool.shortcuts import VisualizerWindow
from visualizers.components.collapsible_section import CollapsibleSection
from visualizers.components.illustration_browser import IllustrationBrowser
from visualizers.components.illustration_source import SilhouetteSource, EngravingSource


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PANEL_W    = 310
_SIDE_PANE_W = 230   # combined inspector + filter side pane
_DEFAULT_MODEL = "sam3.pt"

# (display label, data key) pairs for the cascading sort dropdowns.
# Data keys map to  <key>_score  or  <key>  fields in catalog JSON records.
# "alphabetical" is a special case: sorts by label name (case-insensitive).
_SORT_OPTS: list[tuple[str, str]] = [
    ("confidence",     "confidence"),
    ("usefulness",     "usefulness"),
    ("engraving",      "engraving"),
    ("fullness",       "fullness"),
    ("size",           "size"),
    ("completeness",   "completeness"),
    ("isolation",      "isolation"),
    ("semantic label", "semantic_label"),
    ("semantic field", "semantic_field"),
    ("alphabetical",   "alphabetical"),
    ("engraved",       "engraved_first"),
]

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


from visualizers.components.metadata_block import MetadataBlock
_InfoBlock = MetadataBlock   # backwards-compat alias — remove once all call sites updated


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class SAMExplorer(QMainWindow):
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
        super().__init__()
        self.setWindowTitle("Crossing — Segmentation Visualizer")

        self._project_path = project_path
        self._media_type = media_type
        self._model_name = model_name

        # Navigation state
        self._films: list[dict] = []
        self._film_idx: int = 0
        self._filename: str = ""

        self._shots: list[dict] = []
        self._scene_nums: list[str] = []
        self._shots_by_scene: dict[str, list[dict]] = {}
        self._scene_idx: int = 0
        self._shot_idx: int = 0

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

        self._build_ui()
        restore_window_geometry(self, "window_sam_explorer")
        self._load_films()

    def closeEvent(self, event) -> None:
        if self._seg_worker and self._seg_worker.isRunning():
            self._seg_worker.terminate()
            self._seg_worker.wait(2000)
        save_window_geometry(self, "window_sam_explorer")
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # UI construction

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Canvas (left)
        self._canvas = _CanvasWidget()
        self._canvas.hover_changed.connect(self._on_hover_changed)
        outer.addWidget(self._canvas, 1)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setFrameShadow(QFrame.Sunken)
        outer.addWidget(sep)

        # Right panel
        panel = QWidget()
        panel.setFixedWidth(_PANEL_W)
        panel.setStyleSheet(
            f"QWidget {{ background: {theme.PANEL_BG}; }}"
            f" QComboBox {{ background-color: {theme.INPUT_BG}; color: {theme.TEXT}; }}"
            f" QLabel {{ color: {theme.TEXT};"
            f"           font-family: '{theme.FAMILY_UI}';"
            f"           font-size: {theme.BASE_PT}pt; }}"
        )
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(12, 12, 12, 12)
        panel_layout.setSpacing(10)

        # Navigation group
        nav_group = QGroupBox("Navigate")
        nav_layout = QVBoxLayout(nav_group)
        nav_layout.setContentsMargins(8, 10, 8, 8)
        nav_layout.setSpacing(4)

        def _nav_row(label: str, combo: QComboBox) -> None:
            lbl = QLabel(label)
            lbl.setStyleSheet(
                f"color: {theme.TEXT_DIM}; font-size: {theme.BASE_PT}pt;"
            )
            nav_layout.addWidget(lbl)
            nav_layout.addWidget(combo)

        self._media_type_combo = QComboBox()
        self._media_type_combo.setFocusPolicy(Qt.NoFocus)
        self._media_type_combo.addItems(["movie", "gameplay"])
        self._media_type_combo.setCurrentText(self._media_type)
        self._media_type_combo.currentTextChanged.connect(self._on_media_type_changed)
        _nav_row("Type", self._media_type_combo)

        self._movie_combo = QComboBox()
        self._movie_combo.setFocusPolicy(Qt.NoFocus)
        self._movie_combo.currentIndexChanged.connect(self._on_movie_changed)
        _nav_row("Movie", self._movie_combo)

        self._scene_combo = QComboBox()
        self._scene_combo.setFocusPolicy(Qt.NoFocus)
        self._scene_combo.currentIndexChanged.connect(self._on_scene_changed)
        _nav_row("Scene", self._scene_combo)

        self._shot_combo = QComboBox()
        self._shot_combo.setFocusPolicy(Qt.NoFocus)
        self._shot_combo.currentIndexChanged.connect(self._on_shot_changed)
        _nav_row("Shot", self._shot_combo)

        panel_layout.addWidget(nav_group)

        # Concept input
        concept_lbl = QLabel("Concept")
        concept_lbl.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-size: {theme.BASE_PT}pt;"
        )
        panel_layout.addWidget(concept_lbl)
        self._concept_edit = QLineEdit()
        self._concept_edit.setPlaceholderText("e.g. horse, revolver, hat…")
        self._concept_edit.setStyleSheet(
            f"background: {theme.INPUT_BG}; color: {theme.TEXT};"
            f" font-family: '{theme.FAMILY_MONO}'; font-size: {theme.BASE_PT}pt;"
        )
        self._concept_edit.textChanged.connect(self._on_concept_changed)
        panel_layout.addWidget(self._concept_edit)

        # SAM run button
        self._run_btn = QPushButton("▶  Run Segmentation")
        self._run_btn.setFixedHeight(32)
        self._run_btn.setStyleSheet(
            f"QPushButton {{"
            f"  background: {theme.ACCENT};"
            f"  color: #000000;"
            f"  font-family: '{theme.FAMILY_UI}';"
            f"  font-size: {theme.BASE_PT + 1}pt;"
            f"  font-weight: bold;"
            f"  border: none;"
            f"  border-radius: 3px;"
            f"}}"
            f"QPushButton:hover {{ background: {theme.ACCENT}; color: {theme.ACCENT_TEXT}; }}"
            f"QPushButton:pressed {{ background: {theme.BTN_PRESSED}; }}"
            f"QPushButton:disabled {{"
            f"  background: {theme.BTN_BG}; color: {theme.TEXT_DIM};"
            f"}}"
        )
        self._run_btn.setFocusPolicy(Qt.NoFocus)
        self._run_btn.clicked.connect(self._run_segmentation)
        panel_layout.addWidget(self._run_btn)

        # Status / progress label
        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-size: {theme.BASE_PT}pt;"
        )
        self._status_lbl.setWordWrap(True)
        panel_layout.addWidget(self._status_lbl)

        # Current shot info
        frame_group = QGroupBox("Current Shot")
        frame_layout = QVBoxLayout(frame_group)
        frame_layout.setContentsMargins(8, 10, 8, 8)
        self._frame_info = MetadataBlock(["film", "scene", "shot", "frame", "model", "blobs"])
        frame_layout.addWidget(self._frame_info)
        panel_layout.addWidget(frame_group)

        # Hovered blob info
        blob_group = QGroupBox("Hovered Blob")
        blob_layout = QVBoxLayout(blob_group)
        blob_layout.setContentsMargins(8, 10, 8, 8)
        self._blob_info = MetadataBlock(["#", "area", "bbox", "iou", "stability"])
        blob_layout.addWidget(self._blob_info)
        panel_layout.addWidget(blob_group)

        # Keyboard hint
        hint = QLabel(
            "↑ ↓ shot   PgUp / PgDn scene   Home / End movie\n"
            "← → frame   Shift+←→ 1 second   s = segment"
        )
        hint.setStyleSheet(
            f"color: {theme.TEXT_DIM};"
            f" font-family: '{theme.FAMILY_MONO}';"
            f" font-size: {max(6, theme.BASE_PT - 1)}pt;"
        )
        hint.setAlignment(Qt.AlignCenter)
        panel_layout.addWidget(hint)

        panel_layout.addStretch()
        outer.addWidget(panel)

        self.setMinimumSize(780, 480)
        self.resize(1200, 700)
        for _w in (
            self._media_type_combo, self._movie_combo,
            self._scene_combo, self._shot_combo,
            self._concept_edit,
        ):
            _w.installEventFilter(self)
        self._canvas.setFocus()

    # ------------------------------------------------------------------
    # Data loading

    def _on_media_type_changed(self, media_type: str) -> None:
        if media_type == self._media_type:
            return
        self._media_type = media_type
        self._load_films()

    def _load_films(self) -> None:
        from data.metadata import get_metadata

        self._updating = True
        try:
            self._films = get_metadata(self._project_path, media_type=self._media_type)
        except Exception:
            self._films = []

        self._movie_combo.clear()
        for entry in self._films:
            name = entry.get("title") or _display_name(entry.get("filename", "?"))
            year = entry.get("year", "")
            label = f"{name} ({year})" if year else name
            self._movie_combo.addItem(label)

        self._updating = False

        if self._films:
            self._film_idx = 0
            self._movie_combo.setCurrentIndex(0)
            self._on_movie_changed(0)

    def _on_movie_changed(self, idx: int) -> None:
        if self._updating or idx < 0 or idx >= len(self._films):
            return
        self._film_idx = idx
        self._filename = self._films[idx].get("filename", "")
        self._video_fps = 0.0
        self._load_shots()

    def _load_shots(self) -> None:
        """Load the shotlist for the current film and populate scene/shot combos."""
        from data.shotlist import read_shotlist, attach_shot_ids
        from data.media_id import compute_media_id

        # Reset all dependent state
        self._shots = []
        self._scene_nums = []
        self._shots_by_scene = {}

        self._updating = True
        self._scene_combo.clear()
        self._shot_combo.clear()
        self._updating = False

        self._canvas.set_frame(None)
        self._bgr = None
        self._masks = []
        self._blobs = []
        self._frame_info.clear()
        self._blob_info.clear()
        self._status_lbl.setText("")

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

        # Populate scene combo
        self._updating = True
        self._scene_combo.clear()
        for sn in seen_scenes:
            n = len(self._shots_by_scene[sn])
            self._scene_combo.addItem(f"Scene {sn}  ({n} shots)")
        self._updating = False

        if seen_scenes:
            # Navigate to the shot with the highest best_frame.score; fall
            # back to scene 0 / shot 0 when no scored best-frames exist.
            best_shot = max(
                self._shots,
                key=lambda s: (s.get("best_frame") or {}).get("score") or 0.0,
            )
            bf_score = (best_shot.get("best_frame") or {}).get("score") or 0.0
            if bf_score > 0.0:
                target_scene = str(best_shot.get("Scene", "0") or "0")
                scene_idx = (
                    self._scene_nums.index(target_scene)
                    if target_scene in self._scene_nums else 0
                )
            else:
                target_scene = seen_scenes[0]
                scene_idx = 0
            self._scene_idx = scene_idx
            self._scene_combo.setCurrentIndex(scene_idx)
            # _on_scene_changed already loaded shot 0; override if best shot differs
            if bf_score > 0.0:
                scene_shots = self._shots_by_scene.get(target_scene, [])
                for i, shot in enumerate(scene_shots):
                    if shot is best_shot and i != self._shot_idx:
                        self._shot_combo.setCurrentIndex(i)
                        break

    def _on_scene_changed(self, idx: int) -> None:
        if self._updating or idx < 0 or idx >= len(self._scene_nums):
            return
        self._scene_idx = idx
        scene = self._scene_nums[idx]
        scene_shots = self._shots_by_scene.get(scene, [])

        self._updating = True
        self._shot_combo.clear()
        for i, shot in enumerate(scene_shots):
            start = shot.get("start_time") or f"f{shot.get('start_frame', '?')}"
            self._shot_combo.addItem(f"Shot {i + 1}  [{start}]")
        self._updating = False

        if scene_shots:
            self._shot_idx = 0
            self._shot_combo.setCurrentIndex(0)
            self._on_shot_changed(0)

    def _on_shot_changed(self, idx: int) -> None:
        if self._updating:
            return
        scene = self._scene_nums[self._scene_idx] if self._scene_nums else ""
        scene_shots = self._shots_by_scene.get(scene, [])
        if idx < 0 or idx >= len(scene_shots):
            return
        self._shot_idx = idx
        self._masks = []
        self._blobs = []
        self._canvas.clear_blobs()
        self._status_lbl.setText("")
        self._load_best_frame_for_current_shot()

    def _load_best_frame_for_current_shot(self) -> None:
        scene = self._scene_nums[self._scene_idx] if self._scene_nums else ""
        scene_shots = self._shots_by_scene.get(scene, [])
        if self._shot_idx >= len(scene_shots):
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
        if self._films and self._film_idx < len(self._films):
            entry = self._films[self._film_idx]
            film_title = entry.get("title") or _display_name(self._filename)

        scene = self._scene_nums[self._scene_idx] if self._scene_nums else "—"
        scene_shots = self._shots_by_scene.get(scene, []) if self._scene_nums else []
        shot = scene_shots[self._shot_idx] if self._shot_idx < len(scene_shots) else {}
        shot_id = shot.get("shot_id", "—")

        if len(shot_id) > 28:
            shot_id = "…" + shot_id[-26:]

        model_lbl = self._effective_model or self._model_name
        blob_lbl = str(len(self._blobs)) if self._blobs else "—"

        self._frame_info.set("film", (film_title or "—")[:28])
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

        self._seg_worker = _SegmentationWorker(
            self._project_path, self._model_name, self._bgr, self._concept
        )
        self._seg_worker.progress.connect(self._status_lbl.setText)
        self._seg_worker.masks_ready.connect(self._on_masks_ready)
        self._seg_worker.error.connect(self._on_seg_error)
        self._seg_worker.start()

    def _on_masks_ready(self, raw_masks: list, effective_model: str) -> None:
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

        if key == Qt.Key_Escape:
            self.close()
            return
        if key in (Qt.Key_Q, Qt.Key_W) and mod & Qt.ControlModifier:
            self.close()
            return
        if key == Qt.Key_S:
            self._run_segmentation()
            return
        if key == Qt.Key_Left:
            fps = self._video_fps if self._video_fps > 0 else 25.0
            self._navigate_frame(-int(round(fps)) if mod & Qt.ShiftModifier else -1)
            return
        if key == Qt.Key_Right:
            fps = self._video_fps if self._video_fps > 0 else 25.0
            self._navigate_frame(int(round(fps)) if mod & Qt.ShiftModifier else 1)
            return
        if key == Qt.Key_Up:
            self._navigate_shot(-1)
            return
        if key == Qt.Key_Down:
            self._navigate_shot(1)
            return
        if key == Qt.Key_PageUp:
            self._navigate_scene(-1)
            return
        if key == Qt.Key_PageDown:
            self._navigate_scene(1)
            return
        if key == Qt.Key_Home:
            self._navigate_movie(-1)
            return
        if key == Qt.Key_End:
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
                if key in (Qt.Key_Up, Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown):
                    self.keyPressEvent(event)
                    return True
                if key in (Qt.Key_Left, Qt.Key_Right) and (mod & Qt.ShiftModifier):
                    self.keyPressEvent(event)
                    return True
            else:
                # Combo boxes: steal all navigation keys.
                if key in (
                    Qt.Key_Up, Qt.Key_Down, Qt.Key_Left, Qt.Key_Right,
                    Qt.Key_PageUp, Qt.Key_PageDown, Qt.Key_Home, Qt.Key_End,
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
        scene = self._scene_nums[self._scene_idx] if self._scene_nums else ""
        scene_shots = self._shots_by_scene.get(scene, [])
        if self._shot_idx >= len(scene_shots):
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

        if self._movie_combo.currentIndex() != target_film_idx:
            self._movie_combo.setCurrentIndex(target_film_idx)
        else:
            self._on_movie_changed(target_film_idx)

        shot_id_str = str(shot_id)
        target_scene: str | None = None
        for shot in self._shots:
            if str(shot.get("shot_id", "")) == shot_id_str:
                target_scene = str(shot.get("Scene", "0") or "0")
                break
        if target_scene is None:
            return

        if target_scene in self._scene_nums:
            scene_idx = self._scene_nums.index(target_scene)
            if self._scene_combo.currentIndex() != scene_idx:
                self._scene_combo.setCurrentIndex(scene_idx)
            else:
                self._on_scene_changed(scene_idx)

        scene_shots = self._shots_by_scene.get(target_scene, [])
        for i, shot in enumerate(scene_shots):
            if str(shot.get("shot_id", "")) == shot_id_str:
                self._shot_combo.setCurrentIndex(i)
                break

    def _navigate_shot(self, delta: int) -> None:
        scene = self._scene_nums[self._scene_idx] if self._scene_nums else ""
        scene_shots = self._shots_by_scene.get(scene, [])
        if not scene_shots:
            return
        new_idx = self._shot_idx + delta
        if 0 <= new_idx < len(scene_shots):
            self._shot_idx = new_idx
            self._updating = True
            self._shot_combo.setCurrentIndex(new_idx)
            self._updating = False
            self._on_shot_changed(new_idx)

    def _navigate_scene(self, delta: int) -> None:
        if not self._scene_nums:
            return
        new_idx = self._scene_idx + delta
        if 0 <= new_idx < len(self._scene_nums):
            self._scene_idx = new_idx
            self._updating = True
            self._scene_combo.setCurrentIndex(new_idx)
            self._updating = False
            self._on_scene_changed(new_idx)

    def _navigate_movie(self, delta: int) -> None:
        if not self._films:
            return
        new_idx = self._film_idx + delta
        if 0 <= new_idx < len(self._films):
            self._film_idx = new_idx
            self._updating = True
            self._movie_combo.setCurrentIndex(new_idx)
            self._updating = False
            self._on_movie_changed(new_idx)


# ---------------------------------------------------------------------------
# Catalog browser
# ---------------------------------------------------------------------------

_THUMB_SIZE  = 120   # px per thumbnail cell — passed to IllustrationBrowser

# Info table keys per source mode.
_SIL_INFO_KEYS = (
    "label", "film", "shot", "frame", "confidence",
    "usefulness", "fullness", "size", "overlap",
    "semantic_label", "semantic_field", "model",
)
_ENG_INFO_KEYS = (
    "label", "film", "mode", "model", "object_id",
)


# ---------------------------------------------------------------------------
# IPC — single-instance navigate socket
# ---------------------------------------------------------------------------

def _ill_ipc_socket_path(project_path: str) -> Path:
    """Return a per-project socket file path for the Illustration Visualizer."""
    import tempfile, hashlib
    h = hashlib.md5(str(project_path).encode()).hexdigest()[:8]
    return Path(tempfile.gettempdir()) / f"crossing_illustration_{h}.sock"


class _IllIpcServer(QThread):
    """Listens on a Unix-domain socket and emits navigate_requested."""

    navigate_requested = pyqtSignal(str, str, str, str)  # film, field, label, shot_id

    def __init__(self, project_path: str, parent=None) -> None:
        super().__init__(parent)
        self._project_path = project_path
        self._running = True

    def run(self) -> None:
        import json as _json
        import socket as _socket
        sock_path = _ill_ipc_socket_path(self._project_path)
        try:
            sock_path.unlink()
        except FileNotFoundError:
            pass
        srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        try:
            srv.bind(str(sock_path))
            srv.listen(5)
            srv.settimeout(1.0)
            while self._running:
                try:
                    conn, _ = srv.accept()
                except _socket.timeout:
                    continue
                try:
                    data = b""
                    while True:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    msg = _json.loads(data.decode())
                    if msg.get("action") == "navigate":
                        self.navigate_requested.emit(
                            msg.get("film", ""),
                            msg.get("field", ""),
                            msg.get("label", ""),
                            msg.get("shot_id", ""),
                        )
                except Exception:
                    pass
                finally:
                    conn.close()
        finally:
            srv.close()
            try:
                sock_path.unlink()
            except FileNotFoundError:
                pass

    def stop(self) -> None:
        self._running = False


def _ill_ipc_send_navigate(
    project_path: str,
    film: str = "",
    field: str = "",
    label: str = "",
    shot_id: str = "",
) -> bool:
    """Send a navigate request to a running Illustration Visualizer.

    Returns True if the message was delivered, False if no server is listening.
    """
    import json as _json
    import socket as _socket
    sock_path = _ill_ipc_socket_path(project_path)
    if not sock_path.exists():
        return False
    try:
        conn = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        conn.settimeout(2.0)
        conn.connect(str(sock_path))
        msg = _json.dumps({
            "action":  "navigate",
            "film":    film,
            "field":   field,
            "label":   label,
            "shot_id": shot_id,
        })
        conn.sendall(msg.encode())
        conn.close()
        return True
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        return False


# ---------------------------------------------------------------------------

class _DeleteWorker(QThread):
    """Delete an engraving directory in a background thread, then signal done."""
    finished = pyqtSignal()

    def __init__(self, dir_path: Path, parent=None) -> None:
        super().__init__(parent)
        self._dir_path = dir_path

    def run(self) -> None:
        import shutil
        try:
            shutil.rmtree(str(self._dir_path))
        except Exception:
            pass
        self.finished.emit()


class _EngravingWorker(QThread):
    """Run a CLI engraving command in a background thread (single item)."""
    finished = pyqtSignal(bool, str)   # success, error_message

    def __init__(self, cmd: list, parent=None) -> None:
        super().__init__(parent)
        self._cmd = cmd

    def run(self) -> None:
        import subprocess
        try:
            result = subprocess.run(
                self._cmd,
                capture_output=True,
                text=True,
                timeout=600,
            )
            ok  = result.returncode == 0
            err = result.stderr.strip() if not ok else ""
            if not ok:
                import sys as _sys
                print(f"[EngravingWorker] command failed (rc={result.returncode}):",
                      file=_sys.stderr)
                if err:
                    print(err, file=_sys.stderr)
            self.finished.emit(ok, err)
        except subprocess.TimeoutExpired:
            self.finished.emit(False, "Timed out after 10 min")
        except Exception as exc:
            self.finished.emit(False, str(exc))


class _BatchEngravingWorker(QThread):
    """Run ``engraving batch`` in a background thread with line-by-line stdout.

    Supports cooperative cancellation via ``cancel()`` which terminates the
    subprocess.  Emits ``finished(success, error_message)`` when done.
    """
    finished = pyqtSignal(bool, str)

    def __init__(self, cmd: list, parent=None) -> None:
        super().__init__(parent)
        self._cmd       = cmd
        self._process   = None
        self._cancelled = False

    def cancel(self) -> None:
        """Request cancellation — terminates the subprocess if running."""
        self._cancelled = True
        if self._process is not None:
            try:
                self._process.terminate()
            except Exception:
                pass

    def run(self) -> None:
        import subprocess, sys as _sys
        try:
            self._process = subprocess.Popen(
                self._cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            # Drain stdout so the pipe never blocks.
            for line in self._process.stdout:
                if self._cancelled:
                    break
            self._process.wait()
            if self._cancelled:
                self.finished.emit(False, "Cancelled")
            else:
                ok  = self._process.returncode == 0
                err = self._process.stderr.read().strip() if not ok else ""
                if not ok:
                    print(f"[BatchEngravingWorker] rc={self._process.returncode}",
                          file=_sys.stderr)
                    if err:
                        print(err, file=_sys.stderr)
                self.finished.emit(ok, err)
        except Exception as exc:
            self.finished.emit(False, str(exc))


class _HoverIconButton(QPushButton):
    """QPushButton that swaps its icon to an accent-coloured version on hover."""

    def __init__(self, text: str = "", normal_icon=None, hover_icon=None, parent=None):
        super().__init__(text, parent)
        self._normal_icon = normal_icon
        self._hover_icon  = hover_icon
        self.setFocusPolicy(Qt.NoFocus)
        if normal_icon:
            self.setIcon(normal_icon)

    def enterEvent(self, event):
        if self._hover_icon and self.isEnabled():
            self.setIcon(self._hover_icon)
        super().enterEvent(event)

    def leaveEvent(self, event):
        # Keep hover icon while checked (running state); restore normal otherwise.
        if self._normal_icon and not self.isChecked():
            self.setIcon(self._normal_icon)
        super().leaveEvent(event)

    def setChecked(self, checked: bool) -> None:
        super().setChecked(checked)
        # Lock to hover icon while checked so the icon matches the ACCENT bg.
        if checked and self._hover_icon:
            self.setIcon(self._hover_icon)
        elif not checked and self._normal_icon:
            self.setIcon(self._normal_icon)


class _WrapLabel(QLabel):
    """QLabel that self-corrects its minimum height on every resize.

    Standard word-wrap QLabels inside QGridLayout / QScrollArea often report
    a stale sizeHint because heightForWidth() is not reliably called by the
    layout engine once the widget is already sized.  This subclass forces a
    recalculation in resizeEvent so the grid row is always tall enough.
    """

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.wordWrap():
            needed = self.heightForWidth(event.size().width())
            if needed > 0:
                self.setMinimumHeight(needed)


class IllustrationPane(QWidget):
    """Reference framework composition for silhouette/engraving browsing.

    Layout contract:
    - Left pane: browser surface (collection browsing + selection).
    - Right pane: inspector composition (sections reacting to selection).

    The pane wires browser signals to inspector and operation controls. It does
    not reimplement source scanning or service business logic.
    """

    def __init__(self, project_path: str, media_type: Optional[str] = None, parent=None) -> None:
        super().__init__(parent)
        self._project_path = project_path
        self._current_rec: Optional[dict] = None

        # Restore last-used media type; fall back to "movie" on first launch.
        if media_type is None:
            try:
                from tool import prefs as _prefs
                _saved = _prefs.get("ill_media_type")
                if _saved is None:
                    media_type = "movie"
                elif _saved == "":
                    media_type = None
                else:
                    media_type = _saved
            except Exception:
                media_type = "movie"

        # ── Sources ───────────────────────────────────────────────────────
        self._sil_source = SilhouetteSource(project_path)
        self._eng_source = EngravingSource(project_path)

        # ── Browsers (one per source; only the active one is visible) ─────
        _browser_kwargs = dict(thumb_size=_THUMB_SIZE, detach_controls=True)
        self._browser_sil = IllustrationBrowser(
            source=self._sil_source, media_type=media_type, **_browser_kwargs
        )
        self._browser_eng = IllustrationBrowser(
            source=self._eng_source, media_type=media_type, light_bg=True, **_browser_kwargs
        )
        # Active browser alias — updated on tab switch.
        self._browser = self._browser_sil
        self._browser_sil.selectionChanged.connect(self._on_selection_changed)
        self._browser_sil.catalogReloaded.connect(self._update_eng_buttons)
        self._browser_eng.selectionChanged.connect(self._on_selection_changed)

        # Persist media-type selection across restarts (shared for both modes).
        self._browser_sil._media_combo.currentIndexChanged.connect(
            self._on_media_type_pref_changed
        )
        self._browser_eng._media_combo.currentIndexChanged.connect(
            self._on_media_type_pref_changed
        )

        # Side-panel toggle state (Tab key)
        self._panels_hidden: bool = False
        self._saved_panel_sizes: list = []

        self._build_ui()
        QTimer.singleShot(0, self._fit_panel_width)

    # ------------------------------------------------------------------
    # UI construction

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._panel_splitter = GripSplitter(Qt.Horizontal)

        # ── Pane 0: Browser (sacred content surface) ──────────────────────
        self._browser_stack = QStackedWidget()
        self._browser_stack.addWidget(self._browser_sil)   # index 0
        self._browser_stack.addWidget(self._browser_eng)   # index 1
        self._panel_splitter.addWidget(self._browser_stack)

        # Double-click dispatches to the source-appropriate primary action.
        self._browser_sil.itemActivated.connect(self._open_in_shotlist)
        self._browser_eng.itemActivated.connect(self._open_engraving_in_viewer)

        # ── Pane 1: Filter — cascade combos + pagination + sort ───────────

        self._panel_splitter.addWidget(self._build_inspector_pane())

        self._panel_splitter.setStretchFactor(0, 1)
        self._panel_splitter.setStretchFactor(1, 0)
        self._panel_splitter.setCollapsible(0, False)


        root.addWidget(self._panel_splitter)

    # --

    def _build_inspector_pane(self) -> QTabWidget:
        """Right pane: two source tabs (Silhouettes / Engravings), each with
        Filter / Sort / Info / Tools collapsible sections."""
        # Styled tab widget (no content tabs yet).
        tabs = self._make_tab_widget()
        self._side_scroll = tabs

        # ── Silhouettes tab ───────────────────────────────────────────────
        sil_scroll, self._sil_sort_combo, self._sil_meta_rows = self._build_source_panel(
            self._browser_sil, "ill_sil", _SIL_INFO_KEYS, has_sort=True,  has_tools=True
        )
        tabs.addTab(sil_scroll, " Silhouettes ")

        # ── Engravings tab ────────────────────────────────────────────────
        eng_scroll, self._eng_sort_combo, self._eng_meta_rows = self._build_source_panel(
            self._browser_eng, "ill_eng", _ENG_INFO_KEYS,
            has_sort=False, has_mode_filter=True, has_eng_tools=True
        )
        tabs.addTab(eng_scroll, " Engravings ")

        # Active-panel aliases (Silhouettes is default).
        self._sort_combo = self._sil_sort_combo
        self._meta_rows  = self._sil_meta_rows

        # Track scrollbar visibility to keep pane width stable.
        self._inspector_sb_visible = False
        self._inspector_scroll = sil_scroll
        sil_scroll.verticalScrollBar().rangeChanged.connect(self._on_inspector_sb_range)

        tabs.currentChanged.connect(self._on_source_tab_changed)
        return tabs

    # ------------------------------------------------------------------ helpers

    def _make_tab_widget(self) -> QTabWidget:
        """Return a styled QTabWidget for the inspector pane."""
        _TAB_ACTIVE = theme.TAB_BG
        tabs = QTabWidget()
        tabs.setMinimumWidth(_SIDE_PANE_W)
        tabs.setDocumentMode(True)
        tabs.tabBar().setDrawBase(False)
        tabs.tabBar().setExpanding(False)
        tabs.tabBar().setUsesScrollButtons(False)
        tabs.setFocusPolicy(Qt.NoFocus)
        tabs.tabBar().setFocusPolicy(Qt.NoFocus)
        tabs.setStyleSheet(
            f"QTabWidget           {{ background: {theme.CANVAS_BG}; border: none; }}"
            f"QTabWidget::pane     {{ border: none; background: {_TAB_ACTIVE}; }}"
            f"QTabBar              {{ background: {theme.CANVAS_BG}; border: none; }}"
            f"QTabBar::tab {{"
            f"  background: {theme.CANVAS_BG}; color: {theme.TEXT_DIM};"
            f"  padding: 2px 16px; border: none; margin-bottom: 0;"
            f"  font-family: '{theme.FAMILY_UI}'; font-size: {theme.BASE_PT}pt;"
            f"  font-weight: {theme.WEIGHT_UI};"
            f"  min-height: 20px;"
            f"  min-width: 0px;"
            f"}}"
            f"QTabBar::tab:selected {{ background: {_TAB_ACTIVE}; color: {theme.TEXT}; border: none; }}"
            f"QTabBar::tab:focus {{ outline: none; }}"
            f"QTabBar::tab:hover    {{ background: {_TAB_ACTIVE}; color: {theme.TEXT}; }}"
        )
        return tabs

    def _make_panel_scroll(self) -> tuple:
        """Return (QScrollArea, QWidget panel, QVBoxLayout) for a source panel."""
        _TAB_ACTIVE = theme.TAB_BG
        _content_style = (
            f"QWidget {{ background: {_TAB_ACTIVE}; }}"
            f" QComboBox {{ background-color: {theme.BTN_BG}; color: {theme.TEXT}; }}"
            f" QComboBox::drop-down {{ border: none; }}"
            f" QComboBox QAbstractItemView, QComboBox QListView {{"
            f"   background: {theme.INPUT_BG}; color: {theme.TEXT};"
            f"   border: 0px; margin: 0px; padding: 0px; outline: 0px;"
            f"   selection-background-color: {theme.ACCENT};"
            f"   selection-color: {theme.ACCENT_TEXT}; }}"
            f" QComboBox QAbstractItemView::item, QComboBox QListView::item {{"
            f"   padding: 0px 8px; min-height: 24px; border: 0px; }}"
            f" QPushButton {{ background-color: {theme.BTN_BG}; border: none;"
            f" padding: 0 10px; border-radius: 3px;"
            f" min-height: {theme.BTN_H}px; max-height: {theme.BTN_H}px; }}"
            f" QPushButton:hover    {{ background-color: {theme.BTN_HOVER}; }}"
            f" QPushButton:pressed  {{ background-color: {theme.BTN_PRESSED}; }}"
            f" QPushButton:checked  {{ background-color: {theme.ACCENT}; color: {theme.ACCENT_TEXT}; }}"
            f" QPushButton:disabled {{ color: {theme.TEXT_DIM};"
            f" background-color: {theme.BTN_BG}; }}"
        )
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFocusPolicy(Qt.NoFocus)
        scroll.viewport().setFocusPolicy(Qt.NoFocus)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet(f"QScrollArea {{ background: {_TAB_ACTIVE}; border: none; }}")
        panel = QWidget()
        panel.setStyleSheet(_content_style)
        panel.setMinimumWidth(_SIDE_PANE_W)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)
        scroll.setWidget(panel)
        return scroll, panel, layout

    def _make_sort_combo(self, pv: QVBoxLayout, pref_key: str) -> QComboBox:
        """Build, wire, and add a Sort section+combo to *pv*; return the combo."""
        sort_sec = CollapsibleSection("Sort", pref_key=pref_key)
        combo = QComboBox()
        combo.setFocusPolicy(Qt.NoFocus)
        combo.setMaxVisibleItems(10)
        combo.setSizeAdjustPolicy(QComboBox.AdjustToContentsOnFirstShow)
        # Custom QListView for popup styling.
        _sv = QListView(combo)
        _sv.setUniformItemSizes(True)
        _sv.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        _sv.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        _sv.setFrameShape(QFrame.NoFrame)
        _sv.setLineWidth(0)
        _sv.setMidLineWidth(0)
        _sv.setContentsMargins(0, 0, 0, 0)
        _sv.setStyleSheet(
            f"QListView {{ background: {theme.INPUT_BG}; color: {theme.TEXT};"
            f" border: 0px; margin: 0px; padding: 0px; outline: 0px; }}"
            f"QListView::item {{ background: {theme.INPUT_BG}; padding: 0px 8px;"
            f" min-height: 24px; border: 0px; }}"
            f"QListView::item:selected {{ background: {theme.ACCENT}; color: {theme.ACCENT_TEXT}; }}"
        )
        combo.setView(_sv)
        _sv.setViewportMargins(0, 0, 0, 0)
        _sc = _sv.parentWidget()
        if _sc is not None:
            _sc.setFrameStyle(QFrame.NoFrame)
            _sc.setLineWidth(0)
            _sc.setMidLineWidth(0)
            _sc.setStyleSheet(f"QFrame {{ background: {theme.INPUT_BG}; border: 0px; margin: 0px; padding: 0px; }}")
            if _sc.layout():
                _sc.layout().setContentsMargins(0, 0, 0, 0)
                _sc.layout().setSpacing(0)
        combo.addItem("-----", userData=None)
        for disp, key in _SORT_OPTS:
            combo.addItem(disp, userData=key)

        def _refresh_color(_idx: int = 0, _c=combo) -> None:
            _col = theme.TEXT_DIM if _c.currentData() is None else theme.TEXT
            _c.setStyleSheet(
                f"QComboBox {{ background: {theme.BTN_BG}; color: {_col};"
                f" border: none; border-radius: 3px; padding: 0px 6px;"
                f" min-height: 24px; max-height: 24px; }}"
                f"QComboBox::drop-down {{ border: none; }}"
            )
        combo.currentIndexChanged.connect(_refresh_color)
        combo.currentIndexChanged.connect(self._on_sort_changed)
        _refresh_color()
        sort_sec.add_widget(combo)
        pv.addWidget(sort_sec)
        return combo

    def _make_info_grid(self, pv: QVBoxLayout, pref_key: str,
                        info_keys: tuple) -> dict:
        """Build and add an Info collapsible section; return {key: QLabel} dict."""
        info_sec = CollapsibleSection("Info", pref_key=pref_key)
        block = MetadataBlock(list(info_keys))
        info_sec.add_widget(block)
        pv.addWidget(info_sec)
        return block.labels()

    def _build_source_panel(
        self,
        browser: "IllustrationBrowser",
        pref_prefix: str,
        info_keys: tuple,
        has_sort: bool = True,
        has_mode_filter: bool = False,
        has_tools: bool = False,
        has_eng_tools: bool = False,
    ) -> tuple:
        """Build a complete Filter/[Sort]/[Mode]/Info/[Tools] panel for *browser*.

        Returns (QScrollArea, sort_combo, meta_rows).  sort_combo is None when
        has_sort is False.
        """
        scroll, panel, pv = self._make_panel_scroll()

        # ── Filter ────────────────────────────────────────────────────────
        filter_sec = CollapsibleSection("Filter",
                                        pref_key=f"{pref_prefix}_section_filter")
        _fb = QWidget()
        _fb_lay = QVBoxLayout(_fb)
        _fb_lay.setContentsMargins(0, 0, 0, 0)
        _fb_lay.setSpacing(0)
        _fb_lay.addWidget(browser.filter_panel)
        _fb_lay.addWidget(browser.status_bar)
        _fb_lay.addWidget(browser.pagination_panel)
        filter_sec.add_widget(_fb)
        browser.keywordChanged.connect(
            lambda kw, sec=filter_sec: sec.set_subtitle(kw.capitalize() if kw else "")
        )
        pv.addWidget(filter_sec)
        filter_sec.set_subbar(browser._loading_bar)

        # ── Sort (optional) ───────────────────────────────────────────────
        sort_combo = None
        if has_sort:
            sort_combo = self._make_sort_combo(pv, f"{pref_prefix}_section_sort")

        # ── Mode filter (engravings) ──────────────────────────────────────
        if has_mode_filter:
            self._build_mode_filter_section(pv, browser)

        # ── Info ──────────────────────────────────────────────────────────
        meta_rows = self._make_info_grid(pv, f"{pref_prefix}_section_info", info_keys)

        # ── Tools ─────────────────────────────────────────────────────────
        if has_tools:
            self._build_tools_section(pv)
        if has_eng_tools:
            self._build_engraving_tools_section(pv)

        pv.addStretch()
        return scroll, sort_combo, meta_rows

    # ------------------------------------------------------------------ shared button helpers

    def _btn_style(self) -> str:
        """Single canonical stylesheet for all action buttons in this visualizer."""
        return (
            f"QPushButton {{"
            f"  background-color: {theme.BTN_BG}; color: {theme.TEXT};"
            f"  border: none; border-radius: 3px; padding: 0 8px;"
            f"  min-height: {theme.BTN_H}px; max-height: {theme.BTN_H}px;"
            f"}}"
            f"QPushButton:hover   {{ background-color: {theme.ACCENT}; color: {theme.ACCENT_TEXT}; }}"
            f"QPushButton:pressed {{ background-color: {theme.BTN_PRESSED}; }}"
            f"QPushButton:checked {{ background-color: {theme.ACCENT}; color: {theme.ACCENT_TEXT}; }}"
            f"QPushButton[accent_text='true']:checked"
            f" {{ background-color: {theme.BTN_BG}; color: {theme.ACCENT}; }}"
            f"QPushButton:disabled {{ background-color: {theme.BTN_BG};"
            f" color: rgba(255,255,255,0.15); }}"
            f"QPushButton:focus {{ outline: none; }}"
        )

    def _make_btn_icon(self, svg_name: str, size: int = 14) -> tuple:
        """Return (normal_icon, hover_icon) for *svg_name*.

        normal_icon  — TEXT colour when enabled; dim grey when disabled.
        hover_icon   — ACCENT_TEXT colour when enabled; dim grey when disabled.
        Both icons have a proper QIcon.Disabled pixmap so Qt renders the right
        colour automatically when the button is disabled, with no extra code.
        """
        _dim = _svg_icon(svg_name, size, "#7f7f7f").pixmap(size, size)
        normal = QIcon()
        normal.addPixmap(_svg_icon(svg_name, size, theme.TEXT).pixmap(size, size))
        normal.addPixmap(_dim, QIcon.Disabled)
        hover = QIcon()
        hover.addPixmap(_svg_icon(svg_name, size, theme.ACCENT_TEXT).pixmap(size, size))
        hover.addPixmap(_dim, QIcon.Disabled)
        return normal, hover

    def _build_tools_section(self, pv: QVBoxLayout) -> None:
        """Add the Tools collapsible section to *pv*."""
        tools_sec = CollapsibleSection("Tools", pref_key="ill_section_tools")
        _icon_sz = QSize(14, 14)
        _pix_normal   = _svg_icon("open-in-window", 14, theme.TEXT).pixmap(14, 14)
        _pix_hover    = _svg_icon("open-in-window", 14, theme.ACCENT_TEXT).pixmap(14, 14)
        _pix_disabled = _svg_icon("open-in-window", 14, "#7f7f7f").pixmap(14, 14)
        _open_icon = QIcon()
        _open_icon.addPixmap(_pix_normal)
        _open_icon.addPixmap(_pix_disabled, QIcon.Disabled)
        _open_icon_hover = QIcon()
        _open_icon_hover.addPixmap(_pix_hover)
        _open_icon_hover.addPixmap(_pix_disabled, QIcon.Disabled)

        _abtn = (
            f"QPushButton {{"
            f"  background-color: {theme.BTN_BG}; color: {theme.TEXT};"
            f"  border: none; border-radius: 3px; padding: 0 8px;"
            f"  min-height: {theme.BTN_H}px; max-height: {theme.BTN_H}px;"
            f"}}"
            f"QPushButton:hover   {{ background-color: {theme.ACCENT}; color: {theme.ACCENT_TEXT}; }}"
            f"QPushButton:pressed {{ background-color: {theme.BTN_PRESSED}; }}"
            f"QPushButton:checked {{ background-color: {theme.ACCENT}; color: {theme.ACCENT_TEXT}; }}"
            f"QPushButton[accent_text='true']:checked"
            f" {{ background-color: {theme.BTN_BG}; color: {theme.ACCENT}; }}"
            f"QPushButton:disabled {{ background-color: {theme.BTN_BG};"
            f" color: rgba(255,255,255,0.15); }}"
            f"QPushButton:focus {{ outline: none; }}"
        )
        _row = QWidget()
        _rl  = QHBoxLayout(_row)
        _rl.setContentsMargins(0, 0, 0, 0)
        _rl.setSpacing(2)

        self._best_btn = QPushButton("Best")
        self._best_btn.setCheckable(True)
        self._best_btn.setFocusPolicy(Qt.NoFocus)
        self._best_btn.setEnabled(False)
        self._best_btn.setFixedHeight(theme.BTN_H)
        self._best_btn.setStyleSheet(_abtn)
        self._best_btn.clicked.connect(self._on_best_btn_clicked)
        tools_sec.add_widget(self._best_btn)

        self._shotlist_btn = _HoverIconButton("Shotlist", _open_icon, _open_icon_hover)
        self._shotlist_btn.setIconSize(_icon_sz)
        self._shotlist_btn.setFocusPolicy(Qt.NoFocus)
        self._shotlist_btn.setEnabled(False)
        self._shotlist_btn.setStyleSheet(_abtn)
        self._shotlist_btn.clicked.connect(self._open_in_shotlist)
        _rl.addWidget(self._shotlist_btn, 1)

        self._sam_btn = _HoverIconButton("Segmentation", _open_icon, _open_icon_hover)
        self._sam_btn.setIconSize(_icon_sz)
        self._sam_btn.setFocusPolicy(Qt.NoFocus)
        self._sam_btn.setEnabled(False)
        self._sam_btn.setStyleSheet(_abtn)
        self._sam_btn.clicked.connect(self._open_sam_explorer)
        _rl.addWidget(self._sam_btn, 1)
        tools_sec.add_widget(_row)

        # ── Engraving generation row: [Engrave] [Engraving] ─────────────
        _e_icon, _e_icon_hover  = self._make_btn_icon("media-image-plus", 14)
        _v_icon, _v_icon_hover  = self._make_btn_icon("media-image",      14)
        _b_icon, _b_icon_hover  = self._make_btn_icon("media-image-plus", 14)
        _eng_row = QWidget()
        _eng_rl  = QHBoxLayout(_eng_row)
        _eng_rl.setContentsMargins(0, 0, 0, 0)
        _eng_rl.setSpacing(2)
        self._eng_gen_btn = _HoverIconButton("Engrave", _e_icon, _e_icon_hover)
        self._eng_gen_btn.setIconSize(_icon_sz)
        self._eng_gen_btn.setFocusPolicy(Qt.NoFocus)
        self._eng_gen_btn.setCheckable(True)   # stays highlighted while running
        self._eng_gen_btn.setEnabled(False)
        self._eng_gen_btn.setStyleSheet(_abtn)
        self._eng_gen_btn.clicked.connect(self._start_engraving_generation)
        _eng_rl.addWidget(self._eng_gen_btn, 1)
        self._eng_viz_btn = _HoverIconButton("Engraving", _v_icon, _v_icon_hover)
        self._eng_viz_btn.setIconSize(_icon_sz)
        self._eng_viz_btn.setFocusPolicy(Qt.NoFocus)
        self._eng_viz_btn.setEnabled(False)
        self._eng_viz_btn.setStyleSheet(_abtn)
        self._eng_viz_btn.clicked.connect(self._visualize_engraving)
        _eng_rl.addWidget(self._eng_viz_btn, 1)
        tools_sec.add_widget(_eng_row)
        self._eng_batch_btn = _HoverIconButton("Generate All Marked", _b_icon, _b_icon_hover)
        self._eng_batch_btn.setIconSize(_icon_sz)
        self._eng_batch_btn.setFocusPolicy(Qt.NoFocus)
        self._eng_batch_btn.setCheckable(True)   # stays highlighted while running
        self._eng_batch_btn.setEnabled(False)
        self._eng_batch_btn.setStyleSheet(_abtn)
        self._eng_batch_btn.clicked.connect(self._toggle_batch_generation)
        tools_sec.add_widget(self._eng_batch_btn)

        # Animation timer for "Generating…" dots
        self._eng_anim_timer = QTimer(self)
        self._eng_anim_timer.setInterval(400)
        self._eng_anim_timer.timeout.connect(self._tick_eng_animation)
        self._eng_anim_dots  = 0
        # Count-refresh timer: re-checks remaining pending items every 2 s
        # while the batch is running, giving a live countdown on the button.
        self._eng_count_timer = QTimer(self)
        self._eng_count_timer.setInterval(2000)
        self._eng_count_timer.timeout.connect(self._refresh_batch_count)
        self._eng_gen_running   = False
        self._eng_batch_running = False
        tools_sec._header.setToolTip(
            "Home / End \u2014 film\n"
            "PgUp / PgDn \u2014 field\n"
            "\u2190 \u2191 \u2192 \u2193 \u2014 grid\n"
            "a\u2013z / # \u2014 letter\n"
            "Shift+\u2191\u2193 \u2014 keyword\n"
            "Shift+\u2190\u2192 \u2014 page\n"
            "Enter \u2014 toggle best\n"
            "Shift+Enter \u2014 shotlist"
        )
        pv.addWidget(tools_sec)

    # ------------------------------------------------------------------ source switching

    def _on_source_tab_changed(self, idx: int) -> None:
        """Switch the active browser and inspector state when the tab changes."""
        if idx == 0:
            self._browser    = self._browser_sil
            self._meta_rows  = self._sil_meta_rows
            self._sort_combo = self._sil_sort_combo
        else:
            self._browser    = self._browser_eng
            self._meta_rows  = self._eng_meta_rows
            self._sort_combo = self._eng_sort_combo
        self._browser_stack.setCurrentIndex(idx)
        rec = self._browser.currentItem()
        if rec:
            self._on_selection_changed(rec)
        else:
            self._clear_meta()

    @property
    def _active_best_btn(self):
        """The Best button for the currently active source tab."""
        if self._browser is self._browser_eng:
            return getattr(self, "_eng_best_btn", None)
        return getattr(self, "_best_btn", None)

    def _on_splitter_moved(self, pos: int, index: int) -> None:
        pass  # two-pane — no locking needed

    def _on_media_type_pref_changed(self, _idx: int) -> None:
        """Persist the selected media type so it is restored on next launch."""
        val = self._browser._media_combo.currentData()
        try:
            from tool import prefs as _prefs
            _prefs.set("ill_media_type", val if val is not None else "")
        except Exception:
            pass

    def _on_inspector_sb_range(self, _min: int, max_val: int) -> None:
        """Widen the inspector splitter pane when the scrollbar appears, shrink when it hides."""
        from PyQt5.QtWidgets import QStyle
        sb_w = self._inspector_scroll.style().pixelMetric(QStyle.PM_ScrollBarExtent)
        sizes = self._panel_splitter.sizes()
        if max_val > 0 and not self._inspector_sb_visible:
            self._inspector_sb_visible = True
            sizes[-1] += sb_w
            self._panel_splitter.setSizes(sizes)
        elif max_val == 0 and self._inspector_sb_visible:
            self._inspector_sb_visible = False
            sizes[-1] = max(0, sizes[-1] - sb_w)
            self._panel_splitter.setSizes(sizes)

    def _build_mode_filter_section(
        self, pv: QVBoxLayout, browser: "IllustrationBrowser"
    ) -> None:
        """Add a Mode collapsible section with Isolated+Frame / Frame / Isolated combo."""
        mode_sec = CollapsibleSection("Mode", pref_key="ill_eng_section_mode")
        combo = QComboBox()
        combo.setFocusPolicy(Qt.NoFocus)
        combo.setSizeAdjustPolicy(QComboBox.AdjustToContentsOnFirstShow)
        combo.addItem("Isolated + Frame", userData="")          # no filter
        combo.addItem("Frame",            userData="frame")
        combo.addItem("Isolated",         userData="isolated")
        combo.setStyleSheet(
            f"QComboBox {{ background: {theme.BTN_BG}; color: {theme.TEXT};"
            f" border: none; border-radius: 3px; padding: 0px 6px;"
            f" font-family: '{theme.FAMILY_UI}'; font-size: {theme.BASE_PT}pt;"
            f" font-weight: {theme.WEIGHT_UI};"
            f" min-height: 24px; max-height: 24px; }}"
            f"QComboBox::drop-down {{ border: none; }}"
        )

        def _on_mode_changed(_idx: int) -> None:
            mode = combo.currentData()  # "" = Both (no filter), "frame", "isolated"
            self._eng_source.set_mode_filter(mode)
            browser.refresh()

        combo.currentIndexChanged.connect(_on_mode_changed)
        mode_sec.add_widget(combo)
        pv.addWidget(mode_sec)

    def _build_engraving_tools_section(self, pv: QVBoxLayout) -> None:
        """Add the Engravings Tools section (Viewer + Best buttons)."""
        tools_sec = CollapsibleSection("Tools", pref_key="ill_eng_section_tools")
        _icon_sz = QSize(14, 14)
        _abtn = self._btn_style()
        _v_icon, _v_icon_hover = self._make_btn_icon("media-image", 14)
        _s_icon, _s_icon_hover = self._make_btn_icon("media-image", 14)
        _row = QWidget()
        _rl  = QHBoxLayout(_row)
        _rl.setContentsMargins(0, 0, 0, 0)
        _rl.setSpacing(2)

        self._eng_best_btn = QPushButton("Best")
        self._eng_best_btn.setCheckable(True)
        self._eng_best_btn.setFocusPolicy(Qt.NoFocus)
        self._eng_best_btn.setEnabled(False)
        self._eng_best_btn.setFixedHeight(theme.BTN_H)
        self._eng_best_btn.setStyleSheet(_abtn + (
            f"QPushButton:checked {{ background-color: {theme.ACCENT}; color: {theme.ACCENT_TEXT}; }}"
        ))
        self._eng_best_btn.clicked.connect(self._on_best_btn_clicked)
        tools_sec.add_widget(self._eng_best_btn)

        self._eng_view_btn = _HoverIconButton("Viewer", _v_icon, _v_icon_hover)
        self._eng_view_btn.setIconSize(_icon_sz)
        self._eng_view_btn.setFocusPolicy(Qt.NoFocus)
        self._eng_view_btn.setEnabled(False)
        self._eng_view_btn.setStyleSheet(_abtn)
        self._eng_view_btn.clicked.connect(self._open_engraving_in_viewer)
        _rl.addWidget(self._eng_view_btn, 1)
        self._eng_sil_btn = _HoverIconButton("Silhouette", _s_icon, _s_icon_hover)
        self._eng_sil_btn.setIconSize(_icon_sz)
        self._eng_sil_btn.setFocusPolicy(Qt.NoFocus)
        self._eng_sil_btn.setEnabled(False)
        self._eng_sil_btn.setStyleSheet(_abtn)
        self._eng_sil_btn.clicked.connect(self._jump_to_silhouette)
        _rl.addWidget(self._eng_sil_btn, 1)
        tools_sec.add_widget(_row)

        # Delete button — removes the engraving directory; Del/Backspace also fires it
        _del_icon, _del_icon_hover = self._make_btn_icon("trash", 14)
        self._eng_delete_btn = _HoverIconButton("Delete", _del_icon, _del_icon_hover)
        self._eng_delete_btn.setIconSize(_icon_sz)
        self._eng_delete_btn.setFocusPolicy(Qt.NoFocus)
        self._eng_delete_btn.setEnabled(False)
        self._eng_delete_btn.setStyleSheet(_abtn)
        self._eng_delete_btn.clicked.connect(self._delete_engraving)
        tools_sec.add_widget(self._eng_delete_btn)
        pv.addWidget(tools_sec)

    # ------------------------------------------------------------------
    # Sort controls

    def _on_sort_changed(self, _idx: int) -> None:
        """Update the source sort order and refresh the active browser."""
        # Only respond to the active tab's combo to avoid double-firing.
        if self.sender() is not self._sort_combo:
            return
        key  = self._sort_combo.currentData()
        keys = [key] if key else []
        source = self._browser._source
        if hasattr(source, "set_sort_keys"):
            source.set_sort_keys(keys)
        self._browser.refresh()

    # ------------------------------------------------------------------
    # Selection

    def _on_selection_changed(self, rec: dict) -> None:
        """Update the inspector panel when the browser selection changes."""
        self._show_object_meta(rec)
        self._update_best_btn()

    # ------------------------------------------------------------------
    # Object inspector

    def _clear_meta(self) -> None:
        for lbl in self._meta_rows.values():
            lbl.setText("—")
        self._current_rec = None
        if hasattr(self, "_eng_view_btn"):
            self._eng_view_btn.setEnabled(False)
        if hasattr(self, "_eng_sil_btn"):
            self._eng_sil_btn.setEnabled(False)
        if hasattr(self, "_eng_delete_btn"):
            self._eng_delete_btn.setEnabled(False)
            self._eng_delete_btn.setText("Delete")
        btn = self._active_best_btn
        if btn is not None:
            btn.setEnabled(False)
            btn.blockSignals(True)
            btn.setChecked(False)
            btn.blockSignals(False)
        if hasattr(self, "_shotlist_btn"):
            self._shotlist_btn.setEnabled(False)
            self._sam_btn.setEnabled(False)
        if hasattr(self, "_eng_viz_btn"):
            self._eng_viz_btn.setEnabled(False)
        self._update_eng_buttons()

    def _activate_primary_action(self) -> None:
        """Shift+Enter / double-click primary action for the active source."""
        if self._browser is self._browser_eng:
            self._open_engraving_in_viewer()
        else:
            self._open_in_shotlist()

    def _show_object_meta(self, rec: dict) -> None:
        def _set(key: str, value: str) -> None:
            lbl = self._meta_rows.get(key)
            if lbl is not None:
                lbl.setText(value)

        def _stored(key):
            v = rec.get(f"{key}_score")
            if v is None:
                v = rec.get(key)
            try:
                return float(v) if v is not None else None
            except Exception:
                return None

        def _fmt(v):
            return f"{v:.3f}" if v is not None else "—"

        film = rec.get("filename_stem") or rec.get("filename") or "—"
        shot_id = str(rec.get("shot_id", "—"))
        if len(shot_id) > 28:
            shot_id = "…" + shot_id[-26:]

        # Keys shared by all sources
        _set("label", rec.get("label", "—"))
        _set("film",  film)
        _set("model", rec.get("sam_model") or rec.get("model") or "—")

        # Silhouette-only keys
        _set("shot",  shot_id)
        _set("frame", str(rec.get("frame", "—")))
        _set("confidence", _fmt(_stored("confidence")))

        # size: derive from mask_area + frame_size when not yet scored
        size_val = _stored("size")
        if size_val is None:
            mask_area  = rec.get("mask_area")
            frame_size = rec.get("frame_size") or []
            if mask_area is not None and len(frame_size) >= 2:
                frame_area = float(max(1, frame_size[0] * frame_size[1]))
                area_frac  = float(mask_area) / frame_area
                size_val   = max(0.0, min(1.0, (area_frac - 0.002) / max(1e-9, 0.298)))

        # fullness: derive from mask_area + bbox when not yet scored
        fullness_val = _stored("fullness")
        if fullness_val is None:
            mask_area = rec.get("mask_area")
            bbox = rec.get("bbox") or []
            if mask_area is not None and len(bbox) >= 4:
                bbox_area    = float(max(1, bbox[2] * bbox[3]))
                fullness_val = max(0.0, min(1.0, float(mask_area) / bbox_area))

        _set("usefulness",     _fmt(_stored("usefulness")))
        _set("fullness",       _fmt(fullness_val))
        _set("size",           _fmt(size_val))
        _set("overlap",        _fmt(_stored("overlap")))
        _set("semantic_label", _fmt(_stored("semantic_label")))
        _set("semantic_field", _fmt(_stored("semantic_field")))

        # Engraving-only keys
        _set("mode",      rec.get("mode", "—"))
        _set("object_id", rec.get("object_id", "—"))

        # Engraving tools buttons
        if hasattr(self, "_eng_view_btn"):
            has_png = bool(rec.get("output_png") or rec.get("raw_png"))
            self._eng_view_btn.setEnabled(has_png)
        if hasattr(self, "_eng_sil_btn"):
            self._eng_sil_btn.setEnabled(
                bool(rec.get("label") or rec.get("filename_stem"))
            )
        if hasattr(self, "_eng_delete_btn"):
            has_dir = bool(rec.get("path") and Path(str(rec.get("path"))).parent.is_dir())
            self._eng_delete_btn.setEnabled(has_dir)
            self._eng_delete_btn.setText("Delete")

        self._current_rec = rec
        _can_open = bool(rec.get("filename") and rec.get("shot_id"))
        if hasattr(self, "_shotlist_btn"):
            self._shotlist_btn.setEnabled(_can_open)
            self._sam_btn.setEnabled(_can_open)
        self._update_eng_buttons()

    # ------------------------------------------------------------------
    # Best-selection workflow

    def _update_best_btn(self) -> None:
        btn = self._active_best_btn
        if btn is None:
            return
        rec = self._browser.currentItem()
        if rec is None:
            btn.setEnabled(False)
            btn.blockSignals(True)
            btn.setChecked(False)
            btn.blockSignals(False)
            return
        btn.setEnabled(True)
        btn.blockSignals(True)
        btn.setChecked(bool(rec.get("human_best")))
        btn.blockSignals(False)

    def _on_best_btn_clicked(self, checked: bool) -> None:
        if checked:
            self._mark_best()
        else:
            self._unmark_best()

    def _toggle_best(self) -> None:
        rec = self._browser.currentItem()
        if rec is None:
            return
        if rec.get("human_best"):
            self._unmark_best()
        else:
            self._mark_best()

    def _mark_best(self) -> None:
        from services.silhouette_curation import mark_best
        rec = self._browser.currentItem()
        if rec is None:
            return
        # Collect all records for the same label (from the filtered list)
        label = rec.get("label", "")
        all_label_recs = [
            r for r in self._browser._filtered_items
            if r.get("label") == label
        ]
        mark_best(rec, all_label_recs)
        self._browser.refresh_highlights()
        self._update_best_btn()

    def _unmark_best(self) -> None:
        from services.silhouette_curation import unmark_best
        rec = self._browser.currentItem()
        if rec is None:
            return
        unmark_best(rec)
        self._browser.refresh_highlights()
        self._update_best_btn()

    # ------------------------------------------------------------------
    # Action buttons

    def _count_pending_engravings(self) -> int:
        """Count marked silhouettes whose engraving is not yet generated."""
        try:
            from services.engraving_paths import engraving_status as _eng_status
            return sum(
                1 for r in (self._sil_source.items() if hasattr(self, "_sil_source") else [])
                if r.get("human_best") and r.get("path") and
                _eng_status(self._project_path, r["path"], r) != "generated"
            )
        except Exception:
            return 0

    def _update_eng_buttons(self) -> None:
        """Enable/disable and label the Engraving and Marked generation buttons."""
        if not hasattr(self, "_eng_gen_btn"):
            return
        if self._eng_gen_running or self._eng_batch_running:
            return  # keep running state intact

        rec = self._current_rec
        if rec and rec.get("path"):
            status = None
            try:
                from services.engraving_paths import engraving_status as _eng_status
                status = _eng_status(self._project_path, rec["path"], rec)
                done = status in ("generated", "generating")
            except Exception:
                done = False
            self._eng_gen_btn.setEnabled(not done)
            self._eng_gen_btn.setChecked(False)
            self._eng_gen_btn.setText("Engrave")
            self._eng_viz_btn.setEnabled(status == "generated")
        else:
            self._eng_gen_btn.setEnabled(False)
            self._eng_gen_btn.setChecked(False)
            self._eng_gen_btn.setText("Engrave")
            self._eng_viz_btn.setEnabled(False)

        n = self._count_pending_engravings()
        self._eng_batch_btn.setEnabled(n > 0)
        self._eng_batch_btn.setChecked(False)
        self._eng_batch_btn.setText(f"Generate Marked ({n})" if n > 0 else "Generate Marked")

    def _refresh_batch_count(self) -> None:
        """Re-check pending count while batch is running and update button label."""
        if not self._eng_batch_running:
            self._eng_count_timer.stop()
            return
        n = self._count_pending_engravings()
        self._eng_batch_btn.setText(f"Cancel  ({n} left)")

    def _tick_eng_animation(self) -> None:
        """Animate the single-item Engraving button while it runs."""
        self._eng_anim_dots = (self._eng_anim_dots + 1) % 4
        dots = "." * max(1, self._eng_anim_dots)
        if self._eng_gen_running:
            self._eng_gen_btn.setText(f"Generating{dots}")
        if not self._eng_gen_running:
            self._eng_anim_timer.stop()

    def _start_engraving_generation(self) -> None:
        """Generate an engraving for the currently selected silhouette via OpenAI."""
        rec = self._current_rec
        if not rec or not rec.get("path"):
            return
        self._eng_gen_running = True
        self._eng_gen_btn.setChecked(True)   # ACCENT highlight while running
        self._eng_gen_btn.setEnabled(False)
        self._eng_batch_btn.setEnabled(False)
        self._eng_anim_dots = 0
        self._eng_anim_timer.start()
        cmd = [
            sys.executable,
            str(Path(__file__).parent.parent / "cli.py"),
            "engraving", "generate",
            "--source", str(rec["path"]),
            "--provider", "openai",
        ]
        self._eng_gen_worker = _EngravingWorker(cmd, parent=self)
        self._eng_gen_worker.finished.connect(self._on_eng_gen_finished)
        self._eng_gen_worker.start()

    def _on_eng_gen_finished(self, ok: bool, err: str) -> None:
        self._eng_gen_running = False
        self._eng_gen_worker  = None
        self._eng_gen_btn.setChecked(False)
        if not ok:
            self._eng_gen_btn.setText("Failed")
            if err:
                self._eng_gen_btn.setToolTip(err)
            QTimer.singleShot(4000, lambda: (
                self._eng_gen_btn.setText("Engrave"),
                self._eng_gen_btn.setToolTip(""),
            ))
        # Full reload so the new engraving.json is picked up from disk.
        self._browser_eng.reload()
        self._update_eng_buttons()

    def _toggle_batch_generation(self) -> None:
        """Start batch generation, or cancel it if already running."""
        if self._eng_batch_running:
            self._cancel_batch_generation()
        else:
            self._start_batch_generation()

    def _start_batch_generation(self) -> None:
        """Generate engravings for all marked silhouettes that need them."""
        n = self._count_pending_engravings()
        if n == 0:
            return
        self._eng_batch_running = True
        self._eng_gen_btn.setEnabled(False)
        self._eng_batch_btn.setEnabled(True)   # keep enabled so user can cancel
        self._eng_batch_btn.setChecked(True)   # ACCENT highlight
        self._eng_batch_btn.setText(f"Cancel  ({n} left)")
        self._eng_count_timer.start()
        media = self._browser_sil._media_type or "movie"
        cmd = [
            sys.executable,
            str(Path(__file__).parent.parent / "cli.py"),
            "engraving", "batch",
            "--media", media,
            "--verbose",
        ]
        self._eng_batch_worker = _BatchEngravingWorker(cmd, parent=self)
        self._eng_batch_worker.finished.connect(self._on_eng_batch_finished)
        self._eng_batch_worker.start()

    def _cancel_batch_generation(self) -> None:
        """Cancel a running batch generation."""
        if self._eng_batch_worker is not None:
            self._eng_batch_worker.cancel()
        # State reset happens in _on_eng_batch_finished(False, "Cancelled")

    def _on_eng_batch_finished(self, ok: bool, err: str) -> None:
        self._eng_batch_running = False
        self._eng_batch_worker  = None
        self._eng_count_timer.stop()
        self._eng_batch_btn.setChecked(False)
        if not ok and err and err != "Cancelled":
            self._eng_batch_btn.setText("Failed")
            if err:
                self._eng_batch_btn.setToolTip(err)
            QTimer.singleShot(4000, lambda: (
                self._eng_batch_btn.setToolTip(""),
                self._update_eng_buttons(),
            ))
        else:
            self._update_eng_buttons()
        self._browser_eng.refresh()

    def _open_engraving_in_viewer(self) -> None:
        """Open the selected engraving's output PNG in the OS default image viewer."""
        rec = self._current_rec
        if not rec:
            return
        for key in ("output_png", "raw_png"):
            path = rec.get(key, "")
            if path and Path(path).exists():
                from PyQt5.QtCore import QUrl
                from PyQt5.QtGui import QDesktopServices
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
                return

    def _jump_to_silhouette(self) -> None:
        """Switch to the Silhouettes tab and navigate to the source silhouette."""
        rec = self._current_rec
        if not rec:
            return
        from visualizers.components.illustration_browser import _clean_stem
        filename_stem = _clean_stem(rec.get("filename_stem") or "")
        label         = rec.get("label") or ""

        self._side_scroll.setCurrentIndex(0)

        # Clear the grid immediately so the user sees empty state
        # rather than stale previous content during the navigation delay.
        self._browser_sil._filtered_items = []
        self._browser_sil._selected_index = -1
        self._browser_sil._page_index     = 0
        self._browser_sil._rebuild_grid()
        self._browser_sil._loading_bar.start()
        self._browser_sil._loading_timer.start()

        object_id = rec.get("object_id") or ""

        def _navigate() -> None:
            self._browser_sil._loading_timer.stop()
            self._browser_sil._loading_bar.stop()
            self._browser_sil.navigate_direct(
                item      = filename_stem or None,
                keyword   = label or None,
                object_id = object_id or None,
            )

        already_loaded = any(
            _clean_stem(r.get("filename_stem") or "") == filename_stem
            for r in self._browser_sil._all_items
        )

        if already_loaded:
            QTimer.singleShot(150, _navigate)
        else:
            def _after_reload() -> None:
                try:
                    self._browser_sil.catalogReloaded.disconnect(_after_reload)
                except Exception:
                    pass
                QTimer.singleShot(150, _navigate)

            self._browser_sil.catalogReloaded.connect(_after_reload)
            self._browser_sil.reload()

    def _visualize_engraving(self) -> None:
        """Switch to the Engravings tab and navigate to the current silhouette's engraving.

        If the engraving hasn't been loaded into the browser cache yet (e.g. just
        generated), reloads the source first and navigates once loading completes.
        """
        rec = self._current_rec
        if not rec:
            return

        from visualizers.components.illustration_browser import _clean_stem
        filename_stem = _clean_stem(rec.get("filename_stem") or "")
        # Use the silhouette's label directory name as the engraving keyword.
        # Engraving records store the normalised directory name ("a_t__s_f__sign")
        # not the real label ("A.T.&S.F. sign"), so path.parent.name is the match.
        sil_path  = Path(str(rec.get("path", "")))
        eng_label = sil_path.parent.name  # label directory = engraving record label

        # Clear grid + start loading animation immediately on tab switch.
        self._browser_eng._filtered_items = []
        self._browser_eng._selected_index = -1
        self._browser_eng._page_index     = 0
        self._browser_eng._rebuild_grid()
        self._browser_eng._loading_bar.start()
        self._browser_eng._loading_timer.start()

        def _navigate() -> None:
            self._browser_eng._loading_timer.stop()
            self._browser_eng._loading_bar.stop()
            self._browser_eng.navigate_direct(
                item    = filename_stem or None,
                keyword = eng_label or None,
            )

        # Switch to Engravings tab (triggers _on_source_tab_changed).
        self._side_scroll.setCurrentIndex(1)

        # Check whether the target is already in the browser's cached items.
        already_loaded = any(
            _clean_stem(r.get("filename_stem") or "") == filename_stem
            and r.get("label") == eng_label
            for r in self._browser_eng._all_items
        )

        if already_loaded:
            QTimer.singleShot(150, _navigate)
        else:
            # Target not cached yet — reload and navigate once the scan finishes.
            def _after_reload() -> None:
                try:
                    self._browser_eng.catalogReloaded.disconnect(_after_reload)
                except Exception:
                    pass
                QTimer.singleShot(150, _navigate)

            self._browser_eng.catalogReloaded.connect(_after_reload)
            self._browser_eng.reload()

    def _delete_engraving(self) -> None:
        """Delete the selected engraving's mode directory, then refresh the browser.

        The deletion runs in a background thread so the UI stays responsive.
        The engraving directory (``<mode>/``) is removed; parent directories
        (``<object_id>/``, ``<label>/``, ``<film>/``) are left in place so
        sibling engravings are not affected.
        """
        if self._browser is not self._browser_eng:
            return
        rec = self._current_rec
        if not rec:
            return
        eng_json = rec.get("path")
        if not eng_json:
            return
        mode_dir = Path(str(eng_json)).parent
        if not mode_dir.is_dir():
            return
        # Disable the button while deleting to prevent double-fire.
        self._eng_delete_btn.setEnabled(False)
        self._eng_delete_btn.setText("Deleting…")
        self._delete_worker = _DeleteWorker(mode_dir, parent=self)
        self._delete_worker.finished.connect(self._on_delete_finished)
        self._delete_worker.start()

    def _on_delete_finished(self) -> None:
        """Called when the background delete completes; clears state and reloads."""
        self._delete_worker = None
        self._clear_meta()
        self._browser_eng.reload()

    def _open_in_shotlist(self) -> None:
        rec = self._current_rec
        if not rec:
            return
        filename = rec.get("filename") or ""
        shot_id  = str(rec.get("shot_id") or "")
        if not filename:
            return
        from visualizers.shot_visualizer import open_at_shot
        open_at_shot(self._project_path, filename,
                     self._browser._media_type or "movie", shot_id=shot_id,
                     loop=True, no_continue=True, play=True)

    def _open_sam_explorer(self) -> None:
        rec = self._current_rec
        if not rec:
            return
        from tool import prefs as _prefs
        model_name = _prefs.get("model_segmentation", _DEFAULT_MODEL) or _DEFAULT_MODEL
        self._sam_explorer_win = SAMExplorer(
            self._project_path,
            media_type=self._browser._media_type,
            model_name=model_name,
        )
        self._sam_explorer_win.show()
        filename = rec.get("filename") or ""
        shot_id  = str(rec.get("shot_id") or "")
        concept  = rec.get("label") or ""
        if filename and shot_id:
            self._sam_explorer_win.navigate_to(filename, shot_id, concept=concept)

    # ------------------------------------------------------------------
    # Keyboard handling

    def keyPressEvent(self, event) -> None:
        key = event.key()
        mod = event.modifiers()
        if not (mod & (Qt.ControlModifier | Qt.MetaModifier | Qt.AltModifier)):
            ch = event.text().upper()
            if len(ch) == 1 and (ch.isalpha() or ch == "#"):
                self._handle_letter_key(ch)
                return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            if mod & Qt.ShiftModifier:
                self._open_in_shotlist()
            else:
                self._toggle_best()
            return
        if key in (Qt.Key_Home, Qt.Key_End,
                   Qt.Key_PageUp, Qt.Key_PageDown,
                   Qt.Key_Up, Qt.Key_Down,
                   Qt.Key_Left, Qt.Key_Right):
            self._handle_nav_key(key, mod)
        else:
            super().keyPressEvent(event)

    def _handle_nav_key(self, key: int, mod) -> None:
        if key == Qt.Key_Home:
            self._browser.stepItem(-1)
        elif key == Qt.Key_End:
            self._browser.stepItem(1)
        elif key == Qt.Key_PageUp:
            self._browser.stepField(-1)
        elif key == Qt.Key_PageDown:
            self._browser.stepField(1)
        elif key == Qt.Key_Up:
            if mod & Qt.ShiftModifier:
                self._browser.stepKeyword(-1)
            else:
                self._browser.navigate_grid(0, -1)
        elif key == Qt.Key_Down:
            if mod & Qt.ShiftModifier:
                self._browser.stepKeyword(1)
            else:
                self._browser.navigate_grid(0, 1)
        elif key == Qt.Key_Left:
            if mod & Qt.ShiftModifier:
                self._browser._on_prev_page()
            else:
                self._browser.navigate_grid(-1, 0)
        elif key == Qt.Key_Right:
            if mod & Qt.ShiftModifier:
                self._browser._on_next_page()
            else:
                self._browser.navigate_grid(1, 0)

    def _handle_letter_key(self, letter: str) -> None:
        """Toggle the alphabetical bucket filter for *letter*.

        First press → activates the letter filter.
        Second press of the same letter → resets to <Letter> (no filter).
        """
        current = self._browser._letter_combo.currentData()
        if current == letter:
            # Already on this letter — toggle off
            self._browser.navigate_to_filters(letter="--all")
        else:
            self._browser.navigate_to_filters(letter=letter)

    # ------------------------------------------------------------------
    # IPC navigation

    def navigate_to(
        self,
        film: Optional[str] = None,
        field: Optional[str] = None,
        label: Optional[str] = None,
        shot_id: Optional[str] = None,
    ) -> None:
        """Select *field*, *film*, and *label* in the filter combos.

        If *shot_id* is given the matching thumbnail is also selected.
        """
        self._browser.navigate_to_filters(
            item=film,
            field=field,
            keyword=label,
        )
        if shot_id:
            for abs_idx, rec in enumerate(self._browser._filtered_items):
                if str(rec.get("shot_id", "")) == str(shot_id):
                    self._browser.select_index(abs_idx)
                    break

    # ------------------------------------------------------------------
    # Layout helpers

    def _fit_panel_width(self) -> None:
        """Set the Inspector pane to its opening minimum width."""
        total = self._panel_splitter.width()
        if total <= 0:
            QTimer.singleShot(100, self._fit_panel_width)
            return
        pw = _SIDE_PANE_W
        bw = max(1, total - pw)
        self._panel_splitter.setSizes([bw, pw])

    def _toggle_panels(self) -> None:
        """Toggle between BROWSER mode (panels hidden) and TOOLS mode (panels visible).

        TAB key binding.  In BROWSER mode the Filter and Inspector widgets are
        invisible so the Browser fills the entire window.  In TOOLS mode they
        reappear at exactly the widths and collapse state they had before.
        The state (mode + sizes) is persisted to prefs so it survives restarts.
        """
        if self._panels_hidden:
            # ── Restore TOOLS mode ──────────────────────────────────────────
            self._side_scroll.setVisible(True)
            if self._saved_panel_sizes and len(self._saved_panel_sizes) == 2:
                self._panel_splitter.setSizes(self._saved_panel_sizes)
            else:
                QTimer.singleShot(0, self._fit_panel_width)
            self._panels_hidden = False
        else:
            self._saved_panel_sizes = list(self._panel_splitter.sizes())
            self._side_scroll.setVisible(False)
            self._panels_hidden = True
# Main window
# ---------------------------------------------------------------------------

class IllustrationWindow(VisualizerWindow):
    """Top-level host for the framework reference visualizer.

    Owns window shell concerns (geometry, fullscreen state, IPC lifecycle) and
    delegates browsing/presentation responsibilities to ``IllustrationPane``.
    """

    def __init__(
        self,
        project_path: str,
        media_type: Optional[str] = None,
        model_name: str = _DEFAULT_MODEL,
        initial_film: Optional[str] = None,
        initial_field: Optional[str] = None,
        initial_label: Optional[str] = None,
        initial_shot: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("Crossing — Illustration Visualizer")
        self._project_path = project_path

        self._catalog = IllustrationPane(project_path, media_type=media_type)
        if initial_film or initial_field or initial_label or initial_shot:
            QTimer.singleShot(0, lambda: self._catalog.navigate_to(
                initial_film, initial_field, initial_label, initial_shot
            ))
        self.setCentralWidget(self._catalog)
        self.setMinimumSize(900, 560)
        self.resize(1300, 760)
        restore_window_geometry(self, "window_illustration")
        # Reopen in fullscreen if that was the state when the app was last closed
        QTimer.singleShot(0, self._restore_saved_state)

        # IPC server — lets open_at_illustration navigate an existing instance
        self._ipc_server = _IllIpcServer(project_path, parent=self)
        self._ipc_server.navigate_requested.connect(self._on_ipc_navigate)
        self._ipc_server.start()

    def _on_ipc_navigate(
        self, film: str, field: str, label: str, shot_id: str
    ) -> None:
        """Raise this window and navigate the catalog."""
        self.show()
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self._catalog.navigate_to(
            film or None,
            field or None,
            label or None,
            shot_id or None,
        )

    def _restore_saved_state(self) -> None:
        """Restore panel mode, splitter sizes and fullscreen state from prefs.

        Deferred to a single-shot timer so it runs after the initial layout
        pass (_fit_panel_width) has already set default pane widths.
        """
        from tool import prefs as _prefs

        # ── Panel sizes ──────────────────────────────────────────────────────
        saved_sizes = _prefs.get("window_illustration_panel_sizes")
        if saved_sizes and len(saved_sizes) == 2:
            self._catalog._saved_panel_sizes = [int(v) for v in saved_sizes]

        # ── Mode (BROWSER / TOOLS) ───────────────────────────────────────────
        in_browser_mode = bool(_prefs.get("window_illustration_browser_mode"))
        if in_browser_mode:
            self._catalog._panels_hidden = True
            self._catalog._side_scroll.setVisible(False)
            # Browser fills the window; no setSizes needed
        else:
            if self._catalog._saved_panel_sizes:
                self._catalog._panel_splitter.setSizes(self._catalog._saved_panel_sizes)
            else:
                self._catalog._fit_panel_width()

        # ── Fullscreen ───────────────────────────────────────────────────────
        if _prefs.get("window_illustration_fullscreen"):
            self.showFullScreen()

    def closeEvent(self, event) -> None:
        from tool import prefs as _prefs
        _prefs.set("window_illustration_fullscreen", self.isFullScreen())
        _prefs.set("window_illustration_browser_mode", self._catalog._panels_hidden)
        # Save the TOOLS-mode panel sizes.  When in BROWSER mode the splitter
        # sizes are meaningless (panels are invisible), so save the pre-browser
        # sizes that _toggle_panels stored in _saved_panel_sizes instead.
        if self._catalog._panels_hidden and self._catalog._saved_panel_sizes:
            panel_sizes = self._catalog._saved_panel_sizes
        else:
            panel_sizes = list(self._catalog._panel_splitter.sizes())
        _prefs.set("window_illustration_panel_sizes", panel_sizes)
        self._ipc_server.stop()
        self._ipc_server.wait(1000)
        save_window_geometry(self, "window_illustration")
        super().closeEvent(event)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        mod = event.modifiers()
        if key == Qt.Key_Escape:
            self.close()
        elif key in (Qt.Key_Q, Qt.Key_W) and mod & Qt.ControlModifier:
            self.close()
        elif key in (Qt.Key_Backtab, Qt.Key_Tab) and mod & Qt.ShiftModifier:
            # Shift+Tab — toggle true fullscreen
            if self.isFullScreen():
                self.showNormal()
            else:
                self.showFullScreen()
        elif key == Qt.Key_Tab and not (mod & (Qt.ControlModifier | Qt.MetaModifier | Qt.AltModifier)):
            # Tab — toggle Filter + Inspector panes
            self._catalog._toggle_panels()
        elif key in (Qt.Key_Home, Qt.Key_End,
                     Qt.Key_PageUp, Qt.Key_PageDown,
                     Qt.Key_Up, Qt.Key_Down,
                     Qt.Key_Left, Qt.Key_Right):
            self._catalog._handle_nav_key(key, mod)
        elif key in (Qt.Key_Return, Qt.Key_Enter):
            if mod & Qt.ShiftModifier:
                self._catalog._activate_primary_action()
            else:
                self._catalog._toggle_best()
        elif key in (Qt.Key_Delete, Qt.Key_Backspace) and not (
            mod & (Qt.ControlModifier | Qt.MetaModifier | Qt.AltModifier)
        ):
            self._catalog._delete_engraving()
        elif not (mod & (Qt.ControlModifier | Qt.MetaModifier | Qt.AltModifier)):
            ch = event.text().upper()
            if len(ch) == 1 and (ch.isalpha() or ch == "#"):
                self._catalog._handle_letter_key(ch)
        else:
            super().keyPressEvent(event)



def run_visualizer(
    project_path: str,
    media_type: Optional[str] = None,
    field: Optional[str] = None,
    initial_film: Optional[str] = None,
    initial_field: Optional[str] = None,
    initial_label: Optional[str] = None,
    initial_shot: Optional[str] = None,
) -> None:
    """Create QApplication (if needed) and launch the Illustration window."""
    from tool import prefs as _prefs

    model_name = _prefs.get("model_segmentation", _DEFAULT_MODEL) or _DEFAULT_MODEL

    from visualizers._window_helpers import raise_existing_window
    if raise_existing_window("illustration"):
        return

    app = QApplication.instance() or QApplication(sys.argv)
    theme.apply_theme(app)

    win = IllustrationWindow(
        project_path,
        media_type=media_type,
        model_name=model_name,
        initial_film=initial_film or (field and None),
        initial_field=initial_field or field,
        initial_label=initial_label,
        initial_shot=initial_shot,
    )
    win.show()
    sys.exit(app.exec_())


def open_at_illustration(
    project_path: str,
    filename_stem: str = "",
    field: Optional[str] = None,
    media_type: str = "movie",
    label: Optional[str] = None,
    shot_id: Optional[str] = None,
) -> None:
    """Open (or navigate) the Illustration Visualizer.

    If an instance is already running (socket exists), delivers a navigate
    command via IPC and raises the existing window.  Otherwise spawns a new
    process with the supplied filter arguments.
    """
    import subprocess as _sp
    # Try IPC first (works whether the window is in-process or a subprocess)
    if _ill_ipc_send_navigate(
        project_path,
        film=filename_stem,
        field=field or "",
        label=label or "",
        shot_id=str(shot_id) if shot_id else "",
    ):
        # Also raise any in-process window
        from visualizers._window_helpers import raise_existing_window
        raise_existing_window("illustration")
        return
    cmd = [
        sys.executable, str(Path(__file__)),
        "--project", project_path,
        "--media",   media_type,
    ]
    if filename_stem:
        cmd += ["--film", filename_stem]
    if field:
        cmd += ["--field", field]
    if label:
        cmd += ["--label", label]
    if shot_id:
        cmd += ["--shot", str(shot_id)]
    _sp.Popen(cmd)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Illustration Visualizer")
    ap.add_argument("--project", required=True, help="Project path")
    ap.add_argument("--media",  default="movie")
    ap.add_argument("--field",  default=None)
    ap.add_argument("--film",   default=None, help="Initial film stem to select")
    ap.add_argument("--label",  default=None, help="Initial label to select")
    ap.add_argument("--shot",   default=None, help="Initial shot_id to select")
    parsed = ap.parse_args()
    run_visualizer(
        parsed.project,
        media_type=parsed.media,
        initial_film=parsed.film,
        initial_field=parsed.field,
        initial_label=parsed.label,
        initial_shot=parsed.shot,
    )
