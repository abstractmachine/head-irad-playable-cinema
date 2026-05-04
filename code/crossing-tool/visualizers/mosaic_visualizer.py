#!/usr/bin/env python3
"""Mosaic Visualizer — live, interactive visual browser for shot search results.

Launched via:
    crossing generate mosaic visualizer

The window has:
  LEFT  — scrollable, zoomable mosaic canvas (image tiles)
  RIGHT — control panel (movie scope, field, query, options)

Zoom: Ctrl + scroll wheel
Pan:  scroll wheel / scrollbars
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

# Allow imports from the tool root (data/, services/, generators/)
sys.path.insert(0, str(Path(__file__).parent.parent))

from styles import theme
from styles.theme import JumpScrollBar, save_window_geometry, restore_window_geometry
from services.frame_match import best_frame_path

# Fix Qt plugin conflict with OpenCV — import PyQt5 before cv2
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,  # noqa: F401 (unused after refactor, kept for import compatibility)
    QStatusBar,
    QVBoxLayout,
    QWidget,
)
from PyQt5.QtGui import QImage, QPixmap, QWheelEvent

import cv2
import numpy as np

if "QT_QPA_PLATFORM_PLUGIN_PATH" in os.environ:
    del os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"]

from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ANNOTATION_FIELDS = [
    "--all",
    "setting",
    "objects",
    "wearing",
    "action",
    "humans",
    "animals",
    "text",
    "description",
]

DEFAULT_TILE_SIZE = 200
MIN_TILE_SIZE = 80
MAX_TILE_SIZE = 480
ZOOM_STEP = 24


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@lru_cache(maxsize=64)
def _get_sar(video_path: str) -> tuple:
    """Return (sar_num, sar_den) for video_path via ffprobe. Falls back to (1, 1)."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=sample_aspect_ratio", "-of", "json", video_path],
            capture_output=True, text=True, timeout=5,
        )
        data = json.loads(result.stdout)
        sar_str = data["streams"][0].get("sample_aspect_ratio", "1:1")
        if sar_str in ("", "0:1", "1:1"):
            return (1, 1)
        parts = sar_str.replace("/", ":").split(":")
        return (int(parts[0]), int(parts[1]))
    except Exception:
        return (1, 1)


def _extract_frame_pixmap(video_path: Path, frame_index: int) -> Optional[QPixmap]:
    """Extract a single video frame and return as QPixmap. Returns None on failure.

    Applies SAR correction so the pixmap has the correct display aspect ratio.
    """
    try:
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            return None
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ret, bgr = cap.read()
        cap.release()
        if not ret:
            return None
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        sar = _get_sar(str(video_path))
        if sar != (1, 1):
            display_w = int(round(rgb.shape[1] * sar[0] / sar[1]))
            rgb = cv2.resize(rgb, (display_w, rgb.shape[0]), interpolation=cv2.INTER_LINEAR)
        h, w, ch = rgb.shape
        qimg = QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888)
        return QPixmap.fromImage(qimg)
    except Exception:
        return None


def _find_video_path(project_path: str, movie_id: str) -> Optional[Path]:
    """Locate the video file whose stem matches *movie_id*.

    Mirrors the same logic as services.mosaic._find_video_path.
    """
    videos_base = Path(project_path) / "media" / "videos"
    if not videos_base.exists():
        return None
    for subdir in sorted(videos_base.iterdir()):
        if not subdir.is_dir():
            continue
        for f in subdir.iterdir():
            if f.is_file() and f.stem == movie_id:
                return f
    return None


def _open_video_looping(path: str) -> None:
    """Open a video file in a looping player.

    Tries mpv, vlc, and ffplay (all with a loop flag) in order before
    falling back to xdg-open (which typically plays once and exits).
    """
    import shutil
    import subprocess

    if shutil.which("mpv"):
        subprocess.Popen(["mpv", "--loop=inf", path])
    elif shutil.which("vlc"):
        subprocess.Popen(["vlc", "--repeat", path])
    elif shutil.which("ffplay"):
        subprocess.Popen(["ffplay", "-loop", "0", path])
    else:
        subprocess.Popen(["xdg-open", path])


def _short_title(raw_title: str) -> str:
    """Strip TMDB suffix and year for compact tile captions."""
    s = re.sub(r"\s*\{tmdb-\d+\}", "", raw_title).strip()
    # Also strip trailing (year) for very long titles
    if len(s) > 32:
        s = re.sub(r"\s*\(\d{4}\)$", "", s).strip()
    return s if s else raw_title


# ---------------------------------------------------------------------------
# Background worker: search + frame extraction
# ---------------------------------------------------------------------------

class SearchWorker(QThread):
    """Runs search_shots() + per-result frame extraction in a background thread.

    Signals
    -------
    tile_ready(result_dict, pixmap_or_None)
        Emitted for every search result once its frame is resolved.
    finished_signal(total_count)
        Emitted when all results have been processed.
    error(message)
        Emitted on unexpected errors.
    """

    tile_ready      = pyqtSignal(dict, object)
    finished_signal = pyqtSignal(int)
    error           = pyqtSignal(str)

    def __init__(
        self,
        query: str,
        scope: Optional[str],
        field: Optional[str],
        limit: Optional[int],
        limit_per_movie: bool,
        project_path: str,
        best_mode: bool = False,
        model_name: str = "clip-vit-base-patch32",
        parent=None,
    ):
        super().__init__(parent)
        self.query           = query
        self.scope           = scope
        self.field           = field
        self.limit           = limit
        self.limit_per_movie = limit_per_movie
        self.project_path    = project_path
        self.best_mode       = best_mode
        self.model_name      = model_name
        self._cancelled      = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            from services.search import search_shots

            scopes   = None if (not self.scope or self.scope == "--all") else [self.scope]
            use_all  = scopes is None
            field    = None if (not self.field or self.field == "--all") else self.field
            limit_pi = self.limit if self.limit_per_movie else None

            result  = search_shots(
                query          = self.query,
                scopes         = scopes,
                field          = field,
                limit          = self.limit,
                limit_per_item = limit_pi,
                use_all        = use_all,
                project_path   = self.project_path,
            )
            results = result.get("results", [])

            # Load CLIP model once when query-based best-frame matching is needed
            clip_model = clip_processor = clip_device = None
            if self.best_mode and self.query and results:
                try:
                    from services.frame_match import _load_clip_model
                    clip_model, clip_processor, clip_device = _load_clip_model(
                        self.project_path, self.model_name
                    )
                except Exception:
                    pass  # fall back to midpoint

            count = 0
            for r in results:
                if self._cancelled:
                    break

                movie_id   = r.get("movie_id", "")
                video_path = _find_video_path(self.project_path, movie_id)
                pixmap     = None

                if video_path is not None:
                    sf = r.get("start_frame")
                    ef = r.get("end_frame")

                    if self.best_mode and self.query and clip_model is not None:
                        try:
                            from services.frame_match import find_query_best_frame_for_shot
                            frame_index, best_score = find_query_best_frame_for_shot(
                                project_path=self.project_path,
                                filename=r.get("filename", ""),
                                shot_id=r.get("shot_id", ""),
                                query=self.query,
                                media_type="movies",
                                model=clip_model,
                                processor=clip_processor,
                                device=clip_device,
                            )
                            r = dict(r)
                            r["frame"] = frame_index
                            r["score"] = round(best_score, 6)
                        except Exception:
                            if sf is not None and ef is not None:
                                frame_index = int(sf + (ef - sf) * 0.5)
                            elif sf is not None:
                                frame_index = int(sf)
                            else:
                                frame_index = 0
                    else:
                        if sf is not None and ef is not None:
                            frame_index = int(sf + (ef - sf) * 0.5)
                        elif sf is not None:
                            frame_index = int(sf)
                        else:
                            frame_index = 0

                    pixmap = _extract_frame_pixmap(video_path, frame_index)

                self.tile_ready.emit(r, pixmap)
                count += 1

            self.finished_signal.emit(count)

        except Exception as exc:
            import traceback
            self.error.emit(f"{exc}\n{traceback.format_exc()}")


# ---------------------------------------------------------------------------
# Vocabulary background worker
# ---------------------------------------------------------------------------

class VocabularyWorker(QThread):
    """Fetches vocabulary for one annotation field in a background thread.

    Signals
    -------
    items_ready(list)   list of {"value": str, "count": int} dicts
    error(str)          emitted on failure
    """

    items_ready = pyqtSignal(list)
    error       = pyqtSignal(str)

    def __init__(self, field: str, scope: Optional[str], project_path: str, parent=None):
        super().__init__(parent)
        self.field        = field
        self.scope        = scope
        self.project_path = project_path

    def run(self) -> None:
        try:
            from services.search import vocabulary_from_field
            scopes  = None if (not self.scope or self.scope == "--all") else [self.scope]
            use_all = scopes is None
            result  = vocabulary_from_field(
                field        = self.field,
                scopes       = scopes,
                use_all      = use_all,
                show_count   = True,
                project_path = self.project_path,
                media_type   = "movies",
                sort         = "count",
            )
            self.items_ready.emit(result)
        except Exception as exc:
            import traceback
            self.error.emit(f"{exc}\n{traceback.format_exc()}")


# ---------------------------------------------------------------------------
# Background worker: JPEG export
# ---------------------------------------------------------------------------

class ExportWorker(QThread):
    """Runs export_frames_from_search_results() in a background thread.

    Signals
    -------
    finished_signal(export_dir_str)
        Emitted with the output folder path when done.
    error(message)
        Emitted on failure.
    """

    finished_signal = pyqtSignal(str)
    error           = pyqtSignal(str)

    def __init__(
        self,
        results: list,
        project_path: str,
        query: str,
        field: Optional[str],
        parent=None,
    ):
        super().__init__(parent)
        self.results      = results
        self.project_path = project_path
        self.query        = query
        self.field        = field

    def run(self) -> None:
        try:
            from generators.mosaic import export_frames_from_search_results
            out_dir = export_frames_from_search_results(
                self.results,
                self.project_path,
                query=self.query,
                field=self.field,
            )
            self.finished_signal.emit(str(out_dir))
        except Exception as exc:
            import traceback
            self.error.emit(f"{exc}\n{traceback.format_exc()}")


# ---------------------------------------------------------------------------
# Background worker: video mosaic generation
# ---------------------------------------------------------------------------

class VideoMosaicWorker(QThread):
    """Runs mosaic_video_from_search_results() in a background thread.

    Signals
    -------
    finished_signal(output_path_str)
        Emitted with the saved .mp4 path when done.
    error(message)
        Emitted on failure.
    """

    finished_signal = pyqtSignal(str)
    error           = pyqtSignal(str)

    def __init__(
        self,
        results: list,
        project_path: str,
        query: str,
        fps: int,
        duration: int,
        layout: str,
        limit: int,
        parent=None,
    ):
        super().__init__(parent)
        self.results      = results
        self.project_path = project_path
        self.query        = query
        self.fps          = fps
        self.duration     = duration
        self.layout       = layout
        self.limit        = limit

    def run(self) -> None:
        try:
            from generators.mosaic import mosaic_video_from_search_results
            out = mosaic_video_from_search_results(
                self.results,
                self.project_path,
                layout   = self.layout,
                fps      = self.fps,
                duration = self.duration,
                limit    = self.limit,
                query    = self.query,
            )
            self.finished_signal.emit(str(out))
        except Exception as exc:
            import traceback
            self.error.emit(f"{exc}\n{traceback.format_exc()}")


# ---------------------------------------------------------------------------
# Individual tile widget
# ---------------------------------------------------------------------------

class TileWidget(QFrame):
    """A single mosaic tile: scaled video frame + short caption + hover tooltip."""

    _PLACEHOLDER_BG = theme.CANVAS_BG

    clicked = pyqtSignal(dict)   # emitted with the result dict on left-click

    def __init__(
        self,
        result: dict,
        pixmap: Optional[QPixmap],
        tile_size: int,
        parent=None,
    ):
        super().__init__(parent)
        self.result          = result
        self.original_pixmap = pixmap

        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(f"TileWidget {{ border: none; background: {theme.CANVAS_BG}; }}")
        self.setCursor(Qt.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self._img_label = QLabel()
        self._img_label.setAlignment(Qt.AlignCenter)
        self._img_label.setStyleSheet(f"background: {self._PLACEHOLDER_BG}; border: none;")
        layout.addWidget(self._img_label)

        frame = result.get("frame")
        if frame is not None:
            caption_text = f"f{int(frame):06d}"
        else:
            caption_text = _short_title(result.get("movie_title", "") or result.get("movie_id", ""))
        self._cap_label = QLabel(caption_text)
        self._cap_label.setAlignment(Qt.AlignCenter)
        self._cap_label.setStyleSheet(
            f"color: {theme.TEXT_DIM}; border: none;"
            f" font-family: '{theme.FAMILY_UI}'; font-size: {theme.BASE_PT}pt;"
        )
        self._cap_label.setMaximumHeight(16)
        layout.addWidget(self._cap_label)

        # Tooltip with full metadata
        movie_title   = result.get("movie_title", result.get("movie_id", ""))
        shot_id       = result.get("shot_id", "")
        matched       = ", ".join(result.get("matched_fields", []))
        matched_text  = result.get("matched_text", "")
        score         = result.get("score", 0.0)
        self.setToolTip(
            f"{movie_title}\n"
            f"Shot: {shot_id}\n"
            f"Fields: {matched}\n"
            f"Match: {matched_text[:80]}\n"
            f"Score: {score:.4f}"
        )

        self._render(tile_size)

    # ------------------------------------------------------------------

    def _tile_dimensions(self, tile_size: int) -> tuple[int, int]:
        """Return (img_w, img_h) for the given tile_size (height).

        Uses the actual pixmap aspect ratio so wide/narrow films are displayed
        correctly instead of being forced into 16:9 and cropped.
        """
        img_h = tile_size
        if self.original_pixmap is not None and not self.original_pixmap.isNull():
            pw = self.original_pixmap.width()
            ph = self.original_pixmap.height()
            if ph > 0:
                img_w = max(1, int(tile_size * pw / ph))
                return img_w, img_h
        img_w = int(tile_size * 16 / 9)  # fallback for missing frames
        return img_w, img_h

    def _render(self, tile_size: int) -> None:
        img_w, img_h = self._tile_dimensions(tile_size)
        self.setFixedSize(img_w, img_h + 18)
        self._img_label.setFixedSize(img_w, img_h)

        if self.original_pixmap is not None and not self.original_pixmap.isNull():
            scaled = self.original_pixmap.scaled(
                img_w, img_h,
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
            self._img_label.setPixmap(scaled)
            self._img_label.setText("")
        else:
            self._img_label.setPixmap(QPixmap())
            self._img_label.setText("⚠")
            self._img_label.setStyleSheet(
                f"background: {self._PLACEHOLDER_BG}; color: {theme.TEXT_DIM}; font-size: 24px; border: none;"
            )

    def resize_tile(self, tile_size: int) -> None:
        """Re-render at a new tile_size (called when user zooms)."""
        self._render(tile_size)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.result)
        super().mousePressEvent(event)


# ---------------------------------------------------------------------------
# Mosaic canvas — scrollable grid of tiles
# ---------------------------------------------------------------------------

class MosaicCanvas(QScrollArea):
    """Scrollable viewport containing a grid of TileWidget tiles.

    Ctrl + scroll wheel  → zoom in / out
    Plain scroll wheel   → vertical scroll (default)
    """

    tile_clicked = pyqtSignal(dict)   # forwarded from individual TileWidget clicks

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setStyleSheet(f"QScrollArea {{ border: none; background: {theme.CANVAS_BG}; }}")
        self.setVerticalScrollBar(JumpScrollBar())

        self._tile_size: int = DEFAULT_TILE_SIZE
        self._tiles: list[TileWidget] = []

        self._container = QWidget()
        self._container.setStyleSheet(f"background: {theme.CANVAS_BG};")
        self.setWidget(self._container)

    _SPACING = 4
    _MARGIN  = 8

    # ------------------------------------------------------------------
    # Public API

    @property
    def tile_count(self) -> int:
        return len(self._tiles)

    def clear(self) -> None:
        for tile in self._tiles:
            tile.deleteLater()
        self._tiles.clear()
        self._container.setFixedSize(max(self.viewport().width(), 1), 1)

    def add_tile(self, result: dict, pixmap) -> None:
        tile = TileWidget(result, pixmap, self._tile_size, parent=self._container)
        tile.clicked.connect(self.tile_clicked)
        tile.show()
        self._tiles.append(tile)
        self._do_flow_layout()

    # ------------------------------------------------------------------
    # Flow layout

    def _do_flow_layout(self) -> None:
        """Position all tiles with word-wrap: wrap to the next row when a
        tile's right edge would exceed the viewport width."""
        if not self._tiles:
            self._container.setFixedSize(max(self.viewport().width(), 1), 1)
            return

        spacing = self._SPACING
        margin  = self._MARGIN
        vp_w    = self.viewport().width()

        x     = margin
        y     = margin
        row_h = 0

        for tile in self._tiles:
            tw = tile.width()
            th = tile.height()

            # If this tile's right edge would exceed the usable width, wrap.
            if x > margin and x + tw > vp_w - margin:
                x      = margin
                y     += row_h + spacing
                row_h  = 0

            tile.move(x, y)
            x    += tw + spacing
            row_h = max(row_h, th)

        total_h = y + row_h + margin
        self._container.setFixedSize(max(vp_w, 1), max(total_h, 1))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._do_flow_layout()

    # ------------------------------------------------------------------
    # Zoom

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.ControlModifier:
            delta = event.angleDelta().y()
            if delta > 0:
                self._tile_size = min(MAX_TILE_SIZE, self._tile_size + ZOOM_STEP)
            else:
                self._tile_size = max(MIN_TILE_SIZE, self._tile_size - ZOOM_STEP)
            self._apply_zoom()
            event.accept()
        else:
            super().wheelEvent(event)

    def _apply_zoom(self) -> None:
        for tile in self._tiles:
            tile.resize_tile(self._tile_size)
        self._do_flow_layout()


# ---------------------------------------------------------------------------
# Background worker: best-only tile loading
# ---------------------------------------------------------------------------

class BestOnlyWorker(QThread):
    """Resolves precomputed best-frame PNG paths in a background thread.

    Emits the result dict and the resolved path string so that QPixmap
    construction happens on the main thread (Qt requirement).

    Signals
    -------
    tile_ready(result_dict, img_path_str_or_None)
        Emitted for every shot with the PNG path (or None if missing).
    finished_signal(total_count)
        Emitted when all shots have been processed.
    """

    tile_ready      = pyqtSignal(dict, object)
    finished_signal = pyqtSignal(int)

    def __init__(self, lookup: dict, filename: str, project_path: str, parent=None):
        super().__init__(parent)
        self.lookup       = lookup
        self.filename     = filename
        self.project_path = project_path
        self._cancelled   = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        count = 0
        for shot_id, bf in self.lookup.items():
            if self._cancelled:
                break
            img_path = best_frame_path(
                self.project_path, "movies", self.filename, shot_id
            )
            path_str = str(img_path) if img_path.exists() else None
            result = {
                "movie_title": self.filename,
                "movie_id":    Path(self.filename).stem,
                "shot_id":     shot_id,
                "frame":       bf.get("frame"),
            }
            self.tile_ready.emit(result, path_str)
            count += 1
        self.finished_signal.emit(count)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

_CTRL_PANEL_WIDTH = 270


class MosaicVisualizer(QMainWindow):
    """Interactive mosaic visualizer window."""

    def __init__(self, project_path: str):
        super().__init__()
        self.project_path = project_path
        self._worker: Optional[SearchWorker] = None
        self._best_worker: Optional[BestOnlyWorker] = None
        self._vocab_worker: Optional[VocabularyWorker] = None
        self._export_worker: Optional[ExportWorker] = None
        self._video_worker: Optional[VideoMosaicWorker] = None
        self._current_results: list = []   # results for the last completed search
        self.best_mode: bool = False
        self._best_lookup: dict = {}
        self._query_best_active: bool = False

        self.setWindowTitle("Crossing — Mosaic Visualizer")
        self.resize(1440, 900)
        restore_window_geometry(self, "window_mosaic")

        # Central layout: left = canvas (expands), divider, right = controls (fixed)
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.canvas = MosaicCanvas()

        # 1px fuchsia progress bar sits between canvas and the rest of the layout
        self._progress = QProgressBar()
        self._progress.setFixedHeight(1)
        self._progress.setTextVisible(False)
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self._progress.setStyleSheet(
            f"QProgressBar {{ background-color: {theme.UI_BORDER}; border: none; "
            f"border-radius: 0px; max-height: 1px; }}"
            f"QProgressBar::chunk {{ background-color: {theme.ACCENT}; "
            f"border-radius: 0px; }}"
        )

        canvas_col = QWidget()
        canvas_col.setStyleSheet("QWidget { background: transparent; }")
        canvas_vbox = QVBoxLayout(canvas_col)
        canvas_vbox.setContentsMargins(0, 0, 0, 0)
        canvas_vbox.setSpacing(0)
        canvas_vbox.addWidget(self._progress)
        canvas_vbox.addWidget(self.canvas, stretch=1)
        root.addWidget(canvas_col, stretch=1)

        divider = QFrame()
        divider.setFrameShape(QFrame.VLine)
        divider.setFrameShadow(QFrame.Plain)
        divider.setFixedWidth(1)
        divider.setStyleSheet(f"background: {theme.UI_BORDER};")
        root.addWidget(divider)

        ctrl = self._build_control_panel()
        root.addWidget(ctrl)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("Ready — enter a query and press Search.")

        self.canvas.tile_clicked.connect(self._on_tile_clicked)
        self._populate_movies()

    # ------------------------------------------------------------------
    # Control panel

    def _on_tile_clicked(self, result: dict) -> None:
        """Open the clicked shot in the Shotlist Visualizer.

        If the Shotlist Visualizer is already running, jumps via IPC.
        Otherwise, launches a new process and passes --shot-id so it opens
        directly at the correct shot.
        """
        filename = result.get("filename") or result.get("movie_title", "")
        shot_id  = str(result.get("shot_id", ""))
        if not filename:
            self.status.showMessage("Cannot open shot: no filename in result.", 4000)
            return

        from visualizers.shot_visualizer import ipc_send_load
        sent = ipc_send_load(
            self.project_path,
            filename,
            "movies",
            shot_id=shot_id,
            playback="pause",
        )
        if sent:
            self.status.showMessage(
                f"Jumped Shotlist Visualizer → {filename}  shot {shot_id}", 4000
            )
            return

        # No running instance — launch one.
        visualizer_path = Path(__file__).parent / "shot_visualizer.py"
        cmd = [
            sys.executable, str(visualizer_path),
            "--project", self.project_path,
            "--media", "movies",
            "--filenames", filename,
        ]
        if shot_id:
            cmd += ["--shot-id", shot_id]
        subprocess.Popen(cmd)
        self.status.showMessage(
            f"Launching Shotlist Visualizer → {filename}  shot {shot_id}", 4000
        )

    # ------------------------------------------------------------------
    # Control panel

    def _build_control_panel(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(_CTRL_PANEL_WIDTH)
        panel.setStyleSheet(
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

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(14)

        # Movie scope
        scope_group = QGroupBox("Movie Scope")
        scope_layout = QVBoxLayout(scope_group)
        scope_layout.setContentsMargins(8, 12, 8, 8)
        self.movie_combo = QComboBox()
        self.movie_combo.addItem("--all")
        self.movie_combo.currentIndexChanged.connect(self._on_field_changed)
        self.movie_combo.currentIndexChanged.connect(self._update_best_button)
        scope_layout.addWidget(self.movie_combo)
        layout.addWidget(scope_group)

        # Annotation field
        field_group = QGroupBox("Annotation Field")
        field_layout = QVBoxLayout(field_group)
        field_layout.setContentsMargins(8, 12, 8, 8)
        self.field_combo = QComboBox()
        for f in ANNOTATION_FIELDS:
            self.field_combo.addItem(f)
        self.field_combo.currentIndexChanged.connect(self._on_field_changed)
        field_layout.addWidget(self.field_combo)
        layout.addWidget(field_group)

        # Query + search button
        query_group = QGroupBox("Search Query")
        query_layout = QVBoxLayout(query_group)
        query_layout.setContentsMargins(8, 12, 8, 8)
        self.query_input = QLineEdit()
        self.query_input.setPlaceholderText("e.g. gun,  sunset,  rain…")
        self.query_input.returnPressed.connect(self._on_search)
        self.query_input.textChanged.connect(self._update_search_button)
        query_layout.addWidget(self.query_input)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        self.search_btn = QPushButton("Search")
        self.search_btn.setEnabled(False)
        self.search_btn.clicked.connect(self._on_search)
        btn_row.addWidget(self.search_btn)
        self.best_btn = QPushButton("Best")
        self.best_btn.setCheckable(True)
        self.best_btn.setEnabled(False)
        self.best_btn.clicked.connect(self._on_best_toggle)
        btn_row.addWidget(self.best_btn)
        self.export_btn = QPushButton("Export")
        self.export_btn.setEnabled(False)
        self.export_btn.setToolTip(
            "Export each result as an individual JPEG with search info overlay\n"
            "into output/exports/<query>-<timestamp>/"
        )
        self.export_btn.clicked.connect(self._on_export)
        btn_row.addWidget(self.export_btn)
        self._export_btn_image_tip = (
            "Export each result as an individual JPEG with search info overlay\n"
            "into output/exports/<query>-<timestamp>/"
        )
        self._export_btn_video_tip = (
            "Generate a looping video mosaic (.mp4) from the current results\n"
            "saved to output/mosaics/video/search/"
        )

        btn_container = QFrame()
        btn_container.setStyleSheet(
            f"QFrame {{ background: {theme.INPUT_BG}; border-radius: 3px; }}"
        )
        btn_container.setLayout(btn_row)
        btn_row.setContentsMargins(4, 4, 4, 4)
        query_layout.addWidget(btn_container)
        layout.addWidget(query_group)

        # Options
        opt_group = QGroupBox("Options")
        opt_layout = QVBoxLayout(opt_group)
        opt_layout.setContentsMargins(8, 12, 8, 8)

        limit_row = QHBoxLayout()
        lim_label = QLabel("Limit:")
        lim_label.setFixedWidth(46)
        self.limit_combo = QComboBox()
        for opt in ["10", "50", "200", "all"]:
            self.limit_combo.addItem(opt)
        self.limit_combo.setCurrentText("50")
        limit_row.addWidget(lim_label)
        limit_row.addWidget(self.limit_combo)
        opt_layout.addLayout(limit_row)

        self.limit_per_movie_cb = QCheckBox("Limit per movie")
        opt_layout.addWidget(self.limit_per_movie_cb)

        # Output mode
        mode_row = QHBoxLayout()
        mode_label = QLabel("Mode:")
        mode_label.setFixedWidth(54)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("image")
        self.mode_combo.addItem("video")
        self.mode_combo.currentTextChanged.connect(self._on_mode_changed)
        mode_row.addWidget(mode_label)
        mode_row.addWidget(self.mode_combo)
        opt_layout.addLayout(mode_row)

        # FPS row — shown only in video mode
        self._fps_row = QWidget()
        fps_row_layout = QHBoxLayout(self._fps_row)
        fps_row_layout.setContentsMargins(0, 0, 0, 0)
        fps_label = QLabel("FPS:")
        fps_label.setFixedWidth(54)
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 30)
        self.fps_spin.setValue(8)
        fps_row_layout.addWidget(fps_label)
        fps_row_layout.addWidget(self.fps_spin)
        opt_layout.addWidget(self._fps_row)
        self._fps_row.setVisible(False)

        # Duration row — shown only in video mode
        self._dur_row = QWidget()
        dur_row_layout = QHBoxLayout(self._dur_row)
        dur_row_layout.setContentsMargins(0, 0, 0, 0)
        dur_label = QLabel("Dur (s):")
        dur_label.setFixedWidth(54)
        self.dur_spin = QSpinBox()
        self.dur_spin.setRange(1, 10)
        self.dur_spin.setValue(2)
        dur_row_layout.addWidget(dur_label)
        dur_row_layout.addWidget(self.dur_spin)
        opt_layout.addWidget(self._dur_row)
        self._dur_row.setVisible(False)

        layout.addWidget(opt_group)

        # Vocabulary
        vocab_group = QGroupBox("Vocabulary")
        vocab_layout = QVBoxLayout(vocab_group)
        vocab_layout.setContentsMargins(4, 12, 4, 4)
        vocab_layout.setSpacing(0)
        self.vocab_list = QListWidget()
        self.vocab_list.setStyleSheet(f"""
            QListWidget {{
                background: {theme.INPUT_BG};
                border: none;
                color: {theme.TEXT};
                font-family: "{theme.FAMILY_MONO}";
                font-size: {theme.BASE_PT}pt;
            }}
            QListWidget::item {{
                padding: 3px 8px;
            }}
            QListWidget::item:hover     {{ background: {theme.BTN_HOVER}; }}
            QListWidget::item:selected  {{ background: {theme.ACCENT}; }}
        """)
        self.vocab_list.itemClicked.connect(self._on_vocab_item_clicked)
        self.vocab_list.setVerticalScrollBar(JumpScrollBar())
        vocab_layout.addWidget(self.vocab_list)
        layout.addWidget(vocab_group, 1)  # stretch=1 fills remaining space
        return panel

    def _populate_movies(self) -> None:
        try:
            from data.metadata import get_metadata
            rows = get_metadata(self.project_path, media_type="movies")
            sorted_rows = sorted(rows, key=lambda r: (r.get("title") or "").lower())
            for row in sorted_rows:
                title = row.get("title", "")
                year  = row.get("year", "")
                label = f"{title} ({year})" if year else title
                if label:
                    self.movie_combo.addItem(label, userData=row.get("filename", ""))
        except Exception as exc:
            self.status.showMessage(f"Warning: could not load movie list — {exc}")

    # ------------------------------------------------------------------
    # Best-mode helpers

    def _update_best_button(self) -> None:
        has_single_movie = self.movie_combo.currentData() is not None
        is_video_mode    = self.mode_combo.currentText() == "video"
        can_use_best     = has_single_movie and not is_video_mode
        self.best_btn.setEnabled(can_use_best)
        if not can_use_best:
            self.best_btn.setChecked(False)
            self.best_mode = False
        self._update_search_button()

    def _update_search_button(self) -> None:
        has_query = bool(self.query_input.text().strip())
        self.search_btn.setEnabled(has_query or self.best_mode)

    def _on_best_toggle(self) -> None:
        self.best_mode = self.best_btn.isChecked()
        self._update_search_button()

    # ------------------------------------------------------------------
    # Search flow

    def _on_search(self) -> None:
        query    = self.query_input.text().strip()
        filename = self.movie_combo.currentData()

        if self.best_mode and not query and filename:
            self._render_best_only(filename)
            return

        if not query:
            self.status.showMessage("Enter a search query first.")
            return

        # Stop any in-flight worker
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(3000)

        self.canvas.clear()
        self._current_results = []
        self.export_btn.setEnabled(False)

        scope_data      = filename  # already resolved above
        scope           = scope_data if scope_data else None
        field_text      = self.field_combo.currentText()
        field           = None if field_text == "--all" else field_text
        limit_text      = self.limit_combo.currentText()
        limit           = None if limit_text == "all" else int(limit_text)
        limit_per_movie = self.limit_per_movie_cb.isChecked()

        self.search_btn.setEnabled(False)
        self.status.showMessage(f"Searching for '{query}'…")
        self._progress.setRange(0, 0)  # indeterminate while searching
        self._progress.setValue(0)

        self._best_lookup = {}
        self._query_best_active = self.best_mode and bool(query)
        if self.best_mode and filename and not self._query_best_active:
            from services.frame_match import load_best_frame_lookup
            self._best_lookup = load_best_frame_lookup(
                self.project_path, filename, "movies"
            )

        import prefs as _prefs
        model_name = _prefs.get("model_frame_match", "clip-vit-base-patch32")

        self._worker = SearchWorker(
            query          = query,
            scope          = scope,
            field          = field,
            limit          = limit,
            limit_per_movie= limit_per_movie,
            project_path   = self.project_path,
            best_mode      = self._query_best_active,
            model_name     = model_name,
        )
        self._worker.tile_ready.connect(self._on_tile_ready)
        self._worker.finished_signal.connect(self._on_search_done)
        self._worker.error.connect(self._on_search_error)
        self._worker.start()

    def _on_tile_ready(self, result: dict, pixmap) -> None:
        if self.best_mode and not self._query_best_active:
            shot_id = result.get("shot_id")
            bf      = self._best_lookup.get(shot_id)
            if bf:
                img_path = best_frame_path(
                    self.project_path,
                    "movies",
                    result.get("movie_id", ""),
                    shot_id,
                )
                pixmap = QPixmap(str(img_path)) if img_path.exists() else None
                result = dict(result)  # avoid mutating the original
                result["frame"] = bf.get("frame")
            else:
                result = dict(result)
                result["frame"] = None
        self._current_results.append(result)
        self.canvas.add_tile(result, pixmap)
        self.status.showMessage(f"Loading… {self.canvas.tile_count} tile(s)")

    def _on_search_done(self, count: int) -> None:
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self.search_btn.setEnabled(True)
        self.export_btn.setEnabled(count > 0)
        if count == 0:
            self.status.showMessage("No results found.")
        else:
            self.status.showMessage(
                f"{count} result(s)  —  Ctrl + scroll to zoom,  scroll to pan"
            )
            # In video mode, automatically start video generation once results
            # are loaded — the user doesn't need to click "Save Video" separately.
            if self.mode_combo.currentText() == "video":
                self._on_save_video()

    def _on_search_error(self, message: str) -> None:
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self.search_btn.setEnabled(True)
        preview = message.splitlines()[0][:120]
        self.status.showMessage(f"Error: {preview}")

    # ------------------------------------------------------------------
    # Best-only render (no query)

    def _render_best_only(self, filename: str) -> None:
        from services.frame_match import load_best_frame_lookup

        # Stop any in-flight best worker
        if self._best_worker and self._best_worker.isRunning():
            self._best_worker.cancel()
            self._best_worker.wait(3000)

        # Show immediate feedback — indeterminate bar while JSON loads
        self._progress.setRange(0, 0)
        self._progress.setValue(0)
        self.search_btn.setEnabled(False)
        self.best_btn.setEnabled(False)
        self.status.showMessage("Loading best frames…")

        self.canvas.clear()
        self._current_results = []
        self.export_btn.setEnabled(False)

        lookup = load_best_frame_lookup(self.project_path, filename, "movies")
        if not lookup:
            self._progress.setRange(0, 1)
            self._progress.setValue(0)
            self.search_btn.setEnabled(True)
            self.best_btn.setEnabled(True)
            self.status.showMessage("No best frames found.")
            return

        self._total_best = len(lookup)
        self.status.showMessage(f"Loading {self._total_best} best frame(s)…")

        self._best_worker = BestOnlyWorker(lookup, filename, self.project_path)
        self._best_worker.tile_ready.connect(self._on_best_tile_ready)
        self._best_worker.finished_signal.connect(self._on_best_done)
        self._best_worker.start()

    def _on_best_tile_ready(self, result: dict, path_str) -> None:
        pixmap = QPixmap(path_str) if path_str else None
        self._current_results.append(result)
        self.canvas.add_tile(result, pixmap)
        n = self.canvas.tile_count
        # Switch from indeterminate to determinate on first tile so the chunk
        # is always visible (starting at 1/total rather than 0/total).
        if n == 1 and hasattr(self, "_total_best") and self._total_best > 0:
            self._progress.setRange(0, self._total_best)
            self._progress.setValue(1)
        else:
            self._progress.setValue(n)
        self.status.showMessage(f"Loading… {n} tile(s)")

    def _on_best_done(self, count: int) -> None:
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self.search_btn.setEnabled(True)
        self.best_btn.setEnabled(True)
        self.export_btn.setEnabled(count > 0)
        if count == 0:
            self.status.showMessage("No best frames found.")
        else:
            self.status.showMessage(
                f"{count} best frame(s)  —  Ctrl + scroll to zoom,  scroll to pan"
            )

    # ------------------------------------------------------------------
    # Mode switching

    def _on_mode_changed(self, mode: str) -> None:
        is_video = (mode == "video")
        self._fps_row.setVisible(is_video)
        self._dur_row.setVisible(is_video)
        if is_video:
            self.export_btn.setText("Save Video")
            self.export_btn.setToolTip(self._export_btn_video_tip)
            # Best mode is incompatible with video mosaic
            self.best_btn.setChecked(False)
            self.best_btn.setEnabled(False)
            self.best_mode = False
        else:
            self.export_btn.setText("Export")
            self.export_btn.setToolTip(self._export_btn_image_tip)
            # Re-evaluate whether Best can be enabled
            self._update_best_button()

    # ------------------------------------------------------------------
    # Export

    def _on_export(self) -> None:
        if self.mode_combo.currentText() == "video":
            self._on_save_video()
            return

        if not self._current_results:
            self.status.showMessage("No results to export — run a search first.")
            return

        # Prevent double-click while export is in flight
        if self._export_worker and self._export_worker.isRunning():
            return

        query      = self.query_input.text().strip()
        field_text = self.field_combo.currentText()
        field      = None if field_text == "--all" else field_text

        self.export_btn.setEnabled(False)
        self.search_btn.setEnabled(False)
        self.status.showMessage(
            f"Exporting {len(self._current_results)} frame(s) for '{query}'…"
        )

        self._export_worker = ExportWorker(
            results      = list(self._current_results),
            project_path = self.project_path,
            query        = query,
            field        = field,
        )
        self._export_worker.finished_signal.connect(self._on_export_done)
        self._export_worker.error.connect(self._on_export_error)
        self._export_worker.start()

    def _on_export_done(self, out_dir: str) -> None:
        import subprocess
        self.export_btn.setEnabled(True)
        self.search_btn.setEnabled(True)
        self.status.showMessage(f"Exported → {out_dir}")
        subprocess.Popen(["xdg-open", out_dir])

    def _on_export_error(self, message: str) -> None:
        self.export_btn.setEnabled(True)
        self.search_btn.setEnabled(True)
        preview = message.splitlines()[0][:120]
        self.status.showMessage(f"Export error: {preview}")

    def _on_save_video(self) -> None:
        """Generate a looping video mosaic (.mp4) from the current results."""
        if not self._current_results:
            self.status.showMessage("No results to generate video from — run a search first.")
            return

        if self._video_worker and self._video_worker.isRunning():
            return

        query = self.query_input.text().strip()
        limit_text = self.limit_combo.currentText()
        limit = 50 if limit_text == "all" else int(limit_text)
        fps      = self.fps_spin.value()
        duration = self.dur_spin.value()

        self.export_btn.setEnabled(False)
        self.search_btn.setEnabled(False)
        self.status.showMessage(
            f"Generating video mosaic: {len(self._current_results)} tile(s) …"
        )
        self._progress.setRange(0, 0)

        self._video_worker = VideoMosaicWorker(
            results      = list(self._current_results),
            project_path = self.project_path,
            query        = query,
            fps          = fps,
            duration     = duration,
            layout       = "landscape",
            limit        = limit,
        )
        self._video_worker.finished_signal.connect(self._on_video_done)
        self._video_worker.error.connect(self._on_video_error)
        self._video_worker.start()

    def _on_video_done(self, out_path: str) -> None:
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self.export_btn.setEnabled(True)
        self.search_btn.setEnabled(True)
        self.status.showMessage(f"✓ Saved: {out_path}  — opening in looping player…")
        _open_video_looping(out_path)

    def _on_video_error(self, message: str) -> None:
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self.export_btn.setEnabled(True)
        self.search_btn.setEnabled(True)
        preview = message.splitlines()[0][:120]
        self.status.showMessage(f"Video error: {preview}")

    # ------------------------------------------------------------------
    # Vocabulary panel

    def _on_field_changed(self) -> None:
        field = self.field_combo.currentText()
        self.vocab_list.clear()
        if not field or field in ("--all", "text", "description"):
            return

        # Cancel any in-flight vocab worker
        if self._vocab_worker and self._vocab_worker.isRunning():
            self._vocab_worker.wait(1000)

        scope_data = self.movie_combo.currentData()  # filename stored as userData
        scope      = scope_data if scope_data else None

        loading = QListWidgetItem("Loading…")
        loading.setFlags(loading.flags() & ~Qt.ItemIsEnabled)
        self.vocab_list.addItem(loading)

        self._vocab_worker = VocabularyWorker(
            field        = field,
            scope        = scope,
            project_path = self.project_path,
        )
        self._vocab_worker.items_ready.connect(self._on_vocab_items)
        self._vocab_worker.error.connect(self._on_vocab_error)
        self._vocab_worker.start()

    def _on_vocab_items(self, items: list) -> None:
        self.vocab_list.clear()
        for entry in items:
            value = entry["value"]
            count = entry["count"]
            item  = QListWidgetItem(f"{value}  ×{count}")
            item.setData(Qt.UserRole, value)
            self.vocab_list.addItem(item)

    def _on_vocab_error(self, message: str) -> None:
        self.vocab_list.clear()
        err = QListWidgetItem("Error loading vocabulary")
        err.setFlags(err.flags() & ~Qt.ItemIsEnabled)
        self.vocab_list.addItem(err)

    def _on_vocab_item_clicked(self, item: QListWidgetItem) -> None:
        value = item.data(Qt.UserRole)
        if value:
            self.query_input.setText(value)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Q, Qt.Key_W) and event.modifiers() & Qt.ControlModifier:
            self.close()
            return
        super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# Public launcher
# ---------------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        save_window_geometry(self, "window_mosaic")
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Public launcher
# ---------------------------------------------------------------------------

def run_visualizer(project_path: str) -> None:
    """Create the QApplication (if needed) and launch the visualizer window."""
    app = QApplication.instance() or QApplication(sys.argv)
    theme.apply_theme(app)
    win = MosaicVisualizer(project_path)
    win.show()
    sys.exit(app.exec_())
