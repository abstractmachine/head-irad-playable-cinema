#!/usr/bin/env python3
"""Silhouette Visualizer — catalog browser + SAM-3 shot explorer.

Two tabs in one window:

  **Catalog** (default)
    Browse extracted silhouette objects by vocabulary label.
    Shows all transparent PNG objects already in the catalog.
    Left panel: label list → film/shot tree.
    Right panel: thumbnail grid of all objects for the selected entry.
    Clicking a thumbnail shows the full object and its metadata.

  **SAM-3 Explorer**
    Interactive shot inspection.  Browse movies → scenes → shots,
    enter a concept, click Run SAM-3 to see masks on the best frame.

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
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
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


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PANEL_W = 310
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
# SAM background worker
# ---------------------------------------------------------------------------

class _SAMWorker(QThread):
    """Run SAM-3 concept segmentation in a background thread."""

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
            self.progress.emit(f"Running SAM-3 segmentation for '{self._concept}'…")
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
# Info block widget
# ---------------------------------------------------------------------------

class _InfoBlock(QWidget):
    """Fixed grid of key–value label pairs for the right-hand info panel."""

    def __init__(self, rows: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self._labels: dict[str, QLabel] = {}
        for key in rows:
            row_widget = QWidget()
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(4)

            key_lbl = QLabel(f"{key}:")
            key_lbl.setStyleSheet(
                f"color: {theme.TEXT_DIM};"
                f" font-family: '{theme.FAMILY_MONO}';"
                f" font-size: {theme.BASE_PT}pt;"
            )
            key_lbl.setFixedWidth(72)
            key_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            row_layout.addWidget(key_lbl)

            val_lbl = QLabel("—")
            val_lbl.setStyleSheet(
                f"color: {theme.TEXT};"
                f" font-family: '{theme.FAMILY_MONO}';"
                f" font-size: {theme.BASE_PT}pt;"
            )
            val_lbl.setWordWrap(True)
            row_layout.addWidget(val_lbl, 1)

            layout.addWidget(row_widget)
            self._labels[key] = val_lbl

    def set(self, key: str, value: str) -> None:
        if key in self._labels:
            self._labels[key].setText(value)

    def clear(self) -> None:
        for lbl in self._labels.values():
            lbl.setText("—")


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class SAMExplorer(QMainWindow):
    """SAM-3 shot exploration visualizer.

    Browse movies → scenes → shots, see each shot's best frame, run SAM-3
    concept segmentation on it, and inspect every mask returned.
    """

    def __init__(
        self,
        project_path: str,
        media_type: str = "movies",
        model_name: str = _DEFAULT_MODEL,
    ) -> None:
        super().__init__()
        self.setWindowTitle("Crossing — SAM-3 Explorer")

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

        # Frame + SAM state
        self._bgr: Optional[np.ndarray] = None
        self._frame_source: str = ""
        self._masks: list[dict] = []
        self._blobs: list[dict] = []
        self._effective_model: str = ""
        self._sam_worker: Optional[_SAMWorker] = None

        # Concept input for SAM-3
        self._concept: str = ""

        # Guard against recursive combo signal handling
        self._updating: bool = False

        self._build_ui()
        restore_window_geometry(self, "window_sam_explorer")
        self._load_films()

    def closeEvent(self, event) -> None:
        if self._sam_worker and self._sam_worker.isRunning():
            self._sam_worker.terminate()
            self._sam_worker.wait(2000)
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

        # Concept input for SAM-3
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
        self._sam_btn = QPushButton("▶  Run SAM-3")
        self._sam_btn.setFixedHeight(32)
        self._sam_btn.setStyleSheet(
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
        self._sam_btn.setFocusPolicy(Qt.NoFocus)
        self._sam_btn.clicked.connect(self._run_sam)
        panel_layout.addWidget(self._sam_btn)

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
        self._frame_info = _InfoBlock(["film", "scene", "shot", "frame", "model", "blobs"])
        frame_layout.addWidget(self._frame_info)
        panel_layout.addWidget(frame_group)

        # Hovered blob info
        blob_group = QGroupBox("Hovered Blob")
        blob_layout = QVBoxLayout(blob_group)
        blob_layout.setContentsMargins(8, 10, 8, 8)
        self._blob_info = _InfoBlock(["#", "area", "bbox", "iou", "stability"])
        blob_layout.addWidget(self._blob_info)
        panel_layout.addWidget(blob_group)

        # Keyboard hint
        hint = QLabel(
            "↑ ↓ shot   PgUp / PgDn scene\n"
            "Home / End movie   s = SAM-3"
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
        self._canvas.setFocus()

    # ------------------------------------------------------------------
    # Data loading

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
            self._scene_idx = 0
            self._scene_combo.setCurrentIndex(0)
            self._on_scene_changed(0)

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
        if bgr is not None and bf_png.exists():
            bf = shot.get("best_frame", {}) or {}
            frame_num = bf.get("frame", "?")
            self._frame_source = f"cached f{frame_num}"
        elif bgr is not None:
            start = int(shot.get("start_frame") or 0)
            end = int(shot.get("end_frame") or start)
            self._frame_source = f"midpoint f{(start + end) // 2}"
        else:
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
    # SAM-3 execution

    def _on_concept_changed(self, text: str) -> None:
        self._concept = text.strip()

    def _run_sam(self) -> None:
        if self._bgr is None:
            self._status_lbl.setText("No frame loaded — select a shot first.")
            return
        if not self._concept:
            self._status_lbl.setText("Enter a concept before running SAM-3.")
            return
        if self._sam_worker and self._sam_worker.isRunning():
            return

        self._sam_btn.setEnabled(False)
        self._status_lbl.setText(f"Starting SAM-3 for '{self._concept}'…")
        self._masks = []
        self._blobs = []
        self._canvas.clear_blobs()

        self._sam_worker = _SAMWorker(
            self._project_path, self._model_name, self._bgr, self._concept
        )
        self._sam_worker.progress.connect(self._status_lbl.setText)
        self._sam_worker.masks_ready.connect(self._on_masks_ready)
        self._sam_worker.error.connect(self._on_sam_error)
        self._sam_worker.start()

    def _on_masks_ready(self, raw_masks: list, effective_model: str) -> None:
        self._sam_btn.setEnabled(True)
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

    def _on_sam_error(self, msg: str) -> None:
        self._sam_btn.setEnabled(True)
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
            self._run_sam()
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

_THUMB_SIZE  = 120   # px per thumbnail cell
_THUMB_GAP   = 8    # px gap between cells
_PAGE_SIZE   = 100  # max thumbnails shown at once
_LOAD_BATCH  = 20   # loader yields UI thread every N images


# ---------------------------------------------------------------------------
# Lazy thumbnail loader
# ---------------------------------------------------------------------------

class _ThumbLoader(QThread):
    """Background thread: loads PNG thumbnails and emits QImages to the UI thread.

    QImage is safe to construct off-thread; QPixmap conversion happens in the
    receiving slot (GUI thread).
    """

    thumb_ready = pyqtSignal(int, QImage)   # (page index, image)
    load_finished = pyqtSignal(int)          # total loaded count

    def __init__(self, records: list[dict], size: int, parent=None) -> None:
        super().__init__(parent)
        self._records = records
        self._size = size
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        from PIL import Image as _PIL

        loaded = 0
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
                loaded += 1
            except Exception:
                pass
            if (i + 1) % _LOAD_BATCH == 0:
                self.msleep(2)   # yield so UI stays responsive
        self.load_finished.emit(loaded)


# ---------------------------------------------------------------------------

class _ThumbnailCell(QLabel):
    """Single grid cell — shows a grey placeholder until the loader fills it."""

    clicked = pyqtSignal(int)

    def __init__(self, index: int, tooltip: str = "", parent=None) -> None:
        super().__init__(parent)
        self._index = index
        self._selected = False
        self.setFixedSize(_THUMB_SIZE, _THUMB_SIZE)
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
        border = f"2px solid {theme.ACCENT}" if self._selected else "none"
        self.setStyleSheet(
            f"background: {theme.CANVAS_BG}; border: {border};"
            f" color: {theme.TEXT_DIM};"
            f" font-family: '{theme.FAMILY_MONO}'; font-size: {theme.BASE_PT}pt;"
        )

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._index)
        super().mousePressEvent(event)


class CatalogBrowser(QWidget):
    """Browse the silhouette catalog — mosaic-style layout.

    LEFT  — full-bleed scrollable thumbnail grid (content area)
    RIGHT — fixed panel: movie scope, field, label, object metadata
    """

    # Standard annotation field order (same as Cloud/Mosaic visualizers)
    _FIELD_ORDER = [
        "--all", "setting", "description", "objects",
        "action", "humans", "wearing", "animals", "text",
    ]

    def __init__(self, project_path: str, media_type: str = "movies", parent=None) -> None:
        super().__init__(parent)
        self._project_path = project_path
        self._media_type = media_type
        # _field_map[field][label] = [records]
        # field "--all" covers every record regardless of field
        self._field_map: dict[str, dict[str, list[dict]]] = {}
        self._film_list: list[str] = []
        self._current_records: list[dict] = []  # after label + film filter
        self._page_records: list[dict] = []     # slice shown in grid
        self._page_offset: int = 0
        self._cells: list[_ThumbnailCell] = []
        self._selected_idx: int = -1
        self._loader: Optional[_ThumbLoader] = None
        self._build_ui()
        self._load_catalog()
        # Reflow grid once Qt has finalised the window/splitter geometry
        QTimer.singleShot(0, self._reflow_grid)

    # ------------------------------------------------------------------
    # UI construction

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        splitter = GripSplitter(Qt.Horizontal)

        # ── LEFT: thumbnail scroll area (content / canvas area) ───────────
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBar(JumpScrollBar())
        self._scroll.setStyleSheet(
            f"QScrollArea {{ background: {theme.CANVAS_BG}; border: none; }}"
        )

        self._grid_widget = QWidget()
        self._grid_widget.setStyleSheet(f"background: {theme.CANVAS_BG};")
        self._grid_layout = QGridLayout(self._grid_widget)
        self._grid_layout.setContentsMargins(_THUMB_GAP, _THUMB_GAP, _THUMB_GAP, _THUMB_GAP)
        self._grid_layout.setSpacing(_THUMB_GAP)
        self._grid_layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._scroll.setWidget(self._grid_widget)
        self._scroll.setFocusPolicy(Qt.NoFocus)
        self._scroll.viewport().setFocusPolicy(Qt.NoFocus)
        self._scroll.installEventFilter(self)
        self._scroll.viewport().installEventFilter(self)
        splitter.addWidget(self._scroll)

        # ── RIGHT: control panel (same pattern as mosaic_visualizer) ─────
        panel = QWidget()
        panel.setFixedWidth(_PANEL_W)
        panel.setStyleSheet(
            f"QWidget {{ background: {theme.PANEL_BG}; }}"
            f" QComboBox {{ background-color: {theme.INPUT_BG}; }}"
            f" QPushButton {{ background-color: {theme.BTN_BG}; border: none;"
            f" padding: 0 10px; border-radius: 3px;"
            f" min-height: {theme.BTN_H}px; max-height: {theme.BTN_H}px; }}"
            f" QPushButton:hover    {{ background-color: {theme.BTN_HOVER}; }}"
            f" QPushButton:pressed  {{ background-color: {theme.BTN_PRESSED}; }}"
            f" QPushButton:checked  {{ background-color: {theme.ACCENT}; color: {theme.TEXT}; }}"
            f" QPushButton:disabled {{ color: {theme.TEXT_DIM};"
            f" background-color: {theme.BTN_BG}; }}"
        )
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(14, 14, 14, 14)
        pv.setSpacing(14)

        # Movie Scope (top)
        movie_group = QGroupBox("Movie Scope")
        mg = QVBoxLayout(movie_group)
        mg.setContentsMargins(8, 12, 8, 8)
        self._film_combo = QComboBox()
        self._film_combo.setFocusPolicy(Qt.NoFocus)
        self._film_combo.currentIndexChanged.connect(self._on_film_changed)
        self._film_combo.installEventFilter(self)
        mg.addWidget(self._film_combo)
        pv.addWidget(movie_group)

        # Field (second)
        field_group = QGroupBox("Field")
        fieldg = QVBoxLayout(field_group)
        fieldg.setContentsMargins(8, 12, 8, 8)
        self._field_combo = QComboBox()
        self._field_combo.setFocusPolicy(Qt.NoFocus)
        self._field_combo.currentIndexChanged.connect(self._on_field_changed)
        self._field_combo.installEventFilter(self)
        fieldg.addWidget(self._field_combo)
        pv.addWidget(field_group)

        # Label (third)
        label_group = QGroupBox("Label")
        lg = QVBoxLayout(label_group)
        lg.setContentsMargins(8, 12, 8, 8)
        self._label_combo = QComboBox()
        self._label_combo.setFocusPolicy(Qt.NoFocus)
        self._label_combo.currentIndexChanged.connect(self._on_label_changed)
        self._label_combo.installEventFilter(self)
        lg.addWidget(self._label_combo)
        pv.addWidget(label_group)

        self._status_lbl = QLabel("—")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-size: {theme.BASE_PT - 1}pt;"
        )
        pv.addWidget(self._status_lbl)

        self._more_btn = QPushButton(f"Load {_PAGE_SIZE} more  ↓")
        self._more_btn.setFocusPolicy(Qt.NoFocus)
        self._more_btn.setVisible(False)
        self._more_btn.clicked.connect(self._load_more)
        pv.addWidget(self._more_btn)

        # Selected object
        obj_group = QGroupBox("Object")
        ov = QVBoxLayout(obj_group)
        ov.setContentsMargins(8, 12, 8, 8)
        ov.setSpacing(4)

        self._preview = QLabel()
        self._preview.setAlignment(Qt.AlignCenter)
        self._preview.setMinimumHeight(140)
        self._preview.setMaximumHeight(200)
        self._preview.setStyleSheet(f"background: {theme.CANVAS_BG};")
        ov.addWidget(self._preview)

        self._meta_rows: dict[str, QLabel] = {}
        for key in ("label", "film", "shot", "frame", "confidence", "model"):
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(4)
            kl = QLabel(f"{key}:")
            kl.setFixedWidth(64)
            kl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            kl.setStyleSheet(f"color: {theme.TEXT_DIM};")
            rl.addWidget(kl)
            vl = QLabel("—")
            vl.setWordWrap(True)
            rl.addWidget(vl, 1)
            ov.addWidget(row)
            self._meta_rows[key] = vl

        pv.addWidget(obj_group)
        pv.addStretch()

        hint = QLabel("Home/End  movie    PgUp/PgDn  field\n↑ ↓  label    ← →  page")
        hint.setAlignment(Qt.AlignCenter)
        hint.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-size: {theme.BASE_PT - 1}pt;"
        )
        pv.addWidget(hint)

        self._sam_btn = QPushButton("SAM-3 Explorer →")
        self._sam_btn.setFocusPolicy(Qt.NoFocus)
        self._sam_btn.clicked.connect(self._open_sam_explorer)
        pv.addWidget(self._sam_btn)

        splitter.addWidget(panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([10000, _PANEL_W])
        root.addWidget(splitter)

    # ------------------------------------------------------------------
    # Catalog loading

    def _load_catalog(self) -> None:
        self._stop_loader()
        from services.silhouette_catalog import scan_catalog

        all_records = scan_catalog(self._project_path, media_type=self._media_type)

        # Build field_map[field][label] = [records]
        # Also maintain an "--all" key that aggregates across all fields
        field_map: dict[str, dict[str, list[dict]]] = {"--all": {}}
        film_set: set[str] = set()
        for rec in all_records:
            if "error" in rec:
                continue
            label = rec.get("label") or ""
            field = rec.get("field") or "--all"
            if not label:
                continue
            # per-field bucket
            field_map.setdefault(field, {}).setdefault(label, []).append(rec)
            # --all bucket
            field_map["--all"].setdefault(label, []).append(rec)
            stem = rec.get("filename_stem") or rec.get("filename") or ""
            if stem:
                film_set.add(stem)

        self._field_map = field_map
        self._film_list = sorted(film_set)

        # Populate film combo
        self._film_combo.blockSignals(True)
        self._film_combo.clear()
        self._film_combo.addItem("All films", userData=None)
        for stem in self._film_list:
            self._film_combo.addItem(stem, userData=stem)
        self._film_combo.blockSignals(False)

        # Populate field combo (standard order, only fields with data)
        present_fields = set(field_map.keys())
        self._field_combo.blockSignals(True)
        self._field_combo.clear()
        for f in self._FIELD_ORDER:
            if f in present_fields:
                self._field_combo.addItem(f, userData=f)
        # Any fields not in the standard order go at the end
        for f in sorted(present_fields - set(self._FIELD_ORDER)):
            self._field_combo.addItem(f, userData=f)
        self._field_combo.blockSignals(False)

        if not field_map.get("--all"):
            self._status_lbl.setText(
                "Catalog empty.\n\ncrossing index silhouette\nextract <label>"
            )
            self._label_combo.clear()
            self._clear_grid()
            return

        # Trigger field→label cascade from the first field entry
        self._field_combo.setCurrentIndex(0)
        self._on_field_changed(0)

    # ------------------------------------------------------------------
    # Filtering

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
        self._page_offset = 0
        if self._label_combo.count() > 0:
            self._label_combo.setCurrentIndex(0)
        self._apply_filters()

    def _on_label_changed(self, _idx: int) -> None:
        self._page_offset = 0
        self._apply_filters()

    def _on_film_changed(self, _idx: int) -> None:
        self._page_offset = 0
        self._apply_filters()

    def _apply_filters(self) -> None:
        field = self._field_combo.currentData() or "--all"
        label = self._label_combo.currentData()
        film  = self._film_combo.currentData()
        records = self._field_map.get(field, {}).get(label, []) if label else []
        if film:
            records = [
                r for r in records
                if (r.get("filename_stem") or r.get("filename") or "") == film
            ]
        self._current_records = records
        self._selected_idx = -1
        self._clear_meta()
        self._show_page(0)

    def _show_page(self, offset: int) -> None:
        self._stop_loader()
        self._page_offset = offset
        total = len(self._current_records)
        end = min(offset + _PAGE_SIZE, total)
        self._page_records = self._current_records[offset:end]

        if total == 0:
            self._status_lbl.setText("No objects found")
        else:
            self._status_lbl.setText(f"{offset + 1}–{end} of {total}   loading…")

        self._more_btn.setVisible(end < total)
        self._rebuild_grid()
        self._start_loader()

    def _load_more(self) -> None:
        new_off = self._page_offset + _PAGE_SIZE
        if new_off < len(self._current_records):
            self._show_page(new_off)

    # ------------------------------------------------------------------
    # Grid management

    def _clear_grid(self) -> None:
        self._stop_loader()
        self._cells = []
        while self._grid_layout.count():
            item = self._grid_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _rebuild_grid(self) -> None:
        self._clear_grid()
        cols = self._cols()
        for i, rec in enumerate(self._page_records):
            stem = rec.get("filename_stem") or rec.get("filename") or ""
            shot = rec.get("shot_id", "")
            frame = rec.get("frame", "")
            conf = rec.get("confidence", 0)
            tip = f"#{self._page_offset + i + 1}  {stem}  shot:{shot}  f:{frame}  conf:{conf:.3f}"
            cell = _ThumbnailCell(i, tooltip=tip)
            cell.clicked.connect(self._on_cell_clicked)
            self._grid_layout.addWidget(cell, i // cols, i % cols)
            self._cells.append(cell)

    def _cols(self) -> int:
        vw = self._scroll.viewport().width()
        if vw <= 0:
            vw = 800
        return max(1, (vw - _THUMB_GAP) // (_THUMB_SIZE + _THUMB_GAP))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if not self._cells:
            return
        cols = self._cols()
        for i, cell in enumerate(self._cells):
            self._grid_layout.addWidget(cell, i // cols, i % cols)

    def _reflow_grid(self) -> None:
        """Reflow after Qt has committed the first layout pass."""
        if self._cells:
            self.resizeEvent(None)
        else:
            # No cells yet means the loader hasn't fired — rebuild from scratch
            # so column count is correct for the actual viewport width.
            if self._page_records:
                self._rebuild_grid()
                self._start_loader()

    # ------------------------------------------------------------------
    # Background loader

    def _start_loader(self) -> None:
        self._loader = _ThumbLoader(self._page_records, _THUMB_SIZE)
        self._loader.thumb_ready.connect(self._on_thumb_ready)
        self._loader.load_finished.connect(self._on_load_finished)
        self._loader.start()

    def _stop_loader(self) -> None:
        if self._loader and self._loader.isRunning():
            self._loader.cancel()
            self._loader.wait(500)
        self._loader = None

    def _on_thumb_ready(self, idx: int, qimg: QImage) -> None:
        if 0 <= idx < len(self._cells):
            self._cells[idx].set_image(qimg)

    def _on_load_finished(self, loaded: int) -> None:
        total = len(self._current_records)
        off = self._page_offset
        end = min(off + _PAGE_SIZE, total)
        self._status_lbl.setText(f"{off + 1}–{end} of {total}  ·  {loaded} loaded")

    # ------------------------------------------------------------------
    # Object selection

    def _on_cell_clicked(self, idx: int) -> None:
        if self._selected_idx == idx:
            return
        if 0 <= self._selected_idx < len(self._cells):
            self._cells[self._selected_idx].set_selected(False)
        self._selected_idx = idx
        if 0 <= idx < len(self._cells):
            self._cells[idx].set_selected(True)
        if idx < len(self._page_records):
            self._show_object_meta(self._page_records[idx])

    def _clear_meta(self) -> None:
        self._preview.clear()
        self._preview.setText("")
        for lbl in self._meta_rows.values():
            lbl.setText("—")

    def _show_object_meta(self, rec: dict) -> None:
        from PIL import Image as _PIL

        json_path = rec.get("path")
        png_path = Path(str(json_path)).with_suffix(".png") if json_path else None
        if png_path and png_path.exists():
            try:
                img = _PIL.open(str(png_path)).convert("RGBA")
                img.thumbnail((_PANEL_W - 28, 190), _PIL.LANCZOS)
                w, h = img.size
                data = img.tobytes("raw", "RGBA")
                qimg = QImage(data, w, h, 4 * w, QImage.Format_RGBA8888)
                self._preview.setPixmap(QPixmap.fromImage(qimg))
            except Exception:
                self._preview.setText("error")
        else:
            self._preview.setText("no png")

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

    # ------------------------------------------------------------------
    # Keyboard handling — event filter intercepts keys stolen by child widgets

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.KeyPress:
            key = event.key()
            mod = event.modifiers()
            if key in (Qt.Key_Home, Qt.Key_End,
                       Qt.Key_PageUp, Qt.Key_PageDown,
                       Qt.Key_Up, Qt.Key_Down,
                       Qt.Key_Left, Qt.Key_Right):
                self._handle_nav_key(key, mod)
                return True
            if key == Qt.Key_Escape:
                # bubble up to the window
                return False
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        mod = event.modifiers()
        if key in (Qt.Key_Home, Qt.Key_End,
                   Qt.Key_PageUp, Qt.Key_PageDown,
                   Qt.Key_Up, Qt.Key_Down,
                   Qt.Key_Left, Qt.Key_Right):
            self._handle_nav_key(key, mod)
        else:
            super().keyPressEvent(event)

    def _handle_nav_key(self, key: int, mod) -> None:
        if key == Qt.Key_Home:
            idx = self._film_combo.currentIndex()
            if idx > 0:
                self._film_combo.setCurrentIndex(idx - 1)
        elif key == Qt.Key_End:
            idx = self._film_combo.currentIndex()
            if idx < self._film_combo.count() - 1:
                self._film_combo.setCurrentIndex(idx + 1)
        elif key == Qt.Key_PageUp:
            idx = self._field_combo.currentIndex()
            if idx > 0:
                self._field_combo.setCurrentIndex(idx - 1)
        elif key == Qt.Key_PageDown:
            idx = self._field_combo.currentIndex()
            if idx < self._field_combo.count() - 1:
                self._field_combo.setCurrentIndex(idx + 1)
        elif key == Qt.Key_Up:
            idx = self._label_combo.currentIndex()
            if idx > 0:
                self._label_combo.setCurrentIndex(idx - 1)
        elif key == Qt.Key_Down:
            idx = self._label_combo.currentIndex()
            if idx < self._label_combo.count() - 1:
                self._label_combo.setCurrentIndex(idx + 1)
        elif key == Qt.Key_Left:
            if self._page_offset > 0:
                self._show_page(max(0, self._page_offset - _PAGE_SIZE))
        elif key == Qt.Key_Right:
            next_off = self._page_offset + _PAGE_SIZE
            if next_off < len(self._current_records):
                self._show_page(next_off)

    def _open_sam_explorer(self) -> None:
        from tool import prefs as _prefs
        model_name = _prefs.get("model_segmentation", _DEFAULT_MODEL) or _DEFAULT_MODEL
        self._sam_explorer_win = SAMExplorer(
            self._project_path, media_type=self._media_type, model_name=model_name
        )
        self._sam_explorer_win.show()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class SilhouetteWindow(QMainWindow):
    """Top-level window: Silhouette catalog browser."""

    def __init__(
        self,
        project_path: str,
        media_type: str = "movies",
        model_name: str = _DEFAULT_MODEL,
    ) -> None:
        super().__init__()
        self.setWindowTitle("Crossing — Silhouette")

        self._catalog = CatalogBrowser(project_path, media_type=media_type)
        self.setCentralWidget(self._catalog)
        self.setMinimumSize(900, 560)
        self.resize(1300, 760)
        restore_window_geometry(self, "window_silhouette")

    def closeEvent(self, event) -> None:
        save_window_geometry(self, "window_silhouette")
        super().closeEvent(event)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        mod = event.modifiers()
        if key == Qt.Key_Escape:
            self.close()
        elif key in (Qt.Key_Q, Qt.Key_W) and mod & Qt.ControlModifier:
            self.close()
        elif key in (Qt.Key_Home, Qt.Key_End,
                     Qt.Key_PageUp, Qt.Key_PageDown,
                     Qt.Key_Up, Qt.Key_Down,
                     Qt.Key_Left, Qt.Key_Right):
            self._catalog._handle_nav_key(key, mod)
        else:
            super().keyPressEvent(event)



def run_visualizer(
    project_path: str,
    media_type: str = "movies",
    field: Optional[str] = None,   # accepted for CLI compat; not used
) -> None:
    """Create QApplication (if needed) and launch the Silhouette window."""
    from tool import prefs as _prefs

    model_name = _prefs.get("model_segmentation", _DEFAULT_MODEL) or _DEFAULT_MODEL

    app = QApplication.instance() or QApplication(sys.argv)
    theme.apply_theme(app)

    win = SilhouetteWindow(project_path, media_type=media_type, model_name=model_name)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Silhouette Visualizer")
    ap.add_argument("--project", required=True, help="Project path")
    ap.add_argument("--media", default="movies")
    ap.add_argument("--field", default=None, help="(unused, kept for compat)")
    parsed = ap.parse_args()
    run_visualizer(parsed.project, media_type=parsed.media, field=parsed.field)
