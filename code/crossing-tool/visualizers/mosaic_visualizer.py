#!/usr/bin/env python3
"""Mosaic Visualizer — live, interactive visual browser for shot search results.

Launched via:
    crossing generate mosaic visualizer

The window has:
  LEFT  — scrollable, zoomable mosaic canvas (image tiles)
  RIGHT — control panel (movie scope, field, query, options)

Zoom: Ctrl + scroll wheel
Pan:  scroll wheel / scrollbars

Keyboard:
  Home          — previous title in list
  End           — next title in list
  PgUp / PgDn   — previous / next annotation field
  Escape / Ctrl+Q / Ctrl+W — close
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
from tool.shortcuts import KEY_PREV_TITLE, KEY_NEXT_TITLE, KEY_PREV_ITEM, KEY_NEXT_ITEM

# Fix Qt plugin conflict with OpenCV — import PyQt5 before cv2
from PyQt5.QtCore import Qt, QEvent, QRect, QThread, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGraphicsOpacityEffect,
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
    QShortcut,
    QSizePolicy,
    QSpinBox,
    QSplitter,  # noqa: F401 (unused after refactor, kept for import compatibility)
    QVBoxLayout,
    QWidget,
)
from PyQt5.QtGui import QColor, QFont, QFontDatabase, QImage, QKeySequence, QPainter, QPixmap, QWheelEvent

import cv2
import numpy as np

if "QT_QPA_PLATFORM_PLUGIN_PATH" in os.environ:
    del os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"]

from typing import Optional


# ---------------------------------------------------------------------------
# Status label (replaces QStatusBar — lives inside the right control panel)
# ---------------------------------------------------------------------------

class _StatusLabel(QLabel):
    """A QLabel that exposes a QStatusBar-compatible showMessage() interface.

    ``showMessage(text, timeout=0)`` sets the label text and, when *timeout*
    is non-zero, clears it automatically after that many milliseconds.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWordWrap(True)
        self.setStyleSheet(
            f"QLabel {{ color: {theme.TEXT_DIM}; padding: 4px 0 2px 0; "
            f"font-size: {theme.BASE_PT - 1}pt; }}"
        )
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.clear)

    def showMessage(self, text: str, timeout: int = 0) -> None:  # noqa: N802
        self._timer.stop()
        self.setText(text)
        if timeout > 0:
            self._timer.start(timeout)

    def clearMessage(self) -> None:  # noqa: N802
        self._timer.stop()
        self.clear()

    def set_busy_style(self, busy: bool) -> None:  # noqa: N802
        """Toggle a fuchsia background to indicate an in-progress export."""
        if busy:
            self.setStyleSheet(
                f"QLabel {{ color: {theme.TEXT}; background-color: {theme.ACCENT}; "
                f"padding: 4px 4px 2px 4px; font-size: {theme.BASE_PT - 1}pt; }}"
            )
        else:
            self.setStyleSheet(
                f"QLabel {{ color: {theme.TEXT_DIM}; padding: 4px 0 2px 0; "
                f"font-size: {theme.BASE_PT - 1}pt; }}"
            )


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
# Scene label frames (title card + scene-number cards in the Scenes view)
# ---------------------------------------------------------------------------

_LABEL_W  = 640
_LABEL_H  = 360
_LABEL_BG = (128, 128, 128)   # 50 % grey
_LABEL_FG = (255, 255, 255)   # white
_FONT_DIR = (
    Path(__file__).parent.parent
    / "styles" / "fonts" / "libre_clarendon" / "fonts"
)


def _make_label_pixmap(
    text: str,
    font_filename: str,
    font_size: int,
    *,
    width: int = _LABEL_W,
    height: int = _LABEL_H,
) -> Optional[QPixmap]:
    """Render centred white *text* on a 50 % grey canvas and return a QPixmap.

    Returns ``None`` if PIL or the requested font is unavailable.
    """
    try:
        from PIL import Image as _PILImage, ImageDraw as _IDraw, ImageFont as _IFont
        img  = _PILImage.new("RGB", (width, height), _LABEL_BG)
        draw = _IDraw.Draw(img)
        font = _IFont.truetype(str(_FONT_DIR / font_filename), font_size)
        # Truncate text that is wider than the canvas (leaves 20 px margin each side)
        while len(text) > 1:
            bbox = draw.textbbox((0, 0), text, font=font)
            if (bbox[2] - bbox[0]) <= (width - 40):
                break
            text = text[:-1].rstrip() + "…"
        bbox = draw.textbbox((0, 0), text, font=font)
        tx   = (width  - (bbox[2] - bbox[0])) // 2 - bbox[0]
        ty   = (height - (bbox[3] - bbox[1])) // 2 - bbox[1]
        draw.text((tx, ty), text, font=font, fill=_LABEL_FG)
        arr = np.array(img)
        h, w, ch = arr.shape
        qimg = QImage(arr.data, w, h, ch * w, QImage.Format_RGB888)
        return QPixmap.fromImage(qimg)
    except Exception:
        return None


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


def _renumber_scenes(shots: list) -> None:
    """Re-assign sequential integer Scene labels (1, 2, 3…) in-place.

    Walks the shot list in order and increments the counter each time the
    raw Scene value changes, so the final values are always 1, 2, 3… even
    after merge / split operations that leave gaps or placeholders.
    """
    if not shots:
        return
    counter  = 1
    prev_raw = shots[0].get("Scene", "")
    shots[0]["Scene"] = str(counter)
    for shot in shots[1:]:
        raw = shot.get("Scene", "")
        if raw != prev_raw:
            counter  += 1
            prev_raw  = raw
        shot["Scene"] = str(counter)


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

            # When "Limit per movie" is checked the number applies per movie
            # (limit_per_item) with no overall cap; otherwise it caps the total.
            if self.limit_per_movie:
                total_limit = None
                limit_pi    = self.limit
            else:
                total_limit = self.limit
                limit_pi    = None

            result  = search_shots(
                query          = self.query,
                scopes         = scopes,
                field          = field,
                limit          = total_limit,
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

_BTN_DIAM = 34   # diameter of each circular hover-overlay button


class _HoverButton(QPushButton):
    """Small circular dark button used in TileWidget hover overlays."""

    def __init__(self, symbol: str, parent=None):
        super().__init__(symbol, parent)
        r = _BTN_DIAM // 2
        self.setFixedSize(_BTN_DIAM, _BTN_DIAM)
        self.setStyleSheet(
            f"QPushButton {{"
            f"  background-color: rgba(30,30,30,190);"
            f"  color: white;"
            f"  border: none;"
            f"  border-radius: {r}px;"
            f"  font-size: 15px;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background-color: rgba(70,70,70,220);"
            f"}}"
            f"QPushButton:pressed {{"
            f"  background-color: rgba(100,100,100,220);"
            f"}}"
            f"QPushButton:disabled {{"
            f"  color: rgba(128,128,128,90);"
            f"  background-color: rgba(30,30,30,80);"
            f"}}"
        )


class TileWidget(QFrame):
    """A single mosaic tile: scaled video frame + short caption + hover tooltip.

    Overlay buttons appear on mouse-hover (WA_Hover keeps them visible while
    the cursor moves between the widget and a child button):

        Frame tiles  →  ⊕ add scene  ▶ open shotlist visualiser  🛈 toggle ignore
        Scene cards  →  ⓧ remove scene boundary  (only on non-first cards)
    """

    _PLACEHOLDER_BG = theme.CANVAS_BG

    # Retained for any existing listeners; no longer emitted internally.
    clicked = pyqtSignal(dict)

    # Per-button action signals forwarded through MosaicCanvas to MosaicWindow
    btn_open_visualizer = pyqtSignal(dict)
    btn_toggle_ignore   = pyqtSignal(dict)
    btn_add_scene       = pyqtSignal(dict)
    btn_remove_scene    = pyqtSignal(dict)

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
        if not result.get("is_label"):
            self.setCursor(Qt.PointingHandCursor)

        # WA_Hover fires HoverEnter/HoverLeave for the whole widget subtree,
        # so the overlay stays visible while the cursor is over a child button.
        self.setAttribute(Qt.WA_Hover, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self._img_label = QLabel()
        self._img_label.setAlignment(Qt.AlignCenter)
        self._img_label.setStyleSheet(f"background: {self._PLACEHOLDER_BG}; border: none;")
        layout.addWidget(self._img_label)

        caption_text = result.get("caption")
        if caption_text is None:
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

        # Transparent overlay parented to the image label; sized in _render()
        self._overlay = QWidget(self._img_label)
        self._overlay.setAttribute(Qt.WA_TranslucentBackground)
        self._overlay.hide()
        self._overlay_btns: list[QPushButton] = []
        self._create_overlay_buttons()

        # Opacity effect for ignored shots (applied to image only)
        self._opacity_effect: Optional[QGraphicsOpacityEffect] = None
        if not result.get("is_label"):
            if str(result.get("Ignore", "")).strip().lower() in ("true", "1", "yes"):
                self._apply_ignored(True)

        self._render(tile_size)

    # ------------------------------------------------------------------

    def _create_overlay_buttons(self) -> None:
        """Populate self._overlay_btns based on tile type."""
        r = self.result
        is_label       = r.get("is_label", False)
        is_title       = r.get("is_title", False)
        is_scene_card  = is_label and not is_title
        is_frame       = not is_label
        is_first_scene = r.get("is_first_scene", True)
        is_first_shot  = r.get("is_first_shot", False)

        if is_frame:
            btn_add = _HoverButton("\u2295", self._overlay)   # ⊕  add scene here
            btn_add.setEnabled(not is_first_shot)
            btn_add.clicked.connect(lambda: self.btn_add_scene.emit(self.result))

            btn_vis = _HoverButton("\u25b6", self._overlay)   # ▶  open visualiser
            btn_vis.clicked.connect(lambda: self.btn_open_visualizer.emit(self.result))

            btn_ign = _HoverButton("\U0001f6c8", self._overlay)  # 🛈  toggle ignore
            btn_ign.clicked.connect(lambda: self.btn_toggle_ignore.emit(self.result))

            self._overlay_btns = [btn_add, btn_vis, btn_ign]

        elif is_scene_card and not is_first_scene:
            btn_rem = _HoverButton("\u24e7", self._overlay)   # ⓧ  remove boundary
            btn_rem.clicked.connect(lambda: self.btn_remove_scene.emit(self.result))
            self._overlay_btns = [btn_rem]

    def _render_overlay(self, img_w: int, img_h: int) -> None:
        """Resize the transparent overlay to fill the image and place buttons."""
        self._overlay.setGeometry(0, 0, img_w, img_h)
        if not self._overlay_btns:
            return
        d       = _BTN_DIAM
        margin  = 6
        spacing = 8
        if len(self._overlay_btns) == 3:
            total_w = 3 * d + 2 * spacing
            x = (img_w - total_w) // 2
            y = img_h - d - margin
            for btn in self._overlay_btns:
                btn.move(x, y)
                x += d + spacing
        elif len(self._overlay_btns) == 1:
            self._overlay_btns[0].move(
                (img_w - d) // 2, img_h - d - margin
            )

    def _apply_ignored(self, ignored: bool) -> None:
        """Apply or remove the 50 % opacity effect on the image label."""
        if ignored:
            if self._opacity_effect is None:
                self._opacity_effect = QGraphicsOpacityEffect(self._img_label)
                self._img_label.setGraphicsEffect(self._opacity_effect)
            self._opacity_effect.setOpacity(0.5)
        elif self._opacity_effect is not None:
            self._opacity_effect.setOpacity(1.0)

    def set_ignored(self, ignored: bool) -> None:
        """Externally update the ignored visual state (called after shotlist save)."""
        self._apply_ignored(ignored)

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

        self._render_overlay(img_w, img_h)

    def resize_tile(self, tile_size: int) -> None:
        """Re-render at a new tile_size (called when user zooms)."""
        self._render(tile_size)

    def event(self, ev: QEvent) -> bool:
        if ev.type() == QEvent.HoverEnter:
            self._overlay.show()
            self._overlay.raise_()
        elif ev.type() == QEvent.HoverLeave:
            self._overlay.hide()
        return super().event(ev)

    def mousePressEvent(self, event) -> None:
        # Click-to-open replaced by the ▶ overlay button.
        super().mousePressEvent(event)


# ---------------------------------------------------------------------------
# Mosaic canvas — scrollable grid of tiles
# ---------------------------------------------------------------------------

class MosaicCanvas(QScrollArea):
    """Scrollable viewport containing a grid of TileWidget tiles.

    Ctrl + scroll wheel  → zoom in / out
    Plain scroll wheel   → vertical scroll (default)
    """

    tile_clicked        = pyqtSignal(dict)   # retained for compatibility
    btn_open_visualizer = pyqtSignal(dict)
    btn_toggle_ignore   = pyqtSignal(dict)
    btn_add_scene       = pyqtSignal(dict)
    btn_remove_scene    = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setStyleSheet(f"QScrollArea {{ border: none; background: {theme.CANVAS_BG}; }}")
        self.setVerticalScrollBar(JumpScrollBar())

        self._tile_size: int = DEFAULT_TILE_SIZE
        self._tiles: list[TileWidget] = []
        self._row_breaks: set[int] = set()

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
        self._row_breaks.clear()
        self._container.setFixedSize(max(self.viewport().width(), 1), 1)

    def truncate_from_index(self, idx: int) -> None:
        """Delete all tiles from *idx* onwards (inclusive) and re-flow layout."""
        for tile in self._tiles[idx:]:
            tile.deleteLater()
        self._tiles = self._tiles[:idx]
        self._row_breaks = {rb for rb in self._row_breaks if rb < idx}
        self._do_flow_layout()

    def insert_tile(self, idx: int, tile: "TileWidget") -> None:
        """Insert *tile* at position *idx* (no forced row-break), then re-flow."""
        tile.btn_open_visualizer.connect(self.btn_open_visualizer)
        tile.btn_toggle_ignore.connect(self.btn_toggle_ignore)
        tile.btn_add_scene.connect(self.btn_add_scene)
        tile.btn_remove_scene.connect(self.btn_remove_scene)
        tile.show()
        self._tiles.insert(idx, tile)
        # Shift any existing explicit row breaks that sit at or after the insertion
        self._row_breaks = {rb + 1 if rb >= idx else rb for rb in self._row_breaks}
        self._do_flow_layout()

    def remove_tile(self, idx: int) -> None:
        """Remove the tile at *idx*, delete its widget, then re-flow."""
        tile = self._tiles.pop(idx)
        tile.deleteLater()
        self._row_breaks.discard(idx)
        # Shift row breaks above idx down by one
        self._row_breaks = {rb - 1 if rb > idx else rb for rb in self._row_breaks}
        self._do_flow_layout()

    def add_row_break(self) -> None:
        """Force the next tile to start on a new row regardless of available width."""
        self._row_breaks.add(len(self._tiles))

    def add_tile(self, result: dict, pixmap) -> None:
        tile = TileWidget(result, pixmap, self._tile_size, parent=self._container)
        tile.btn_open_visualizer.connect(self.btn_open_visualizer)
        tile.btn_toggle_ignore.connect(self.btn_toggle_ignore)
        tile.btn_add_scene.connect(self.btn_add_scene)
        tile.btn_remove_scene.connect(self.btn_remove_scene)
        tile.show()
        self._tiles.append(tile)
        self._do_flow_layout()

    def tile_for_shot(self, shot_id: str) -> "TileWidget | None":
        """Return the TileWidget for *shot_id*, or None if not found."""
        for tile in self._tiles:
            if tile.result.get("shot_id") == shot_id:
                return tile
        return None

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

        for i, tile in enumerate(self._tiles):
            tw = tile.width()
            th = tile.height()

            if i in self._row_breaks and i > 0:
                # Explicit row break (scene boundary)
                x      = margin
                y     += row_h + spacing
                row_h  = 0
            elif x > margin and x + tw > vp_w - margin:
                # Normal flow wrap
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
# Background worker: all-shots browse (no query)
# ---------------------------------------------------------------------------

class AllShotsWorker(QThread):
    """Loads the first frame of every shot in one film in a background thread.

    Used when the user presses Search with an empty query and a single movie
    selected.  Emits a pixmap directly (same pattern as SearchWorker) so the
    main thread can add each tile as it arrives.

    Signals
    -------
    tile_ready(result_dict, pixmap_or_None)
        Emitted for every shot with the extracted QPixmap (or None on failure).
    finished_signal(total_count)
        Emitted when all shots have been processed.
    error(message)
        Emitted on unexpected errors.
    """

    tile_ready      = pyqtSignal(dict, object)
    finished_signal = pyqtSignal(int)
    error           = pyqtSignal(str)

    def __init__(self, filename: str, project_path: str, parent=None):
        super().__init__(parent)
        self.filename     = filename
        self.project_path = project_path
        self._cancelled   = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            from data.shotlist import read_shotlist

            shots = read_shotlist(self.project_path, self.filename, "movies")
            movie_id   = Path(self.filename).stem
            video_path = _find_video_path(self.project_path, movie_id)

            count = 0
            for shot in shots:
                if self._cancelled:
                    break

                shot_id     = shot.get("shot_id", "")
                start_frame = shot.get("start_frame")
                end_frame   = shot.get("end_frame")

                try:
                    frame_index = int(start_frame) if start_frame is not None else 0
                except (TypeError, ValueError):
                    frame_index = 0

                pixmap = None
                if video_path is not None:
                    pixmap = _extract_frame_pixmap(video_path, frame_index)

                result = {
                    "filename":    self.filename,
                    "movie_title": self.filename,
                    "movie_id":    movie_id,
                    "shot_id":     shot_id,
                    "start_frame": frame_index,
                    "end_frame":   end_frame,
                    "frame":       frame_index,
                    "matched_fields": [],
                    "matched_text":   "",
                    "score":          0.0,
                }
                self.tile_ready.emit(result, pixmap)
                count += 1

            self.finished_signal.emit(count)

        except Exception as exc:
            import traceback
            self.error.emit(f"{exc}\n{traceback.format_exc()}")


# ---------------------------------------------------------------------------
# Background worker: scene-grouped browse
# ---------------------------------------------------------------------------

class ScenesWorker(QThread):
    """Loads shots for one film grouped by scene, in a background thread.

    Emits each shot as a tile with scene metadata so the main thread can
    insert row breaks at scene boundaries.  When *best_lookup* is provided
    the precomputed best-frame PNG is used instead of the first raw frame.

    Signals
    -------
    tile_ready(result_dict, pixmap_or_None)
    finished_signal(total_count)
    error(message)
    """

    tile_ready      = pyqtSignal(dict, object)
    finished_signal = pyqtSignal(int)
    error           = pyqtSignal(str)

    def __init__(
        self,
        filename: str,
        project_path: str,
        best_lookup: "dict | None" = None,
        resume_from_shot_id: "str | None" = None,
        initial_scene: "str | None" = None,
        parent=None,
    ):
        super().__init__(parent)
        self.filename            = filename
        self.project_path        = project_path
        self.best_lookup         = best_lookup or {}
        self.resume_from_shot_id = resume_from_shot_id
        self.initial_scene       = initial_scene
        self._cancelled          = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            from data.shotlist import read_shotlist
            from data.metadata import get_metadata

            shots      = read_shotlist(self.project_path, self.filename, "movies")
            movie_id   = Path(self.filename).stem
            video_path = _find_video_path(self.project_path, movie_id)

            # Read actual video dimensions so label tiles share the movie's ratio
            vid_w, vid_h = 320, 180  # fallback 16:9
            if video_path is not None:
                _cap = cv2.VideoCapture(str(video_path))
                if _cap.isOpened():
                    _fw = int(_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    _fh = int(_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    if _fw > 0 and _fh > 0:
                        sar = _get_sar(str(video_path))
                        vid_w = int(round(_fw * sar[0] / sar[1]))
                        vid_h = _fh
                _cap.release()

            # Clean title + year from stored metadata; fall back to filename parsing
            meta_list = get_metadata(self.project_path, self.filename, "movies")
            if meta_list:
                _meta = meta_list[0]
            else:
                import re as _re
                _stem  = Path(self.filename).stem
                _clean = _re.sub(r'\s*\{tmdb-\d+\}|\s*\(\d{4}\)', '', _stem).strip().rstrip('-').strip()
                _meta  = {
                    "title": _clean.replace('-', ' ').replace('_', ' ').strip().title(),
                    "year":  None,
                }
                _yr = _re.search(r'\((\d{4})\)', _stem)
                if _yr:
                    _meta["year"] = int(_yr.group(1))

            display_title = str(_meta.get("title") or movie_id.replace("-", " ").replace("_", " "))
            movie_year    = str(_meta.get("year") or "")

            # Title card — GUI thread renders text; worker just carries the data
            title_result = {
                "filename":       self.filename,
                "movie_id":       movie_id,
                "is_label":       True,
                "is_title":       True,
                "label_text":     display_title,
                "movie_year":     movie_year,
                "vid_w":          vid_w,
                "vid_h":          vid_h,
                "caption":        "",
                "matched_fields": [],
                "matched_text":   "",
                "score":          0.0,
            }
            if self.resume_from_shot_id is None:
                self.tile_ready.emit(title_result, None)

            if self.resume_from_shot_id is not None:
                # Partial refresh: warm-start the scene tracker at the resume point
                current_scene      = self.initial_scene
                scene_card_count   = 1   # all new cards are non-first
                first_shot_emitted = True
                _skipping          = True
            else:
                current_scene      = None
                scene_card_count   = 0
                first_shot_emitted = False
                _skipping          = False
            count = 0

            for shot in shots:
                if self._cancelled:
                    break

                scene   = str(shot.get("Scene") or "").strip()
                shot_id = shot.get("shot_id", "")

                # When resuming from a mid-movie point, skip shots until the
                # resume target is reached
                if _skipping:
                    if shot_id == self.resume_from_shot_id:
                        _skipping = False
                    else:
                        continue

                start_frame = shot.get("start_frame")
                end_frame   = shot.get("end_frame")
                try:
                    frame_index = int(start_frame) if start_frame is not None else 0
                except (TypeError, ValueError):
                    frame_index = 0

                # Scene-number card — emitted whenever the scene changes
                if scene != current_scene:
                    current_scene = scene
                    scene_result = {
                        "filename":       self.filename,
                        "movie_id":       movie_id,
                        "scene":          scene,
                        "is_label":       True,
                        "is_first_scene": scene_card_count == 0,
                        "label_text":     scene or "?",
                        "vid_w":          vid_w,
                        "vid_h":          vid_h,
                        "caption":        "",
                        "matched_fields": [],
                        "matched_text":   "",
                        "score":          0.0,
                    }
                    self.tile_ready.emit(scene_result, None)
                    scene_card_count += 1

                # Prefer precomputed best-frame PNG when available
                pixmap: Optional[QPixmap] = None
                if self.best_lookup and shot_id in self.best_lookup:
                    img_path = best_frame_path(
                        self.project_path, "movies", self.filename, shot_id
                    )
                    if img_path.exists():
                        pixmap = QPixmap(str(img_path))
                        # Apply SAR correction — best-frame PNGs are stored at
                        # coded (raw pixel) resolution, not display resolution.
                        if video_path is not None and pixmap is not None and not pixmap.isNull():
                            sar = _get_sar(str(video_path))
                            if sar != (1, 1):
                                new_w = int(round(pixmap.width() * sar[0] / sar[1]))
                                pixmap = pixmap.scaled(
                                    new_w, pixmap.height(),
                                    Qt.IgnoreAspectRatio, Qt.SmoothTransformation,
                                )
                if pixmap is None and video_path is not None:
                    pixmap = _extract_frame_pixmap(video_path, frame_index)

                result = {
                    "filename":       self.filename,
                    "movie_title":    self.filename,
                    "movie_id":       movie_id,
                    "shot_id":        shot_id,
                    "start_frame":    frame_index,
                    "end_frame":      end_frame,
                    "frame":          frame_index,
                    "scene":          scene,
                    "Ignore":         shot.get("Ignore", ""),
                    "caption":        f"f{frame_index:06d}",
                    "is_label":       False,
                    "is_first_shot":  not first_shot_emitted,
                    "matched_fields": [],
                    "matched_text":   "",
                    "score":          0.0,
                }
                self.tile_ready.emit(result, pixmap)
                count += 1
                first_shot_emitted = True

            self.finished_signal.emit(count)

        except Exception as exc:
            import traceback
            traceback.print_exc()  # full traceback visible in the terminal
            self.error.emit(f"{exc}\n{traceback.format_exc()}")


# ---------------------------------------------------------------------------
# Background worker: PDF contact-sheet export
# ---------------------------------------------------------------------------

class PdfExportWorker(QThread):
    """Renders current results as a mosaic contact sheet and saves as PDF.

    Signals
    -------
    finished_signal(output_path_str)
    progress(current, total)
    error(message)
    """

    finished_signal = pyqtSignal(str)
    progress        = pyqtSignal(int, int)
    error           = pyqtSignal(str)

    def __init__(self, results: list, project_path: str, query: str,
                 filename: str = "", subfolder: str = "", parent=None):
        super().__init__(parent)
        self.results      = results
        self.project_path = project_path
        self.query        = query
        self.filename     = filename
        self.subfolder    = subfolder

    def run(self) -> None:
        try:
            import datetime
            import re as _re
            # Ensure the JPEG encoder is registered before Pillow's PDF plugin
            # tries to call Image.SAVE["JPEG"] — it is loaded lazily and may
            # not be present if no JPEG has been opened/saved in this process yet.
            import PIL.JpegImagePlugin  # noqa: F401
            from generators.mosaic import results_to_mosaic_items, render_mosaic

            items = results_to_mosaic_items(self.results, self.project_path)

            if not items:
                self.error.emit("No frames available to export.")
                return

            stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
            if self.filename:
                base = _re.sub(r'[/\\:*?"<>|]', '_', Path(self.filename).stem)
            elif self.query:
                base = _re.sub(r"[^\w\-\s]", "_", self.query)[:60].strip()
            else:
                base = "mosaic"
            out_path = (
                Path(self.project_path) / "output" / "mosaics"
                / (self.subfolder or "searches")
                / f"{base} [{stamp}].pdf"
            )
            render_mosaic(items, out_path, layout="landscape",
                          progress_cb=lambda c, t: self.progress.emit(c, t))
            self.finished_signal.emit(str(out_path))

        except Exception as exc:
            import traceback
            traceback.print_exc()  # full traceback visible in the terminal
            self.error.emit(f"{exc}\n{traceback.format_exc()}")


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
        self._all_shots_worker: Optional[AllShotsWorker] = None
        self._scenes_worker: Optional[ScenesWorker] = None
        self._pdf_worker: Optional[PdfExportWorker] = None
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
        self.canvas.installEventFilter(self)
        self.canvas.viewport().installEventFilter(self)

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

        # Status label lives at the bottom of the control panel; create it
        # before calling _build_control_panel so the builder can place it.
        self.status = _StatusLabel()

        ctrl = self._build_control_panel()
        root.addWidget(ctrl)

        self.status.showMessage("Ready — enter a query and press Search, or choose Shots / Scenes.")

        self.canvas.btn_open_visualizer.connect(self._on_btn_open_visualizer)
        self.canvas.btn_toggle_ignore.connect(self._on_btn_toggle_ignore)
        self.canvas.btn_add_scene.connect(self._on_btn_add_scene)
        self.canvas.btn_remove_scene.connect(self._on_btn_remove_scene)
        self._populate_movies()

        # Ensure Ctrl+Q/W quit even when a QLineEdit or QListWidget has keyboard focus.
        for seq in ("Ctrl+Q", "Ctrl+W"):
            QShortcut(QKeySequence(seq), self, self.close)

    # ------------------------------------------------------------------
    # Control panel

    def _on_btn_open_visualizer(self, result: dict) -> None:
        """▶  Open the shot in the Shotlist Visualiser."""
        filename = result.get("filename") or result.get("movie_title", "")
        shot_id  = str(result.get("shot_id", ""))
        if not filename:
            self.status.showMessage("Cannot open shot: no filename in result.", 4000)
            return

        from visualizers.shot_visualizer import open_at_shot
        open_at_shot(self.project_path, filename, "movies", shot_id=shot_id,
                     loop=True, no_continue=True, play=True)
        self.status.showMessage(
            f"Opening Shotlist Visualizer → {filename}  shot {shot_id}", 4000
        )

    def _on_btn_toggle_ignore(self, result: dict) -> None:
        """🛈  Toggle the Ignore flag for this shot and save the shotlist."""
        from data.shotlist import read_shotlist, write_shotlist
        filename = result.get("filename", "")
        shot_id  = result.get("shot_id", "")
        if not filename or not shot_id:
            return
        try:
            shots = read_shotlist(self.project_path, filename, "movies")
            new_ignored = False
            for shot in shots:
                if shot.get("shot_id") == shot_id:
                    current     = str(shot.get("Ignore", "")).strip().lower()
                    new_ignored = current not in ("true", "1", "yes")
                    shot["Ignore"] = "True" if new_ignored else "False"
                    break
            write_shotlist(self.project_path, filename, "movies", shots)
            result["Ignore"] = "True" if new_ignored else "False"
            tile = self.canvas.tile_for_shot(shot_id)
            if tile:
                tile.set_ignored(new_ignored)
            self.status.showMessage(
                f"Shot {shot_id}: Ignore = {'True' if new_ignored else 'False'}", 3000
            )
        except Exception as exc:
            import traceback; traceback.print_exc()
            self.status.showMessage(f"Error toggling ignore: {exc}", 5000)

    def _on_btn_add_scene(self, result: dict) -> None:
        """⊕  Insert a new scene boundary starting at this shot and save."""
        from data.shotlist import read_shotlist, write_shotlist
        filename = result.get("filename", "")
        shot_id  = result.get("shot_id", "")
        if not filename or not shot_id:
            return
        try:
            shots = read_shotlist(self.project_path, filename, "movies")
            shot_idx = next(
                (i for i, s in enumerate(shots) if s.get("shot_id") == shot_id), None
            )
            if shot_idx is None or shot_idx == 0:
                self.status.showMessage("Cannot add scene boundary at first shot.", 3000)
                return
            current_scene = shots[shot_idx].get("Scene", "")
            # Guard: if shot_S is already the first shot of its scene, there is
            # nothing to split off — the split would create an empty preceding scene.
            prev_scene = str(shots[shot_idx - 1].get("Scene", "")).strip()
            if prev_scene != str(current_scene).strip():
                self.status.showMessage(
                    "This shot is already the first of its scene — nothing to split.", 3000
                )
                return
            _SPLIT = "__NEW_SCENE__"
            for i in range(shot_idx, len(shots)):
                if shots[i].get("Scene") == current_scene:
                    shots[i]["Scene"] = _SPLIT
                else:
                    break
            _renumber_scenes(shots)
            write_shotlist(self.project_path, filename, "movies", shots)

            # Build shot_id → new scene map from the modified shots list
            shot_scene_map = {
                str(s.get("shot_id", "")): str(s.get("Scene", ""))
                for s in shots
            }

            # Find the frame tile for this shot
            tile_idx = next(
                (i for i, t in enumerate(self.canvas._tiles)
                 if t.result.get("shot_id") == shot_id),
                None,
            )
            if tile_idx is None:
                self._restart_scenes_view(filename)
                self.status.showMessage("New scene boundary added.", 3000)
                return

            # Save scroll so the clicked tile stays at the same viewport position
            frame_tile = self.canvas._tiles[tile_idx]
            old_tile_y = frame_tile.y()
            old_scroll = self.canvas.verticalScrollBar().value()

            # Get vid dimensions from the nearest preceding scene-card tile
            vid_w, vid_h = 320, 180
            for t in reversed(self.canvas._tiles[:tile_idx]):
                if t.result.get("is_label") and not t.result.get("is_title"):
                    vid_w = t.result.get("vid_w", 320) or 320
                    vid_h = t.result.get("vid_h", 180) or 180
                    break

            # Create the new scene card for the freshly split scene
            new_scene_str = shot_scene_map.get(shot_id, "?")
            scene_card_result = {
                "filename":       filename,
                "movie_id":       frame_tile.result.get("movie_id", ""),
                "scene":          new_scene_str,
                "is_label":       True,
                "is_first_scene": False,
                "label_text":     new_scene_str,
                "vid_w":          vid_w,
                "vid_h":          vid_h,
                "caption":        "",
                "matched_fields": [],
                "matched_text":   "",
                "score":          0.0,
            }
            scene_card_pixmap = self._make_scene_card_pixmap(scene_card_result)
            new_tile = TileWidget(
                scene_card_result, scene_card_pixmap,
                self.canvas._tile_size, parent=self.canvas._container,
            )

            # Insert the scene card before the frame tile, then update labels
            self.canvas.insert_tile(tile_idx, new_tile)
            self._update_tile_scenes_from(tile_idx + 1, shot_scene_map)

            # Restore scroll: frame tile moved down by the scene card height
            new_tile_y = self.canvas._tiles[tile_idx + 1].y()
            self.canvas.verticalScrollBar().setValue(
                old_scroll + (new_tile_y - old_tile_y)
            )

            self.status.showMessage("New scene boundary added.", 3000)
        except Exception as exc:
            import traceback; traceback.print_exc()
            self.status.showMessage(f"Error adding scene: {exc}", 5000)

    def _on_btn_remove_scene(self, result: dict) -> None:
        """ⓧ  Remove a scene boundary card and merge into the previous scene."""
        from data.shotlist import read_shotlist, write_shotlist
        filename     = result.get("filename", "")
        target_scene = result.get("scene", "")
        if not filename or not target_scene:
            return
        try:
            shots = read_shotlist(self.project_path, filename, "movies")
            first_idx = next(
                (i for i, s in enumerate(shots) if s.get("Scene") == target_scene), None
            )
            if first_idx is None or first_idx == 0:
                self.status.showMessage("Cannot remove first scene boundary.", 3000)
                return
            prev_scene = shots[first_idx - 1].get("Scene", "")
            for shot in shots:
                if shot.get("Scene") == target_scene:
                    shot["Scene"] = prev_scene
            _renumber_scenes(shots)
            write_shotlist(self.project_path, filename, "movies", shots)

            # Build shot_id → new scene map from the modified shots list
            shot_scene_map = {
                str(s.get("shot_id", "")): str(s.get("Scene", ""))
                for s in shots
            }

            # Find the scene card tile for target_scene
            sc_idx = next(
                (i for i, t in enumerate(self.canvas._tiles)
                 if t.result.get("is_label")
                 and not t.result.get("is_title")
                 and t.result.get("scene") == target_scene),
                None,
            )
            if sc_idx is None:
                self._restart_scenes_view(filename)
                self.status.showMessage("Scene boundary removed.", 3000)
                return

            # Save scroll relative to the first frame tile after the card
            first_frame_idx = next(
                (i for i in range(sc_idx + 1, len(self.canvas._tiles))
                 if not self.canvas._tiles[i].result.get("is_label")),
                None,
            )
            if first_frame_idx is not None:
                old_frame_y = self.canvas._tiles[first_frame_idx].y()
            old_scroll = self.canvas.verticalScrollBar().value()

            # Surgically remove the scene card tile
            self.canvas.remove_tile(sc_idx)

            # Update scene labels on everything from the removal point onwards
            self._update_tile_scenes_from(sc_idx, shot_scene_map)

            # Restore scroll: frame tile moved up by the scene card height
            if first_frame_idx is not None:
                new_frame_idx = first_frame_idx - 1  # shifted by the removal
                if new_frame_idx < len(self.canvas._tiles):
                    new_frame_y = self.canvas._tiles[new_frame_idx].y()
                    self.canvas.verticalScrollBar().setValue(
                        max(0, old_scroll + (new_frame_y - old_frame_y))
                    )

            self.status.showMessage("Scene boundary removed.", 3000)
        except Exception as exc:
            import traceback; traceback.print_exc()
            self.status.showMessage(f"Error removing scene: {exc}", 5000)

    def _restart_scenes_view(self, filename: str) -> None:
        """Full scenes reload for *filename* (fallback for non-incremental changes)."""
        idx = self.movie_combo.findData(filename)
        if idx >= 0:
            self.movie_combo.setCurrentIndex(idx)
        self._start_scenes_worker(filename)

    def _find_shot_resume_point(
        self, shot_id: str
    ) -> "tuple[int | None, str | None]":
        """Return (tile_idx, initial_scene) for a partial refresh starting at *shot_id*.

        tile_idx      — index of the TileWidget for *shot_id* in canvas._tiles.
        initial_scene — Scene value of the tile immediately before that index;
                        the worker uses this to avoid emitting a spurious scene
                        card for what is still the same scene as the kept tiles.
        """
        tiles = self.canvas._tiles
        for i, tile in enumerate(tiles):
            if tile.result.get("shot_id") == shot_id:
                initial_scene = tiles[i - 1].result.get("scene", "") if i > 0 else None
                return i, initial_scene
        return None, None

    def _find_scene_remove_resume_point(
        self, target_scene: str
    ) -> "tuple[int | None, str | None, str | None]":
        """Return (scene_card_idx, resume_shot_id, initial_scene) for remove_scene.

        scene_card_idx — index of the scene-card TileWidget for *target_scene*.
        resume_shot_id — shot_id of the first frame tile after that card.
        initial_scene  — Scene value of the tile immediately before the card;
                         the worker uses this so the merged shots produce no
                         spurious scene card.
        """
        tiles = self.canvas._tiles
        for i, tile in enumerate(tiles):
            if (
                tile.result.get("is_label")
                and not tile.result.get("is_title")
                and tile.result.get("scene") == target_scene
            ):
                initial_scene  = tiles[i - 1].result.get("scene", "") if i > 0 else None
                resume_shot_id = next(
                    (t.result.get("shot_id") for t in tiles[i + 1:]
                     if not t.result.get("is_label")),
                    None,
                )
                return i, resume_shot_id, initial_scene
        return None, None, None

    def _update_tile_scenes_from(self, start_idx: int, shot_scene_map: dict) -> None:
        """Update scene values on tiles from *start_idx* onwards without reloading.

        For frame tiles:  updates result["scene"] from shot_scene_map.
        For scene-card tiles:  waits for the next frame tile to determine the
        new label, then updates result["scene"], result["label_text"], and
        re-renders the card pixmap.
        """
        tiles = self.canvas._tiles
        pending_card: "TileWidget | None" = None
        for i in range(start_idx, len(tiles)):
            tile = tiles[i]
            if tile.result.get("is_label") and not tile.result.get("is_title"):
                pending_card = tile
            elif not tile.result.get("is_label"):
                shot_id   = tile.result.get("shot_id", "")
                new_scene = shot_scene_map.get(shot_id, tile.result.get("scene", ""))
                tile.result["scene"] = new_scene
                if pending_card is not None:
                    if pending_card.result.get("label_text") != new_scene:
                        pending_card.result["scene"]      = new_scene
                        pending_card.result["label_text"] = new_scene
                        new_px = self._make_scene_card_pixmap(pending_card.result)
                        pending_card.original_pixmap = new_px
                        pending_card._render(self.canvas._tile_size)
                    pending_card = None

    # ------------------------------------------------------------------
    # Control panel

    def _build_control_panel(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(_CTRL_PANEL_WIDTH)
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
        self.movie_combo.installEventFilter(self)
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
        self.field_combo.installEventFilter(self)
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

        btn_grid = QGridLayout()
        btn_grid.setSpacing(4)
        btn_grid.setContentsMargins(4, 4, 4, 4)

        self.search_btn = QPushButton("Search")
        self.search_btn.setEnabled(False)
        self.search_btn.setToolTip("Search shot annotations for the query term")
        self.search_btn.clicked.connect(self._on_search)
        btn_grid.addWidget(self.search_btn, 0, 0)

        self.best_btn = QPushButton("Best")
        self.best_btn.setCheckable(True)
        self.best_btn.setEnabled(False)
        self.best_btn.setToolTip("Show CLIP-scored best frame for each matched shot")
        self.best_btn.clicked.connect(self._on_best_toggle)
        btn_grid.addWidget(self.best_btn, 0, 1)

        self.shots_btn = QPushButton("Shots")
        self.shots_btn.setEnabled(False)
        self.shots_btn.setToolTip("Browse all shots of the selected movie (uses Best frame if active)")
        self.shots_btn.clicked.connect(self._on_shots_clicked)
        btn_grid.addWidget(self.shots_btn, 1, 0)

        self.scenes_btn = QPushButton("Scenes")
        self.scenes_btn.setEnabled(False)
        self.scenes_btn.setToolTip("Browse shots grouped by scene, one row per scene (uses Best frame if active)")
        self.scenes_btn.clicked.connect(self._on_scenes_clicked)
        btn_grid.addWidget(self.scenes_btn, 1, 1)

        self.pdf_btn = QPushButton("PDF")
        self.pdf_btn.setEnabled(False)
        self.pdf_btn.setToolTip("Export a PDF contact sheet of the current results")
        self.pdf_btn.clicked.connect(self._on_export_pdf)
        btn_grid.addWidget(self.pdf_btn, 2, 0)

        self.video_btn = QPushButton("Video")
        self.video_btn.setEnabled(False)
        self.video_btn.setToolTip(
            "Generate a looping video mosaic (.mp4) from the current results\n"
            "saved to output/mosaics/video/search/"
        )
        self.video_btn.clicked.connect(self._on_save_video)
        btn_grid.addWidget(self.video_btn, 2, 1)

        btn_container = QFrame()
        btn_container.setStyleSheet(
            f"QFrame {{ background: {theme.INPUT_BG}; border-radius: 3px; }}"
        )
        btn_container.setLayout(btn_grid)
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
        for opt in ["1", "5", "10", "50", "200", "all"]:
            self.limit_combo.addItem(opt)
        self.limit_combo.setCurrentText("50")
        limit_row.addWidget(lim_label)
        limit_row.addWidget(self.limit_combo)
        opt_layout.addLayout(limit_row)

        self.limit_per_movie_cb = QCheckBox("Limit per movie")
        opt_layout.addWidget(self.limit_per_movie_cb)

        fps_row = QHBoxLayout()
        fps_label = QLabel("FPS:")
        fps_label.setFixedWidth(46)
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 30)
        self.fps_spin.setValue(8)
        fps_row.addWidget(fps_label)
        fps_row.addWidget(self.fps_spin)
        opt_layout.addLayout(fps_row)

        dur_row = QHBoxLayout()
        dur_label = QLabel("Dur (s):")
        dur_label.setFixedWidth(46)
        self.dur_spin = QSpinBox()
        self.dur_spin.setRange(1, 10)
        self.dur_spin.setValue(2)
        dur_row.addWidget(dur_label)
        dur_row.addWidget(self.dur_spin)
        opt_layout.addLayout(dur_row)

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
        layout.addWidget(self.status)
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
        self.best_btn.setEnabled(has_single_movie)
        self.shots_btn.setEnabled(has_single_movie)
        self.scenes_btn.setEnabled(has_single_movie)
        if not has_single_movie:
            self.best_btn.setChecked(False)
            self.best_mode = False
        self._update_search_button()

    def _update_search_button(self) -> None:
        has_query = bool(self.query_input.text().strip())
        self.search_btn.setEnabled(has_query)

    def _on_best_toggle(self) -> None:
        self.best_mode = self.best_btn.isChecked()
        self._update_search_button()

    # ------------------------------------------------------------------
    # Search flow

    def _on_search(self) -> None:
        query = self.query_input.text().strip()
        if not query:
            self.status.showMessage("Enter a search query first.")
            return

        # Stop any in-flight workers
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(3000)
        if self._all_shots_worker and self._all_shots_worker.isRunning():
            self._all_shots_worker.cancel()
            self._all_shots_worker.wait(3000)
        if self._scenes_worker and self._scenes_worker.isRunning():
            self._scenes_worker.cancel()
            self._scenes_worker.wait(3000)

        self.canvas.clear()
        self._current_results = []
        self.pdf_btn.setEnabled(False)
        self.video_btn.setEnabled(False)

        scope_data = self.movie_combo.currentData()
        scope      = scope_data if scope_data else None
        field_text = self.field_combo.currentText()
        field      = None if field_text == "--all" else field_text
        limit_text = self.limit_combo.currentText()
        limit      = None if limit_text == "all" else int(limit_text)
        limit_per_movie = self.limit_per_movie_cb.isChecked()

        self.search_btn.setEnabled(False)
        self.status.set_busy_style(True)
        self.status.showMessage(f"Searching for '{query}'…")
        self._progress.setRange(0, 0)  # indeterminate while searching
        self._progress.setValue(0)

        self._best_lookup = {}
        self._query_best_active = self.best_mode and bool(query)
        if self.best_mode and scope and not self._query_best_active:
            from services.frame_match import load_best_frame_lookup
            self._best_lookup = load_best_frame_lookup(
                self.project_path, scope, "movies"
            )

        from tool import prefs as _prefs
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
        self.status.set_busy_style(False)
        self.pdf_btn.setEnabled(count > 0)
        self.video_btn.setEnabled(count > 0)
        if count == 0:
            self.status.showMessage("No results found.")
        else:
            self.status.showMessage(
                f"{count} result(s)  —  Ctrl + scroll to zoom,  scroll to pan"
            )

    def _on_search_error(self, message: str) -> None:
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self.search_btn.setEnabled(True)
        self.status.set_busy_style(False)
        preview = message.splitlines()[0][:120]
        self.status.showMessage(f"Error: {preview}")

    # ------------------------------------------------------------------
    # Best-only render (no query)

    def _render_best_only(self, filename: str) -> None:
        from services.frame_match import load_best_frame_lookup

        # Stop any in-flight workers
        if self._best_worker and self._best_worker.isRunning():
            self._best_worker.cancel()
            self._best_worker.wait(3000)
        if self._all_shots_worker and self._all_shots_worker.isRunning():
            self._all_shots_worker.cancel()
            self._all_shots_worker.wait(3000)
        if self._scenes_worker and self._scenes_worker.isRunning():
            self._scenes_worker.cancel()
            self._scenes_worker.wait(3000)

        # Show immediate feedback — indeterminate bar while JSON loads
        self._progress.setRange(0, 0)
        self._progress.setValue(0)
        self.search_btn.setEnabled(False)
        self.best_btn.setEnabled(False)
        self.status.set_busy_style(True)
        self.status.showMessage("Loading best frames…")

        self.canvas.clear()
        self._current_results = []
        self.pdf_btn.setEnabled(False)
        self.video_btn.setEnabled(False)

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
        pixmap = None
        if path_str:
            pixmap = QPixmap(path_str)
            # Apply SAR correction so stored best-frame PNGs display correctly
            movie_id   = result.get("movie_id", "")
            video_path = _find_video_path(self.project_path, movie_id)
            if video_path is not None and pixmap is not None and not pixmap.isNull():
                sar = _get_sar(str(video_path))
                if sar != (1, 1):
                    new_w = int(round(pixmap.width() * sar[0] / sar[1]))
                    pixmap = pixmap.scaled(
                        new_w, pixmap.height(),
                        Qt.IgnoreAspectRatio, Qt.SmoothTransformation,
                    )
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
        self.status.set_busy_style(False)
        self.pdf_btn.setEnabled(count > 0)
        self.video_btn.setEnabled(count > 0)
        if count == 0:
            self.status.showMessage("No best frames found.")
        else:
            self.status.showMessage(
                f"{count} best frame(s)  —  Ctrl + scroll to zoom,  scroll to pan"
            )

    # ------------------------------------------------------------------
    # All-shots browse (no query)

    def _render_all_shots(self, filename: str) -> None:
        """Show the first frame of every shot for *filename* (no query needed)."""
        # Stop any in-flight workers
        if self._all_shots_worker and self._all_shots_worker.isRunning():
            self._all_shots_worker.cancel()
            self._all_shots_worker.wait(3000)
        if self._best_worker and self._best_worker.isRunning():
            self._best_worker.cancel()
            self._best_worker.wait(3000)
        if self._scenes_worker and self._scenes_worker.isRunning():
            self._scenes_worker.cancel()
            self._scenes_worker.wait(3000)

        self._progress.setRange(0, 0)
        self._progress.setValue(0)
        self.search_btn.setEnabled(False)
        self.status.set_busy_style(True)
        self.status.showMessage("Loading all shot frames…")

        self.canvas.clear()
        self._current_results = []
        self.pdf_btn.setEnabled(False)
        self.video_btn.setEnabled(False)

        self._all_shots_worker = AllShotsWorker(filename, self.project_path)
        self._all_shots_worker.tile_ready.connect(self._on_all_shots_tile_ready)
        self._all_shots_worker.finished_signal.connect(self._on_all_shots_done)
        self._all_shots_worker.error.connect(self._on_all_shots_error)
        self._all_shots_worker.start()

    def _on_all_shots_tile_ready(self, result: dict, pixmap) -> None:
        self._current_results.append(result)
        self.canvas.add_tile(result, pixmap)
        self.status.showMessage(f"Loading… {self.canvas.tile_count} tile(s)")

    def _on_all_shots_done(self, count: int) -> None:
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self.search_btn.setEnabled(True)
        self.status.set_busy_style(False)
        self.pdf_btn.setEnabled(count > 0)
        self.video_btn.setEnabled(count > 0)
        if count == 0:
            self.status.showMessage("No shots found.")
        else:
            self.status.showMessage(
                f"{count} shot(s)  —  Ctrl + scroll to zoom,  scroll to pan"
            )

    def _on_all_shots_error(self, message: str) -> None:
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self.search_btn.setEnabled(True)
        self.status.set_busy_style(False)
        preview = message.splitlines()[0][:120]
        self.status.showMessage(f"Error: {preview}")

    # ------------------------------------------------------------------
    # Shots button (dedicated browse)

    def _on_shots_clicked(self) -> None:
        """Browse all shots of the selected movie (respects Best mode)."""
        filename = self.movie_combo.currentData()
        if not filename:
            return
        if self.best_mode:
            self._render_best_only(filename)
        else:
            self._render_all_shots(filename)

    # ------------------------------------------------------------------
    # Scenes button

    def _on_scenes_clicked(self) -> None:
        """Browse shots grouped by scene, inserting row breaks at scene changes."""
        filename = self.movie_combo.currentData()
        if not filename:
            return
        self._start_scenes_worker(filename)

    def _start_scenes_worker(
        self,
        filename: str,
        resume_from_shot_id: "str | None" = None,
        initial_scene: "str | None" = None,
    ) -> None:
        """Cancel any in-flight workers and (re)start the ScenesWorker.

        When *resume_from_shot_id* is given the canvas is NOT cleared; the
        caller must have already truncated it to the correct index.  The
        worker will skip all shots before *resume_from_shot_id* and append
        tiles from there.
        """
        for w in (
            self._worker,
            self._best_worker,
            self._all_shots_worker,
            self._scenes_worker,
        ):
            if w and w.isRunning():
                w.cancel()
                w.wait(3000)

        partial = resume_from_shot_id is not None
        if not partial:
            self.canvas.clear()
            self._current_results = []
            self.pdf_btn.setEnabled(False)
            self.video_btn.setEnabled(False)
        self._progress.setRange(0, 0)
        self._progress.setValue(0)
        self.search_btn.setEnabled(False)
        self.status.set_busy_style(True)
        self.status.showMessage("Updating scene…" if partial else "Loading scenes…")

        best_lookup: dict = {}
        if self.best_mode:
            from services.frame_match import load_best_frame_lookup
            best_lookup = load_best_frame_lookup(self.project_path, filename, "movies")

        self._scenes_worker = ScenesWorker(
            filename, self.project_path, best_lookup,
            resume_from_shot_id=resume_from_shot_id,
            initial_scene=initial_scene,
        )
        self._scenes_worker.tile_ready.connect(self._on_scenes_tile_ready)
        self._scenes_worker.finished_signal.connect(self._on_scenes_done)
        self._scenes_worker.error.connect(self._on_scenes_error)
        self._scenes_worker.start()

    def _make_scene_card_pixmap(self, result: dict) -> "QPixmap":
        """Render and return a grey scene-label card QPixmap from *result*."""
        vid_w  = result.get("vid_w", 320) or 320
        vid_h  = result.get("vid_h", 180) or 180
        base_h = 180
        base_w = round(base_h * vid_w / vid_h) if vid_h > 0 else 320

        grey = QPixmap(base_w, base_h)
        grey.fill(QColor(128, 128, 128))

        # Load both Clarendon weights (Qt caches on repeated calls)
        _bold_id   = QFontDatabase.addApplicationFont(str(_FONT_DIR / "LibreClarendonNormal-162Bold.otf"))
        _light_id  = QFontDatabase.addApplicationFont(str(_FONT_DIR / "LibreClarendonNormal-42Light.otf"))
        _bold_fam  = QFontDatabase.applicationFontFamilies(_bold_id)
        _light_fam = QFontDatabase.applicationFontFamilies(_light_id)

        _db = QFontDatabase()
        def _pick_font(fam, keyword, pt):
            if not fam:
                return QFont()
            styles = _db.styles(fam[0])
            style  = next((s for s in styles if keyword in s.lower()), styles[0] if styles else "")
            return _db.font(fam[0], style, pt)

        _painter = QPainter(grey)
        _painter.setPen(QColor(255, 255, 255))

        if result.get("is_title") and _bold_fam and _light_fam:
            # Two-line block: title (Bold) + year (Light), vertically centred
            _pt_title = max(1, round(base_h * 44 / 360))  # ~22pt
            _pt_year  = max(1, round(base_h * 28 / 360))  # ~14pt
            _gap      = max(2, round(base_h * 6  / 360))  # ~3px
            _f_title  = _pick_font(_bold_fam,  "bold",  _pt_title)
            _f_year   = _pick_font(_light_fam, "light", _pt_year)

            _painter.setFont(_f_title)
            _lh_title = _painter.fontMetrics().height()
            _painter.setFont(_f_year)
            _lh_year  = _painter.fontMetrics().height()

            _year_text = str(result.get("movie_year", ""))
            _block_h   = _lh_title + (_gap + _lh_year if _year_text else 0)
            _y0        = (base_h - _block_h) // 2

            _painter.setFont(_f_title)
            _painter.drawText(
                QRect(0, _y0, base_w, _lh_title + 4),
                Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap,
                result.get("label_text", ""),
            )
            if _year_text:
                _painter.setFont(_f_year)
                _painter.drawText(
                    QRect(0, _y0 + _lh_title + _gap, base_w, _lh_year + 4),
                    Qt.AlignHCenter | Qt.AlignTop,
                    _year_text,
                )

        elif _light_fam:
            # Scene-index card — large scene label in Light, centred
            _pt_scene = max(1, round(base_h * 80 / 360))  # ~40pt
            _painter.setFont(_pick_font(_light_fam, "light", _pt_scene))
            _painter.drawText(
                grey.rect(),
                Qt.AlignCenter,
                result.get("label_text", ""),
            )

        _painter.end()
        return grey

    def _on_scenes_tile_ready(self, result: dict, pixmap) -> None:
        if result.get("is_label"):
            pixmap = self._make_scene_card_pixmap(result)
        else:
            self._current_results.append(result)
        self.canvas.add_tile(result, pixmap)
        self.status.showMessage(f"Loading… {self.canvas.tile_count} tile(s)")

    def _on_scenes_done(self, count: int) -> None:
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self.search_btn.setEnabled(True)
        self.status.set_busy_style(False)
        self.pdf_btn.setEnabled(count > 0)
        self.video_btn.setEnabled(count > 0)
        if count == 0:
            self.status.showMessage("No shots/scenes found.")
        else:
            self.status.showMessage(
                f"{count} shot(s)  —  Ctrl + scroll to zoom,  scroll to pan"
            )

    def _on_scenes_error(self, message: str) -> None:
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self.search_btn.setEnabled(True)
        self.status.set_busy_style(False)
        preview = message.splitlines()[0][:120]
        self.status.showMessage(f"Scenes error: {preview}")

    # ------------------------------------------------------------------
    # PDF export

    def _on_export_pdf(self) -> None:
        if self._pdf_worker and self._pdf_worker.isRunning():
            return

        # Build export list from exactly what is visible on the canvas.
        # • Title cards and scene cards → included as intertitle tiles.
        # • Frame tiles with Ignore=True → skipped.
        export_items: list[dict] = []
        for tile in self.canvas._tiles:
            r = tile.result
            if r.get("is_label"):
                export_items.append(r)
            else:
                if str(r.get("Ignore", "")).strip().lower() in ("true", "1", "yes"):
                    continue
                export_items.append(r)

        frame_count = sum(1 for r in export_items if not r.get("is_label"))
        if frame_count == 0:
            self.status.showMessage("No non-ignored frames to export.")
            return

        query    = self.query_input.text().strip()
        filename = self.movie_combo.currentData() or ""

        # Route to the correct subfolder based on the active view:
        # scenes have label tiles; shots have no query; searches have a query.
        has_labels = any(r.get("is_label") for r in export_items)
        if has_labels:
            subfolder = "scenes"
        elif query:
            subfolder = "searches"
        else:
            subfolder = "shots"

        self.pdf_btn.setEnabled(False)
        self.search_btn.setEnabled(False)
        self.status.showMessage(f"Exporting PDF for {frame_count} frame(s)…")
        self.status.set_busy_style(True)

        self._pdf_worker = PdfExportWorker(export_items, self.project_path, query, filename, subfolder)
        self._pdf_worker.finished_signal.connect(self._on_pdf_export_done)
        self._pdf_worker.progress.connect(self._on_pdf_export_progress)
        self._pdf_worker.error.connect(self._on_pdf_export_error)
        self._pdf_worker.start()

    def _on_pdf_export_progress(self, current: int, total: int) -> None:
        self.status.showMessage(f"Exporting PDF… loading frame {current} / {total}")

    def _on_pdf_export_done(self, out_path: str) -> None:
        import subprocess
        self.status.set_busy_style(False)
        self.pdf_btn.setEnabled(True)
        self.search_btn.setEnabled(True)
        self.status.showMessage(f"PDF saved → {out_path}")
        subprocess.Popen(["xdg-open", str(Path(out_path).parent)])

    def _on_pdf_export_error(self, message: str) -> None:
        self.status.set_busy_style(False)
        self.pdf_btn.setEnabled(True)
        self.search_btn.setEnabled(True)
        preview = message.splitlines()[0][:120]
        self.status.showMessage(f"PDF export error: {preview}")

    def _on_save_video(self) -> None:
        """Generate a looping video mosaic (.mp4) from the current results."""
        if not self._current_results:
            self.status.showMessage("No results to generate video from — run a search first.")
            return

        if self._video_worker and self._video_worker.isRunning():
            return

        query      = self.query_input.text().strip()
        limit_text = self.limit_combo.currentText()
        limit      = 50 if limit_text == "all" else int(limit_text)
        fps        = self.fps_spin.value()
        duration   = self.dur_spin.value()

        self.video_btn.setEnabled(False)
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
        self.video_btn.setEnabled(True)
        self.search_btn.setEnabled(True)
        self.status.showMessage(f"✓ Saved: {out_path}  — opening in looping player…")
        _open_video_looping(out_path)

    def _on_video_error(self, message: str) -> None:
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self.video_btn.setEnabled(True)
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

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """Intercept Home/End/PgUp/PgDn on combo boxes to override native
        QComboBox behaviour: Home/End step one title; PgUp/PgDn cycle field."""
        if event.type() == QEvent.KeyPress:
            key = event.key()
            if key in (Qt.Key_Home, Qt.Key_End):
                # Always navigate titles, regardless of which combo has focus
                idx = self.movie_combo.currentIndex()
                if key == Qt.Key_Home:
                    if idx > 0:
                        self.movie_combo.setCurrentIndex(idx - 1)
                else:
                    if idx < self.movie_combo.count() - 1:
                        self.movie_combo.setCurrentIndex(idx + 1)
                return True
            if key in (Qt.Key_PageUp, Qt.Key_PageDown):
                if obj in (self.movie_combo, self.field_combo, self.canvas, self.canvas.viewport()):
                    idx = self.field_combo.currentIndex()
                    if key == Qt.Key_PageUp:
                        if idx > 0:
                            self.field_combo.setCurrentIndex(idx - 1)
                    else:
                        if idx < self.field_combo.count() - 1:
                            self.field_combo.setCurrentIndex(idx + 1)
                    return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key in (Qt.Key_Q, Qt.Key_W) and event.modifiers() & Qt.ControlModifier:
            self.close()
            return
        if key == KEY_PREV_TITLE:
            idx = self.movie_combo.currentIndex()
            if idx > 0:
                self.movie_combo.setCurrentIndex(idx - 1)
        elif key == KEY_NEXT_TITLE:
            idx = self.movie_combo.currentIndex()
            if idx < self.movie_combo.count() - 1:
                self.movie_combo.setCurrentIndex(idx + 1)
        elif key == KEY_PREV_ITEM:
            idx = self.field_combo.currentIndex()
            if idx > 0:
                self.field_combo.setCurrentIndex(idx - 1)
        elif key == KEY_NEXT_ITEM:
            idx = self.field_combo.currentIndex()
            if idx < self.field_combo.count() - 1:
                self.field_combo.setCurrentIndex(idx + 1)
        else:
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
