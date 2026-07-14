#!/usr/bin/env python3
"""Silhouette Visualizer — catalog browser + segmentation explorer.

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
    crossing visualizer silhouette
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
from visualizers.components.illustration_source import SilhouetteSource


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
            f"QPushButton:hover {{ background: #ff55ff; }}"
            f"QPushButton:pressed {{ background: #cc00cc; }}"
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


# ---------------------------------------------------------------------------
# IPC — single-instance navigate socket
# ---------------------------------------------------------------------------

def _sil_ipc_socket_path(project_path: str) -> Path:
    """Return a per-project socket file path for the Silhouette Visualizer."""
    import tempfile, hashlib
    h = hashlib.md5(str(project_path).encode()).hexdigest()[:8]
    return Path(tempfile.gettempdir()) / f"crossing_silhouette_{h}.sock"


class _SilIpcServer(QThread):
    """Listens on a Unix-domain socket and emits navigate_requested."""

    navigate_requested = pyqtSignal(str, str, str, str)  # film, field, label, shot_id

    def __init__(self, project_path: str, parent=None) -> None:
        super().__init__(parent)
        self._project_path = project_path
        self._running = True

    def run(self) -> None:
        import json as _json
        import socket as _socket
        sock_path = _sil_ipc_socket_path(self._project_path)
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


def _sil_ipc_send_navigate(
    project_path: str,
    film: str = "",
    field: str = "",
    label: str = "",
    shot_id: str = "",
) -> bool:
    """Send a navigate request to a running Silhouette Visualizer.

    Returns True if the message was delivered, False if no server is listening.
    """
    import json as _json
    import socket as _socket
    sock_path = _sil_ipc_socket_path(project_path)
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

class CatalogBrowser(QWidget):
    """Silhouette catalog browser built on the Visualizer Framework.

    Uses ``IllustrationBrowser`` for the LEFT content area (thumbnails,
    filter cascade, pagination) and hosts silhouette-specific controls on
    the RIGHT panel: sort, object inspector, best-selection workflow, and
    action buttons.
    """

    def __init__(self, project_path: str, media_type: Optional[str] = None, parent=None) -> None:
        super().__init__(parent)
        self._project_path = project_path
        self._current_rec: Optional[dict] = None

        # Data source — owns catalog loading and sort
        self._source = SilhouetteSource(project_path)

        # Framework browser — detach_controls=True so it shows only the
        # thumbnail grid; filter_panel / status_bar / pagination_panel are
        # placed in the Inspector pane by _build_ui().
        self._browser = IllustrationBrowser(
            source=self._source,
            media_type=media_type,
            thumb_size=_THUMB_SIZE,
            detach_controls=True,
        )
        self._browser.selectionChanged.connect(self._on_selection_changed)
        # Side-panel toggle state (Tab key)
        self._panels_hidden: bool = False
        self._saved_panel_sizes: list = []   # remembered when panels are hidden

        self._build_ui()
        QTimer.singleShot(0, self._fit_panel_width)

    # ------------------------------------------------------------------
    # UI construction

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._panel_splitter = GripSplitter(Qt.Horizontal)

        # ── Pane 0: Browser (pure thumbnail grid) ─────────────────────────
        self._panel_splitter.addWidget(self._browser)

        # ── Pane 1: Filter — cascade combos + pagination + sort ───────────

        self._panel_splitter.addWidget(self._build_inspector_pane())

        self._panel_splitter.setStretchFactor(0, 1)
        self._panel_splitter.setStretchFactor(1, 0)
        self._panel_splitter.setCollapsible(0, False)


        root.addWidget(self._panel_splitter)

    # --

    def _make_pane(self, tab_label: str, min_width: int = _PANEL_W) -> tuple:
        """Return (QTabWidget, inner QWidget, QVBoxLayout) for a side pane."""
        _TAB_ACTIVE = theme.TAB_BG   # 10 % lighter than CANVAS_BG

        tabs = QTabWidget()
        tabs.setMinimumWidth(min_width)
        tabs.setDocumentMode(True)
        tabs.tabBar().setDrawBase(False)
        # Prevent QTabWidget/QTabBar from consuming Left/Right key events for
        # tab switching — they must propagate to SilhouetteWindow.keyPressEvent.
        tabs.setFocusPolicy(Qt.NoFocus)
        tabs.tabBar().setFocusPolicy(Qt.NoFocus)
        tabs.setStyleSheet(
            f"QTabWidget           {{ background: {theme.CANVAS_BG}; border: none; }}"
            f"QTabWidget::pane     {{ border: none; background: {_TAB_ACTIVE}; top: -1px; }}"
            f"QTabBar              {{ background: {theme.CANVAS_BG}; border: none; }}"
            f"QTabBar::tab {{"
            f"  background: {theme.CANVAS_BG}; color: {theme.TEXT_DIM};"
            f"  padding: 4px 12px; border: none; margin-bottom: 0;"
            f"  font-family: '{theme.FAMILY_UI}'; font-size: {theme.BASE_PT}pt;"
            f"}}"
            f"QTabBar::tab:selected {{ background: {_TAB_ACTIVE}; color: {theme.TEXT}; }}"
            f"QTabBar::tab:hover    {{ background: {_TAB_ACTIVE}; color: {theme.TEXT}; }}"
        )
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFocusPolicy(Qt.NoFocus)
        scroll.viewport().setFocusPolicy(Qt.NoFocus)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet(f"QScrollArea {{ background: {_TAB_ACTIVE}; border: none; }}")

        _content_style = (
            f"QWidget {{ background: {_TAB_ACTIVE}; }}"
            f" QComboBox {{ background-color: {theme.INPUT_BG}; color: {theme.TEXT}; }}"
            f" QComboBox::drop-down {{ border: none; }}"
            f" QComboBox QAbstractItemView, QComboBox QListView {{"
            f"   background: {theme.INPUT_BG}; color: {theme.TEXT};"
            f"   border: 0px; margin: 0px; padding: 0px; outline: 0px;"
            f"   selection-background-color: {theme.ACCENT};"
            f"   selection-color: {theme.TEXT}; }}"
            f" QComboBox QAbstractItemView::item, QComboBox QListView::item {{"
            f"   padding: 3px 8px; border: 0px; }}"
            f" QPushButton {{ background-color: {theme.BTN_BG}; border: none;"
            f" padding: 0 10px; border-radius: 3px;"
            f" min-height: {theme.BTN_H}px; max-height: {theme.BTN_H}px; }}"
            f" QPushButton:hover    {{ background-color: {theme.BTN_HOVER}; }}"
            f" QPushButton:pressed  {{ background-color: {theme.BTN_PRESSED}; }}"
            f" QPushButton:checked  {{ background-color: {theme.ACCENT}; color: {theme.TEXT}; }}"
            f" QPushButton:disabled {{ color: {theme.TEXT_DIM};"
            f" background-color: {theme.BTN_BG}; }}"
        )
        panel = QWidget()
        panel.setStyleSheet(_content_style)
        panel.setMinimumWidth(min_width)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        scroll.setWidget(panel)
        tabs.addTab(scroll, tab_label)
        return tabs, panel, layout
    def _build_inspector_pane(self) -> QTabWidget:
        """Right pane: filter cascade + sort + metadata + actions."""
        tabs, panel, pv = self._make_pane("Inspector", min_width=_SIDE_PANE_W)
        self._side_scroll = tabs

        # ── Filter section (cascade drop-downs + status + pagination) ──────────
        self._filter_sec = CollapsibleSection("Filter", pref_key="sil_section_filter")
        # Wrap the three detached browser widgets into one zero-gap container
        # so there is no visible separator between the combos, status bar, and
        # pagination bar.
        _filter_block = QWidget()
        _fb_lay = QVBoxLayout(_filter_block)
        _fb_lay.setContentsMargins(0, 0, 0, 0)
        _fb_lay.setSpacing(0)
        _fb_lay.addWidget(self._browser.filter_panel)
        _fb_lay.addWidget(self._browser.status_bar)
        _fb_lay.addWidget(self._browser.pagination_panel)
        self._filter_sec.add_widget(_filter_block)
        self._browser.keywordChanged.connect(
            lambda kw: self._filter_sec.set_subtitle(kw.capitalize() if kw else "")
        )
        pv.addWidget(self._filter_sec)

        # ── Sort section ─────────────────────────────────────────────────────────
        self._sort_sec = CollapsibleSection("Sort", pref_key="sil_section_sort")
        self._sort_combo = QComboBox()
        self._sort_combo.setFocusPolicy(Qt.NoFocus)
        self._sort_combo.setMaxVisibleItems(10)
        self._sort_combo.setSizeAdjustPolicy(QComboBox.AdjustToContentsOnFirstShow)
        _sv = QListView(self._sort_combo)
        _sv.setUniformItemSizes(True)
        _sv.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        _sv.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        _sv.setFrameShape(QFrame.NoFrame)
        _sv.setLineWidth(0)
        _sv.setMidLineWidth(0)
        _sv.setContentsMargins(0, 0, 0, 0)
        _sv.setStyleSheet(
            f"QListView {{"
            f" background: {theme.INPUT_BG}; color: {theme.TEXT};"
            f" border: 0px; margin: 0px; padding: 0px; outline: 0px; }}"
            f"QListView::item {{ padding: 3px 8px; border: 0px; }}"
            f"QListView::item:selected {{"
            f" background: {theme.ACCENT}; color: {theme.TEXT}; }}"
        )
        self._sort_combo.setView(_sv)
        _sc = _sv.parentWidget()
        if _sc is not None:
            _sc.setFrameStyle(QFrame.NoFrame)
            _sc.setLineWidth(0)
            _sc.setMidLineWidth(0)
            _sc.setStyleSheet(
                "QFrame { border: 0px; margin: 0px; padding: 0px; }"
            )
        self._sort_combo.addItem("-----", userData=None)
        for disp, key in _SORT_OPTS:
            self._sort_combo.addItem(disp, userData=key)
        self._sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        self._sort_sec.add_widget(self._sort_combo)
        pv.addWidget(self._sort_sec)

        # ── Object section ───────────────────────────────────────────────────────
        info_sec = CollapsibleSection("Info", pref_key="sil_section_object")
        _info_widget = QWidget()
        _info_grid = QGridLayout(_info_widget)
        _info_grid.setContentsMargins(0, 0, 0, 0)
        _info_grid.setHorizontalSpacing(0)
        _info_grid.setVerticalSpacing(0)
        _info_grid.setColumnStretch(0, 0)
        _info_grid.setColumnStretch(1, 1)
        self._meta_rows: dict[str, QLabel] = {}
        _INFO_KEYS = (
            "label", "film", "shot", "frame", "confidence",
            "usefulness", "fullness", "size", "overlap",
            "semantic_label", "semantic_field", "model",
        )
        _LAST_INFO_IDX = len(_INFO_KEYS) - 1
        for _row_idx, key in enumerate(_INFO_KEYS):
            _top    = f" border-top: 2px solid {theme.TAB_BG};"    if _row_idx == 0            else ""
            _bottom = f" border-bottom: 2px solid {theme.TAB_BG};" if _row_idx < _LAST_INFO_IDX else ""
            kl = QLabel(key)
            kl.setAlignment(Qt.AlignTop | Qt.AlignRight)
            kl.setStyleSheet(
                f"color: {theme.TEXT_DIM};"
                f"{_top}"
                f" border-right: 2px solid {theme.TAB_BG};"
                f"{_bottom}"
                f" padding: 0px 4px 2px 2px;"
            )
            _info_grid.addWidget(kl, _row_idx, 0)
            vl = QLabel("\u2014")
            vl.setWordWrap(True)
            vl.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            vl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            vl.setStyleSheet(
                f"{_top}{_bottom}"
                f" padding: 0px 2px 2px 3px;"
            )
            _info_grid.addWidget(vl, _row_idx, 1)
            self._meta_rows[key] = vl
        info_sec.add_widget(_info_widget)
        # Remove the body's bottom margin so the last row touches the section edge.
        info_sec._body_layout.setContentsMargins(0, 0, 0, 0)
        pv.addWidget(info_sec)

        action_sec = CollapsibleSection("Actions", pref_key="sil_section_actions")
        _icon_sz   = QSize(14, 14)
        # Build an icon with two modes: normal (white) and disabled (dim grey)
        # so the icon fades to match the barely-visible disabled text.
        _pix_normal   = _svg_icon("open-in-window", 14, theme.TEXT).pixmap(14, 14)
        _pix_disabled = _svg_icon("open-in-window", 14, "#7f7f7f").pixmap(14, 14)
        _open_icon = QIcon()
        _open_icon.addPixmap(_pix_normal)
        _open_icon.addPixmap(_pix_disabled, QIcon.Disabled)

        # Shared style for all three action buttons:
        # • normal   — dark button background, bright text
        # • hover    — fuchsia background, white text
        # • checked  — fuchsia background (Best button when marked)
        # • disabled — same dark background, barely-visible text
        _abtn = (
            f"QPushButton {{"
            f"  background-color: {theme.BTN_BG}; color: {theme.TEXT};"
            f"  border: none; border-radius: 3px;"
            f"  padding: 0 8px;"
            f"  min-height: {theme.BTN_H}px; max-height: {theme.BTN_H}px;"
            f"}}"
            f"QPushButton:hover   {{ background-color: {theme.ACCENT}; color: {theme.TEXT}; }}"
            f"QPushButton:pressed {{ background-color: {theme.BTN_PRESSED}; }}"
            f"QPushButton:checked {{ background-color: {theme.ACCENT}; color: {theme.TEXT}; }}"
            f"QPushButton:disabled {{ background-color: {theme.BTN_BG};"
            f" color: rgba(255,255,255,0.15); }}"
        )

        _action_row = QWidget()
        _action_rl  = QHBoxLayout(_action_row)
        _action_rl.setContentsMargins(0, 0, 0, 0)
        _action_rl.setSpacing(4)

        self._shotlist_btn = QPushButton("Shotlist")
        self._shotlist_btn.setIcon(_open_icon)
        self._shotlist_btn.setIconSize(_icon_sz)
        self._shotlist_btn.setFocusPolicy(Qt.NoFocus)
        self._shotlist_btn.setEnabled(False)
        self._shotlist_btn.setStyleSheet(_abtn)
        self._shotlist_btn.clicked.connect(self._open_in_shotlist)
        _action_rl.addWidget(self._shotlist_btn, 1)

        self._sam_btn = QPushButton("Segmentation")
        self._sam_btn.setIcon(_open_icon)
        self._sam_btn.setIconSize(_icon_sz)
        self._sam_btn.setFocusPolicy(Qt.NoFocus)
        self._sam_btn.setEnabled(False)
        self._sam_btn.setStyleSheet(_abtn)
        self._sam_btn.clicked.connect(self._open_sam_explorer)
        _action_rl.addWidget(self._sam_btn, 1)

        action_sec.add_widget(_action_row)

        self._best_btn = QPushButton("Best")
        self._best_btn.setCheckable(True)
        self._best_btn.setFocusPolicy(Qt.NoFocus)
        self._best_btn.setEnabled(False)
        self._best_btn.setFixedHeight(theme.BTN_H)
        self._best_btn.setStyleSheet(_abtn)
        self._best_btn.clicked.connect(self._on_best_btn_clicked)
        action_sec.add_widget(self._best_btn)
        pv.addWidget(action_sec)

        pv.addStretch()

        hint = QLabel(
            "Home/End  film    PgUp/PgDn  field\n"
            "\u2190 \u2191 \u2192 \u2193  grid    a\u2013z / #  letter\n"
            "Shift+\u2191\u2193  keyword    Shift+\u2190\u2192  page\n"
            "Enter  toggle best    Shift+Enter  shotlist"
        )
        hint.setAlignment(Qt.AlignCenter)
        hint.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-size: {theme.BASE_PT - 1}pt;"
        )
        pv.addWidget(hint)

        return tabs
    def _on_splitter_moved(self, pos: int, index: int) -> None:
        pass  # two-pane — no locking needed

    # ------------------------------------------------------------------
    # Sort controls

    def _on_sort_changed(self, _idx: int) -> None:
        """Update the source sort order, refresh the browser, and show the
        active sort key in the Sort section title."""
        key  = self._sort_combo.currentData()   # None when '-----' selected
        keys = [key] if key else []
        self._source.set_sort_keys(keys)
        self._browser.refresh()
        # Reflect active sort in the section header
        disp = self._sort_combo.currentText() if key else ""
        self._sort_sec.set_subtitle(disp)

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
        self._shotlist_btn.setEnabled(False)
        self._sam_btn.setEnabled(False)
        self._best_btn.setEnabled(False)
        self._best_btn.blockSignals(True)
        self._best_btn.setChecked(False)
        self._best_btn.blockSignals(False)

    def _show_object_meta(self, rec: dict) -> None:
        shot_id = str(rec.get("shot_id", "—"))
        if len(shot_id) > 28:
            shot_id = "…" + shot_id[-26:]
        film = rec.get("filename_stem") or rec.get("filename") or "—"
        if len(film) > 26:
            film = film[:24] + "…"
        conf = rec.get("confidence", 0)

        self._meta_rows["label"].setText(rec.get("label", "—"))
        self._meta_rows["film"].setText(film)
        self._meta_rows["shot"].setText(shot_id)
        self._meta_rows["frame"].setText(str(rec.get("frame", "—")))
        self._meta_rows["confidence"].setText(f"{conf:.3f}")
        self._meta_rows["model"].setText(rec.get("sam_model", "—"))

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

        # size: derive from mask_area + frame_size when not yet scored
        size_val = _stored("size")
        if size_val is None:
            mask_area = rec.get("mask_area")
            frame_size = rec.get("frame_size") or []
            if mask_area is not None and len(frame_size) >= 2:
                frame_area = float(max(1, frame_size[0] * frame_size[1]))
                area_frac = float(mask_area) / frame_area
                size_val = max(0.0, min(1.0, (area_frac - 0.002) / max(1e-9, 0.298)))

        # fullness: derive from mask_area + bbox when not yet scored
        fullness_val = _stored("fullness")
        if fullness_val is None:
            mask_area = rec.get("mask_area")
            bbox = rec.get("bbox") or []
            if mask_area is not None and len(bbox) >= 4:
                bbox_area = float(max(1, bbox[2] * bbox[3]))
                fullness_val = max(0.0, min(1.0, float(mask_area) / bbox_area))

        self._meta_rows["usefulness"].setText(_fmt(_stored("usefulness")))
        self._meta_rows["fullness"].setText(_fmt(fullness_val))
        self._meta_rows["size"].setText(_fmt(size_val))
        self._meta_rows["overlap"].setText(_fmt(_stored("overlap")))
        self._meta_rows["semantic_label"].setText(_fmt(_stored("semantic_label")))
        self._meta_rows["semantic_field"].setText(_fmt(_stored("semantic_field")))
        self._current_rec = rec
        _can_open = bool(rec.get("filename") and rec.get("shot_id"))
        self._shotlist_btn.setEnabled(_can_open)
        self._sam_btn.setEnabled(_can_open)

    # ------------------------------------------------------------------
    # Best-selection workflow

    def _update_best_btn(self) -> None:
        rec = self._browser.currentItem()
        if rec is None:
            self._best_btn.setEnabled(False)
            self._best_btn.blockSignals(True)
            self._best_btn.setChecked(False)
            self._best_btn.blockSignals(False)
            return
        self._best_btn.setEnabled(True)
        self._best_btn.blockSignals(True)
        self._best_btn.setChecked(bool(rec.get("human_best")))
        self._best_btn.blockSignals(False)

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
        """Jump to the alphabetical bucket *letter* in the browser's letter combo."""
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

class SilhouetteWindow(VisualizerWindow):
    """Top-level window: Silhouette catalog browser."""

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
        self.setWindowTitle("Crossing — Silhouette Visualizer")
        self._project_path = project_path

        self._catalog = CatalogBrowser(project_path, media_type=media_type)
        if initial_film or initial_field or initial_label or initial_shot:
            QTimer.singleShot(0, lambda: self._catalog.navigate_to(
                initial_film, initial_field, initial_label, initial_shot
            ))
        self.setCentralWidget(self._catalog)
        self.setMinimumSize(900, 560)
        self.resize(1300, 760)
        restore_window_geometry(self, "window_silhouette")
        # Reopen in fullscreen if that was the state when the app was last closed
        QTimer.singleShot(0, self._restore_saved_state)

        # IPC server — lets open_at_silhouette navigate an existing instance
        self._ipc_server = _SilIpcServer(project_path, parent=self)
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
        saved_sizes = _prefs.get("window_silhouette_panel_sizes")
        if saved_sizes and len(saved_sizes) == 2:
            self._catalog._saved_panel_sizes = [int(v) for v in saved_sizes]

        # ── Mode (BROWSER / TOOLS) ───────────────────────────────────────────
        in_browser_mode = bool(_prefs.get("window_silhouette_browser_mode"))
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
        if _prefs.get("window_silhouette_fullscreen"):
            self.showFullScreen()

    def closeEvent(self, event) -> None:
        from tool import prefs as _prefs
        _prefs.set("window_silhouette_fullscreen", self.isFullScreen())
        _prefs.set("window_silhouette_browser_mode", self._catalog._panels_hidden)
        # Save the TOOLS-mode panel sizes.  When in BROWSER mode the splitter
        # sizes are meaningless (panels are invisible), so save the pre-browser
        # sizes that _toggle_panels stored in _saved_panel_sizes instead.
        if self._catalog._panels_hidden and self._catalog._saved_panel_sizes:
            panel_sizes = self._catalog._saved_panel_sizes
        else:
            panel_sizes = list(self._catalog._panel_splitter.sizes())
        _prefs.set("window_silhouette_panel_sizes", panel_sizes)
        self._ipc_server.stop()
        self._ipc_server.wait(1000)
        save_window_geometry(self, "window_silhouette")
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
                self._catalog._open_in_shotlist()
            else:
                self._catalog._toggle_best()
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
    """Create QApplication (if needed) and launch the Silhouette window."""
    from tool import prefs as _prefs

    model_name = _prefs.get("model_segmentation", _DEFAULT_MODEL) or _DEFAULT_MODEL

    from visualizers._window_helpers import raise_existing_window
    if raise_existing_window("silhouette"):
        return

    app = QApplication.instance() or QApplication(sys.argv)
    theme.apply_theme(app)

    win = SilhouetteWindow(
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


def open_at_silhouette(
    project_path: str,
    filename_stem: str = "",
    field: Optional[str] = None,
    media_type: str = "movie",
    label: Optional[str] = None,
    shot_id: Optional[str] = None,
) -> None:
    """Open (or navigate) the Silhouette Visualizer.

    If an instance is already running (socket exists), delivers a navigate
    command via IPC and raises the existing window.  Otherwise spawns a new
    process with the supplied filter arguments.
    """
    import subprocess as _sp
    # Try IPC first (works whether the window is in-process or a subprocess)
    if _sil_ipc_send_navigate(
        project_path,
        film=filename_stem,
        field=field or "",
        label=label or "",
        shot_id=str(shot_id) if shot_id else "",
    ):
        # Also raise any in-process window
        from visualizers._window_helpers import raise_existing_window
        raise_existing_window("silhouette")
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

    ap = argparse.ArgumentParser(description="Silhouette Visualizer")
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
